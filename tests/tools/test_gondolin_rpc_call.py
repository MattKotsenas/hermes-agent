"""Tests for the gondolin_rpc_call CLI wrapper.

This module is the Popen-shaped bridge between BaseEnvironment._run_bash
(which expects a subprocess it can drain) and the gondolin-host daemon
(which speaks length-prefixed msgpack JSON-RPC over AF_UNIX). Per-call
invocation: the wrapper is stateless — connect, send one exec, write
result back, exit. Daemon crashes don't poison the next call.

These tests stand up a Python AF_UNIX server that mimics the daemon's
JSON-RPC contract so the wrapper can be exercised without booting a VM.
A separate end-to-end test launches the real Node daemon in stub mode
to verify both halves agree on the wire format.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import msgpack
import pytest


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
WRAPPER = REPO_ROOT / "tools" / "environments" / "gondolin_rpc_call.py"


def _encode_frame(obj: dict) -> bytes:
    """Encode a request/response as a length-prefixed msgpack frame."""
    payload = msgpack.packb(obj, use_bin_type=True)
    assert payload is not None
    return len(payload).to_bytes(4, "big") + payload


def _decode_frame_from_socket(conn: socket.socket) -> dict | None:
    """Read one full msgpack frame off ``conn`` or return None on EOF."""
    buf = bytearray()
    while True:
        if len(buf) >= 4:
            n = int.from_bytes(buf[:4], "big")
            if len(buf) >= 4 + n:
                payload = bytes(buf[4:4 + n])
                return msgpack.unpackb(payload, raw=False)
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf.extend(chunk)


class FakeDaemon:
    """Minimal AF_UNIX msgpack JSON-RPC server that scripts a single exec response.

    Accepts one connection, reads one length-prefixed msgpack request,
    writes back the configured response (also length-prefixed msgpack),
    then closes the connection. Designed to be one-shot per test.

    For streaming: pass `stream_frames=[...]` and the server will write
    each frame followed by `response` as the final terminator. Frames are
    wrapped as `{"id": <req-id>, "stream": <frame>}` to match the wire
    contract; pass them as plain dicts (e.g. {"kind": "stdout", "data": b"x"}).
    """

    def __init__(self, response: dict, stream_frames: list[dict] | None = None):
        self.response = response
        self.stream_frames = list(stream_frames or [])
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
            req = _decode_frame_from_socket(conn)
            if req is None:
                return
            self.received_request = req
            req_id = req.get("id") if isinstance(req, dict) else None
            # Emit any streaming frames first.
            for frame in self.stream_frames:
                conn.sendall(_encode_frame({"id": req_id, "stream": frame}))
            resp = dict(self.response)
            resp.setdefault("id", req_id)
            conn.sendall(_encode_frame(resp))


def _run_wrapper(
    sock_path: str,
    cmd: str,
    *,
    timeout_ms: int | None = None,
    env: dict | None = None,
    stream: bool = False,
    stdin_bytes: bytes | None = None,
    login: bool = False,
):
    """Invoke the wrapper as a subprocess; return CompletedProcess.

    ``stdin_bytes``: bytes to feed to the wrapper's stdin (pipe closed
    after write). Mirrors what BaseEnvironment._pipe_stdin does in
    production when ShellFileOperations.write_file pipes content into
    ``cat > path``. When None, the wrapper's stdin is closed immediately
    (DEVNULL-like).
    """
    full_env = os.environ.copy()
    # Make sure the wrapper can import from the repo without an editable install.
    full_env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    argv = [sys.executable, str(WRAPPER), sock_path, cmd]
    if timeout_ms is not None:
        argv.extend(["--timeout-ms", str(timeout_ms)])
    if stream:
        argv.append("--stream")
    if login:
        argv.append("--login")
    if stdin_bytes is None:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env=full_env,
        )
    # Use bytes mode so callers can probe non-UTF-8 stdin without the
    # text-mode encoder mangling it.
    return subprocess.run(
        argv,
        input=stdin_bytes,
        capture_output=True,
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
        s.sendall(_encode_frame({"id": 1, "method": "init", "params": {"config": {}}}))
        # Drain init response (one full msgpack frame). We don't need
        # the contents; just make sure the daemon has fully booted into
        # its initialized state before yielding the socket to the test.
        _decode_frame_from_socket(s)
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


# ---------------------------------------------------------------------------
# Streaming exec — chunks arrive at the wrapper's stdout as they're produced.
# ---------------------------------------------------------------------------

def test_streaming_writes_stdout_chunks_in_order():
    """With --stream, the wrapper sends `exec_stream` and writes each stdout
    chunk to its own stdout immediately, finishing with the daemon's exit_code."""
    frames = [
        {"kind": "stdout", "data": "alpha\n"},
        {"kind": "stdout", "data": "beta\n"},
        {"kind": "stdout", "data": "gamma\n"},
    ]
    response = {"result": {"exit_code": 0, "chunks": 3, "duration_ms": 4}}
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(daemon.sock_path, "echo abc", stream=True)

    assert result.returncode == 0
    assert result.stdout == "alpha\nbeta\ngamma\n"
    assert daemon.received_request["method"] == "exec_stream"
    assert daemon.received_request["params"]["cmd"] == "echo abc"


def test_streaming_stderr_chunks_go_to_stderr():
    """{kind: 'stderr', data: ...} frames write to the wrapper's stderr,
    not its stdout, so existing log-routing keeps working."""
    frames = [
        {"kind": "stdout", "data": "out1\n"},
        {"kind": "stderr", "data": "warn1\n"},
        {"kind": "stdout", "data": "out2\n"},
    ]
    response = {"result": {"exit_code": 0, "chunks": 3, "duration_ms": 4}}
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(daemon.sock_path, "noisy", stream=True)

    assert result.returncode == 0
    assert result.stdout == "out1\nout2\n"
    assert "warn1" in result.stderr


def test_streaming_nonzero_exit_relayed():
    """Final {result: {exit_code: N}} after a stream becomes the wrapper's exit code."""
    frames = [{"kind": "stdout", "data": "partial output\n"}]
    response = {"result": {"exit_code": 99, "chunks": 1, "duration_ms": 4}}
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(daemon.sock_path, "exit 99", stream=True)

    assert result.returncode == 99
    assert "partial output" in result.stdout


def test_streaming_rpc_error_after_partial_chunks():
    """Error final frame after some stream chunks: chunks still surface on
    stdout/stderr; wrapper exits nonzero with the error message on stderr."""
    frames = [{"kind": "stdout", "data": "before-crash\n"}]
    response = {"error": {"code": -32603, "message": "vm crashed mid-stream"}}
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(daemon.sock_path, "go", stream=True)

    assert result.returncode != 0
    assert "before-crash" in result.stdout
    assert "vm crashed mid-stream" in result.stderr


# ---------------------------------------------------------------------------
# Stdin forwarding - the wrapper must propagate its own stdin to the daemon
# as params.stdin so `cat > path` style writes actually get content.
# Regression: ShellFileOperations.write_file silently produced 0-byte files
# because gondolin_rpc_call dropped stdin on the floor.
# ---------------------------------------------------------------------------


def test_stdin_forwarded_to_daemon_in_params_non_stream():
    """Non-streaming exec: wrapper's stdin bytes appear in params.stdin."""
    response = {
        "result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}
    }
    payload = b"the quick brown fox jumps over the lazy dog\n"
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "cat > /tmp/out", stdin_bytes=payload)

    assert result.returncode == 0, result
    req = daemon.received_request
    assert req["method"] == "exec"
    # The wrapper sends bytes (msgpack bin) so the daemon can hand them
    # directly to vm.exec's stdin option as a Buffer without round-tripping
    # through UTF-8.
    assert req["params"].get("stdin") == payload, (
        f"expected stdin={payload!r}, got {req['params'].get('stdin')!r}"
    )


def test_no_stdin_means_no_stdin_field_in_params():
    """If the wrapper sees an empty stdin (DEVNULL or closed pipe), the
    request must NOT carry a stdin field - the daemon's exec stays exactly
    backwards-compatible for the no-input case."""
    response = {
        "result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}
    }
    # stdin_bytes=None routes through subprocess.run without input=, so
    # the child's stdin is implicitly closed (no pipe attached).
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "true")

    assert result.returncode == 0
    req = daemon.received_request
    assert "stdin" not in req["params"], (
        f"expected no stdin key in params for empty stdin, got {req['params']}"
    )


def test_stdin_forwarded_in_streaming_path():
    """Streaming exec_stream takes the same stdin path - the daemon needs
    stdin for write_file regardless of whether output is buffered or streamed."""
    frames = [{"kind": "stdout", "data": ""}]
    response = {"result": {"exit_code": 0, "chunks": 1, "duration_ms": 1}}
    payload = b"streamed-input\n"
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(
            daemon.sock_path, "cat > /tmp/out", stream=True, stdin_bytes=payload
        )

    assert result.returncode == 0, result
    req = daemon.received_request
    assert req["method"] == "exec_stream"
    assert req["params"].get("stdin") == payload, (
        f"expected stdin={payload!r}, got {req['params'].get('stdin')!r}"
    )


def test_binary_stdin_roundtrips_via_msgpack_bin():
    """Non-UTF-8 bytes (PNG header, embedded NULs, high bits) survive the
    wrapper -> msgpack -> daemon path byte-for-byte. ShellFileOperations
    is currently str-only but the wire should not be the bottleneck."""
    response = {
        "result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}
    }
    payload = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe\xfd"
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "cat > /tmp/bin", stdin_bytes=payload)

    assert result.returncode == 0
    received = daemon.received_request["params"].get("stdin")
    assert received == payload, f"binary stdin mangled: {received!r} != {payload!r}"




def test_login_flag_sets_params_login_true_on_the_wire():
    """--login wires through to params.login=true on the exec RPC.

    Every other Hermes backend (local, docker, singularity, ssh, modal,
    vercel) honors a login flag in ``_run_bash`` to control whether the
    spawned shell is ``bash -l -c`` (sources /etc/profile + profile.d)
    or just ``bash -c`` (no profile sourcing). Without this wire field
    the daemon can’t distinguish snapshot capture (login=true,
    once per session) from steady-state execs (login=false, every
    other call), and profile.d fires on every call — the gondolin
    drift that caused universal:6 to leak /opt/conda/bin/xz.
    """
    response = {
        "result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}
    }
    with FakeDaemon(response) as daemon:
        result = _run_wrapper(daemon.sock_path, "true", login=True)

    assert result.returncode == 0
    req = daemon.received_request
    assert req["method"] == "exec"
    assert req["params"].get("login") is True, (
        f"expected params.login=True, got {req['params']!r}"
    )


def test_no_login_flag_omits_login_from_params():
    """Without --login the wire payload must NOT carry a login field.

    Omission (rather than ``login: false``) is the wire-compat-friendly
    way to default — a future daemon could grow new login modes
    (login: "interactive" etc.) and a client that always sent
    ``login: false`` would lock itself out of the default."""
    response = {
        "result": {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1}
    }
    with FakeDaemon(response) as daemon:
        _run_wrapper(daemon.sock_path, "true")

    req = daemon.received_request
    assert "login" not in req["params"], (
        f"expected no login key in params, got {req['params']!r}"
    )


def test_login_flag_in_streaming_path():
    """--login also threads through exec_stream so the snapshot
    machinery (which streams its capture output) gets the same shell
    semantics as the non-streaming path."""
    frames = [{"kind": "stdout", "data": ""}]
    response = {"result": {"exit_code": 0, "chunks": 1, "duration_ms": 1}}
    with FakeDaemon(response, stream_frames=frames) as daemon:
        result = _run_wrapper(
            daemon.sock_path, "true", stream=True, login=True
        )

    assert result.returncode == 0
    req = daemon.received_request
    assert req["method"] == "exec_stream"
    assert req["params"].get("login") is True
