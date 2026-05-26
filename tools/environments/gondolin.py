"""GondolinEnvironment — terminal backend that runs commands inside a
per-session Gondolin microVM via a long-lived Node daemon.

Architecture (see docs/design/gondolin-terminal-backend.md):

- One Node daemon per Hermes session, spawned at backend construction.
  The daemon owns the Gondolin VM and listens on an AF_UNIX socket
  inside the session's sandbox directory.
- Every ``_run_bash`` call spawns the Python wrapper
  (``tools.environments.gondolin_rpc_call``) as a subprocess. The
  wrapper opens a short-lived connection to the socket, sends one
  ``exec`` RPC, writes the daemon's stdout/stderr back, exits with the
  in-VM exit code. BaseEnvironment's existing Popen-shape machinery
  consumes that subprocess unchanged.
- ``cleanup()`` sends a ``shutdown`` RPC, waits briefly, and SIGKILLs
  the daemon if it doesn't exit. The daemon tears down its VM on
  receipt of SIGTERM as a backstop.

The environment is stateful about the daemon process and socket path
but stateless about individual exec calls — a wrapper crash, daemon
crash, or VM hang surfaces as a clean tool error on the next call.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.environments.base import BaseEnvironment, _popen_bash

logger = logging.getLogger(__name__)

# Location of the Node daemon source file relative to this module.
_HERE = Path(__file__).resolve().parent
_DAEMON_JS = _HERE / "gondolin_host" / "src" / "daemon.mjs"


def _ensure_node_available() -> None:
    if not shutil.which("node"):
        raise RuntimeError(
            "Node.js is not installed or not in PATH. "
            "Gondolin terminal backend requires node >= 20. "
            "Install with: apt install nodejs (or use nvm)."
        )


def _wait_for_socket(sock_path: str, timeout: float = 10.0) -> None:
    """Poll-connect until the daemon's socket accepts connections or timeout."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.close()
            return
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"gondolin daemon socket {sock_path} never came up ({last_err})"
    )


def _rpc_call(sock_path: str, request: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """One JSON-RPC roundtrip over a fresh AF_UNIX connection."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        if not buf:
            raise RuntimeError("daemon closed connection without response")
        line = bytes(buf).split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))
    finally:
        try:
            s.close()
        except OSError:
            pass


class GondolinEnvironment(BaseEnvironment):
    """Run commands inside a Gondolin microVM via a per-session Node daemon."""

    def __init__(
        self,
        sandbox_dir: str,
        cwd: str = "/workspace",
        timeout: int = 180,
        config: dict | None = None,
        *,
        stub_vm: bool = False,
        init_timeout: float = 60.0,
        daemon_path: str | None = None,
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        _ensure_node_available()

        self.sandbox_dir = Path(sandbox_dir)
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.sock_path = str(self.sandbox_dir / "gondolin.sock")
        self.config = dict(config or {})
        self.stub_vm = stub_vm

        # Default workspace mount: bind the host sandbox_dir to the in-VM cwd
        # via Gondolin's vfs.mounts. This is what makes file tools work
        # against /workspace in the guest — the same bytes appear on the host
        # under sandbox_dir, so read_file/write_file/patch can either route
        # through the VM (terminal-based) or use the host path directly.
        # User can opt out by setting `workspace_mount: False` in config; a
        # power-user policy_script that defines its own vfs may want that.
        if "workspace_mount" not in self.config:
            self.config["workspace_mount"] = {
                "guest_path": cwd,
                "host_path": str(self.sandbox_dir),
            }
        elif self.config["workspace_mount"] is False:
            # Sentinel for "opt out" — strip so the daemon doesn't see a
            # non-dict and trip its validation.
            self.config.pop("workspace_mount")

        # Captured from the init response; useful for tests and for
        # higher-level code that wants to know where the workspace lives.
        # None when workspace_mount is disabled.
        self.workspace_mount: dict | None = None

        daemon_js = Path(daemon_path) if daemon_path else _DAEMON_JS
        if not daemon_js.exists():
            raise RuntimeError(f"gondolin daemon source not found at {daemon_js}")

        env_vars = os.environ.copy()
        env_vars["GONDOLIN_DAEMON_QUIET"] = "1"
        if stub_vm:
            env_vars["GONDOLIN_DAEMON_STUB_VM"] = "1"

        # Stale socket from a previous crash would make `bind` fail; the
        # daemon also cleans up but pre-clear here to be safe on retries.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

        self._daemon_proc: subprocess.Popen | None = subprocess.Popen(
            ["node", str(daemon_js), "--socket", self.sock_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env_vars,
        )

        try:
            _wait_for_socket(self.sock_path, timeout=init_timeout)
            response = _rpc_call(
                self.sock_path,
                {"id": 1, "method": "init", "params": {"config": self.config}},
                timeout=init_timeout,
            )
            if response.get("error") is not None:
                err = response["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"gondolin daemon init failed: {msg}")
            result = response.get("result") or {}
            wm = result.get("workspaceMount")
            if isinstance(wm, dict):
                self.workspace_mount = wm
        except Exception:
            self._terminate_daemon()
            raise

    # ------------------------------------------------------------------
    # BaseEnvironment hooks
    # ------------------------------------------------------------------

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> subprocess.Popen:
        """Spawn the gondolin_rpc_call wrapper subprocess.

        ``login`` is accepted for BaseEnvironment compatibility but has no
        effect: Gondolin's VM helper runs each command via ``bash -c`` with
        no profile to source. The session-snapshot machinery still works —
        it captures env vars from the first call and re-sources them on
        subsequent ones — but ``bash -l`` semantics aren't available inside
        the VM.
        """
        argv = [
            sys.executable,
            "-m",
            "tools.environments.gondolin_rpc_call",
            self.sock_path,
            cmd_string,
            "--timeout-ms",
            str(timeout * 1000),
        ]
        return _popen_bash(argv, stdin_data)

    def cleanup(self) -> None:
        """Send shutdown RPC, wait for daemon exit, kill if it hangs."""
        if not self._daemon_proc:
            return

        # Best-effort graceful shutdown via RPC. Time-limited so a wedged
        # daemon doesn't block the calling session forever.
        try:
            _rpc_call(
                self.sock_path,
                {"id": 9999, "method": "shutdown", "params": {}},
                timeout=5.0,
            )
        except (OSError, RuntimeError) as exc:
            logger.debug("gondolin shutdown rpc failed (will SIGTERM): %s", exc)

        self._terminate_daemon()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _terminate_daemon(self) -> None:
        proc = self._daemon_proc
        if proc is None:
            return
        self._daemon_proc = None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except OSError as exc:
            logger.debug("gondolin daemon teardown error: %s", exc)
        # Clean up any leftover socket file (daemon's own SIGTERM
        # handler also tries, but races with our terminate()).
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
