"""Popen-shaped bridge from Hermes BaseEnvironment to the gondolin-host daemon.

BaseEnvironment._run_bash returns a subprocess.Popen-ish handle that the
agent's tool dispatch drains for stdout/stderr/returncode. This module is
that subprocess: per terminal call, GondolinEnvironment spawns

    python -m gondolin_rpc_call <socket-path> <command>

which connects to the per-session daemon's AF_UNIX socket, sends one
``exec`` JSON-RPC request, writes the daemon's stdout/stderr to its own,
and exits with the in-VM command's exit code.

Per-call invocation is intentional. The wrapper is stateless: daemon
death surfaces as a clean tool error on the next call without poisoning
GondolinEnvironment's in-memory state. BaseEnvironment's existing
timeout/SIGTERM machinery applies to this subprocess unchanged.

A ``policy_denied: true`` flag on the RPC result is surfaced to stderr as
a ``GONDOLIN_POLICY_DENIED`` sentinel line so the env wrapper can mark
the tool result as a policy denial (vs. a network failure).

Wire format: one JSON object per line, both directions. Matches
``tools/environments/gondolin_host/src/rpc.mjs``.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any


# Sentinel emitted to stderr when the daemon flags a policy denial.
# GondolinEnvironment greps for this string in tool-result post-processing.
POLICY_DENIED_SENTINEL = "GONDOLIN_POLICY_DENIED"


def _recv_line(sock: socket.socket, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Read until the first ``\\n`` or until the daemon closes the socket.

    Hard cap of 64 MB prevents a runaway daemon from blowing host memory.
    The cap is generous — typical agent terminal output is well under 1 MB,
    and the daemon already enforces its own per-exec output limits.
    """
    buf = bytearray()
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise RuntimeError(
                f"gondolin daemon response exceeded {max_bytes} bytes without a newline"
            )
    return bytes(buf).split(b"\n", 1)[0]


def _connect_with_diagnostic(sock_path: str) -> socket.socket:
    """Open the AF_UNIX socket. Translate ENOENT/ECONNREFUSED into a
    diagnostic that names the daemon and the path, so a stale or
    never-started daemon doesn't surface as a bare OSError."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
    except FileNotFoundError as exc:
        sock.close()
        raise SystemExit(
            f"gondolin: socket not found at {sock_path}; daemon may not be running ({exc})"
        )
    except ConnectionRefusedError as exc:
        sock.close()
        raise SystemExit(
            f"gondolin: connection refused to {sock_path}; daemon may have crashed ({exc})"
        )
    return sock


def _send_request(sock: socket.socket, request: dict[str, Any]) -> dict[str, Any]:
    """Send a single JSON-RPC request, read a single JSON-RPC response."""
    sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
    raw = _recv_line(sock)
    if not raw:
        raise SystemExit("gondolin: daemon closed connection without responding")
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"gondolin: malformed daemon response: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gondolin_rpc_call",
        description="Single-shot RPC bridge to the gondolin-host daemon.",
    )
    parser.add_argument("socket", help="Path to the daemon's AF_UNIX socket.")
    parser.add_argument("cmd", help="Shell command to run inside the VM.")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        help="Per-exec timeout in milliseconds (default: daemon-side default).",
    )
    args = parser.parse_args(argv)

    params: dict[str, Any] = {"cmd": args.cmd}
    if args.timeout_ms is not None:
        params["timeout_ms"] = args.timeout_ms
    request = {"id": 1, "method": "exec", "params": params}

    with _connect_with_diagnostic(args.socket) as sock:
        response = _send_request(sock, request)

    if "error" in response and response["error"] is not None:
        err = response["error"]
        msg = err.get("message", str(err))
        sys.stderr.write(f"gondolin: rpc error: {msg}\n")
        return 1

    result = response.get("result") or {}
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = int(result.get("exit_code", 1))
    policy_denied = bool(result.get("policy_denied", False))

    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
    if policy_denied:
        # Sentinel goes after the VM stderr so the original message is
        # preserved verbatim; GondolinEnvironment matches on the marker.
        sys.stderr.write(f"\n{POLICY_DENIED_SENTINEL}\n")
    sys.stderr.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
