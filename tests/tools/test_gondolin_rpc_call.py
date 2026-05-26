"""Tests for the gondolin_rpc_call CLI wrapper.

This module is the Popen-shaped bridge between BaseEnvironment._run_bash
(which expects a subprocess it can drain) and the gondolin-host daemon
(which speaks line-delimited JSON-RPC over AF_UNIX). Per-call invocation:
the wrapper is stateless — connect, send one exec, write result back,
exit. Daemon crashes don't poison the next call.

These tests stand up a Python AF_UNIX server that mimics the daemon's
JSON-RPC contract so the wrapper can be exercised without booting a VM.
A separate end-to-end test launches the real Node daemon in stub mode
to verify both halves agree on the wire format.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
WRAPPER = REPO_ROOT / "tools" / "environments" / "gondolin_rpc_call.py"


class FakeDaemon:
    """Minimal AF_UNIX JSON-RPC server that scripts a single exec response.

    Accepts one connection, reads one newline-delimited JSON-RPC request,
    writes back the configured response (also newline-delimited), then
    closes the connection. Designed to be one-shot per test.
    """

    def __init__(self, response: dict):
        self.response = response
        self.received_request: dict | None = None
        self._tmpdir = tempfile.mkdtemp(prefix="gondolin-fake-")
        # Keep the path short — AF_UNIX has a ~104 byte cap on macOS.
        self.sock_path: str = os.path.join(self._tmpdir, "d.sock")
        self._sock: socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._sock.bind(self.sock_path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self._sock.close()
        except Exception:
            pass
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            line, _, _ = buf.partition(b"\n")
            req = json.loads(line.decode("utf-8"))
            self.received_request = req
            resp = dict(self.response)
            resp.setdefault("id", req.get("id"))
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


def _run_wrapper(sock_path: str, cmd: str, *, timeout_ms: int | None = None, env: dict | None = None):
    """Invoke the wrapper as a subprocess; return CompletedProcess."""
    full_env = os.environ.copy()
    # Make sure the wrapper can import from the repo without an editable install.
    full_env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    argv = [sys.executable, str(WRAPPER), sock_path, cmd]
    if timeout_ms is not None:
        argv.extend(["--timeout-ms", str(timeout_ms)])
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )


def test_happy_path_returns_stdout_and_exit_code():
    """Wrapper relays daemon's stdout/stderr/exit_code 1:1."""
    response = {
        "result": {
            "exit_code": 0,
            "stdout": "hello-from-vm\n",
            "stderr": "",
            "duration_ms": 12,
        }
    }
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "echo hello-from-vm")

    assert result.returncode == 0
    assert "hello-from-vm" in result.stdout
    assert result.stderr == ""
    # The wrapper must have sent an `exec` RPC with the command verbatim.
    req = daemon.received_request
    assert req["method"] == "exec"
    assert req["params"]["cmd"] == "echo hello-from-vm"


def test_nonzero_exit_relayed():
    """Daemon's nonzero exit code becomes the wrapper's exit code."""
    response = {
        "result": {
            "exit_code": 42,
            "stdout": "",
            "stderr": "boom\n",
            "duration_ms": 5,
        }
    }
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "exit 42")

    assert result.returncode == 42
    assert "boom" in result.stderr


def test_timeout_ms_forwarded_to_daemon():
    """`--timeout-ms` is passed through as `params.timeout_ms`."""
    response = {"result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}}
    with FakeDaemon(response) as daemon:
        _run_wrapper(daemon.sock_path, "true", timeout_ms=5000)

    assert daemon.received_request["params"]["timeout_ms"] == 5000


def test_rpc_error_exits_nonzero_with_diagnostic():
    """RPC-level errors (method not found, internal) exit nonzero and
    print a clear diagnostic to stderr."""
    response = {"error": {"code": -32603, "message": "vm crashed mid-exec"}}
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "true")

    assert result.returncode != 0
    assert "vm crashed mid-exec" in result.stderr


def test_connection_refused_exits_with_diagnostic(tmp_path):
    """Daemon not listening → wrapper exits nonzero with named-pipe-style
    error message that surfaces the missing socket."""
    missing = tmp_path / "no-such-socket.sock"
    result = _run_wrapper(str(missing), "true")

    assert result.returncode != 0
    assert "gondolin" in result.stderr.lower()
    # Either ENOENT (path missing) or ECONNREFUSED (path stale) — both clear.
    assert "socket" in result.stderr.lower() or "connect" in result.stderr.lower()


def test_policy_denied_flag_surfaced_on_stderr():
    """When the daemon reports `policy_denied: true`, the wrapper emits a
    sentinel line on stderr the env wrapper can match against."""
    response = {
        "result": {
            "exit_code": 7,
            "stdout": "",
            "stderr": "curl: (52) HTTP/1.1 403 Forbidden\n",
            "duration_ms": 3,
            "policy_denied": True,
        }
    }
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "curl https://blocked.example.com")

    assert result.returncode == 7
    assert "GONDOLIN_POLICY_DENIED" in result.stderr
    # Original stderr from the VM is still preserved.
    assert "403" in result.stderr


def test_large_stdout_does_not_truncate():
    """100 KB of stdout from the daemon must arrive intact at the
    wrapper's stdout — guards against pipe-buffer / recv-loop bugs."""
    big = "x" * 100_000
    response = {
        "result": {
            "exit_code": 0,
            "stdout": big + "\n",
            "stderr": "",
            "duration_ms": 50,
        }
    }
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "yes x | head -c 100000")

    assert result.returncode == 0
    assert len(result.stdout) >= 100_000
    assert result.stdout.startswith("x" * 100)


# ---------------------------------------------------------------------------
# End-to-end: real Node daemon ↔ Python wrapper over a real socket.
# ---------------------------------------------------------------------------
#
# The unit tests above exercise the wrapper against a Python stub server,
# which verifies the wrapper's behavior in isolation but not that the two
# halves agree on the wire format. This end-to-end test launches the
# actual Node daemon in stub mode (GONDOLIN_DAEMON_STUB_VM=1) so it
# doesn't need QEMU/KVM, and runs the real wrapper against it.

NODE_DAEMON = REPO_ROOT / "tools" / "environments" / "gondolin_host" / "src" / "daemon.mjs"
NODE_AVAILABLE = shutil.which("node") is not None and NODE_DAEMON.exists()


def _wait_for_socket(sock_path: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.close()
            return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
    raise RuntimeError(f"daemon socket {sock_path} never came up")


@pytest.fixture
def stubbed_daemon(tmp_path):
    """Launch the real Node daemon in stub mode (no VM). Yields the socket path."""
    if not NODE_AVAILABLE:
        pytest.skip("node or daemon.mjs not available")
    sock_path = str(tmp_path / "d.sock")
    proc = subprocess.Popen(
        ["node", str(NODE_DAEMON), "--socket", sock_path],
        env={
            **os.environ,
            "GONDOLIN_DAEMON_QUIET": "1",
            "GONDOLIN_DAEMON_STUB_VM": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_socket(sock_path)
        # Init the daemon (stub VM) so exec calls work.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        s.sendall(b'{"id":1,"method":"init","params":{"config":{}}}\n')
        s.recv(4096)  # drain init response
        s.close()
        yield sock_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_e2e_wrapper_talks_to_real_daemon(stubbed_daemon):
    """Wrapper successfully exec's through the real Node daemon over the
    real socket. Stub VM echoes the command back as stdout."""
    result = _run_wrapper(stubbed_daemon, "echo hi-from-vm")

    assert result.returncode == 0, f"wrapper failed: stderr={result.stderr!r}"
    assert "echo hi-from-vm" in result.stdout


def test_e2e_consecutive_calls_share_daemon(stubbed_daemon):
    """Two sequential wrapper invocations against the same daemon both
    succeed — proves the daemon accepts many short-lived connections."""
    a = _run_wrapper(stubbed_daemon, "echo first")
    b = _run_wrapper(stubbed_daemon, "echo second")

    assert a.returncode == 0
    assert b.returncode == 0
    assert "first" in a.stdout
    assert "second" in b.stdout
