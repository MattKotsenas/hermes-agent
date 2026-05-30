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

Wire format: length-prefixed msgpack frames (u32 BE length + payload),
both directions. Matches ``tools/environments/gondolin_host/src/rpc.mjs``.
Switched from line-delimited JSON in May 2026 (a) so stream chunks carry
raw bytes through msgpack bin8/bin32 instead of being coerced via
``String(chunk)`` on the daemon side and (b) for the ~5-7x encode/decode
speedup on multi-megabyte payloads (see bench/protocol_compare.py).
"""

from __future__ import annotations

import argparse
import socket
import sys
from typing import Any

import msgpack


# Sentinel emitted to stderr when the daemon flags a policy denial.
# GondolinEnvironment greps for this string in tool-result post-processing.
POLICY_DENIED_SENTINEL = "GONDOLIN_POLICY_DENIED"

# Hard cap on the size of any single frame payload (must match the daemon
# side). Defence-in-depth against a hostile / buggy peer claiming a huge
# frame length and making us allocate gigabytes. The daemon enforces the
# same cap, so a legitimate peer never trips this.
_MAX_FRAME_BYTES = 64 * 1024 * 1024


def _encode_frame(obj: dict[str, Any]) -> bytes:
    """Encode a single request as a length-prefixed msgpack frame."""
    payload: bytes = msgpack.packb(obj, use_bin_type=True)  # type: ignore[assignment]
    return len(payload).to_bytes(4, "big") + payload


def _read_frame(buf: bytearray, sock: socket.socket) -> tuple[Any, bytearray]:
    """Read one full frame off ``sock`` (consuming bytes from ``buf`` first).

    Returns ``(decoded_object, remaining_bytes)``. Raises ``SystemExit`` on
    EOF before a frame is complete, or on a frame size that exceeds the
    cap.
    """
    while True:
        if len(buf) >= 4:
            n = int.from_bytes(buf[:4], "big")
            if n > _MAX_FRAME_BYTES:
                raise SystemExit(
                    f"gondolin: daemon claimed a frame of {n} bytes "
                    f"(cap {_MAX_FRAME_BYTES}); refusing to read"
                )
            if len(buf) >= 4 + n:
                payload = bytes(buf[4:4 + n])
                rest = bytearray(buf[4 + n:])
                return msgpack.unpackb(payload, raw=False), rest
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise SystemExit(
                f"gondolin: daemon went silent for "
                f"{sock.gettimeout():.0f}s mid-frame; aborting "
                f"(daemon may be wedged or vm.exec hung)"
            )
        if not chunk:
            raise SystemExit("gondolin: daemon closed connection mid-frame")
        buf.extend(chunk)


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
    sock.sendall(_encode_frame(request))
    buf = bytearray()
    frame, _rest = _read_frame(buf, sock)
    if not isinstance(frame, dict):
        raise SystemExit(f"gondolin: daemon returned non-dict frame: {type(frame).__name__}")
    return frame


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
    parser.add_argument(
        "--stream",
        action="store_true",
        help=(
            "Use exec_stream: write stdout/stderr chunks to our pipes as they "
            "arrive from the daemon instead of buffering the whole result. "
            "Required for long-running commands whose output the user (or "
            "BaseEnvironment) wants to see live."
        ),
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Run the cmd under a login shell (bash -l -c) inside the VM. "
            "BaseEnvironment passes login=True only during init_session "
            "(snapshot capture) so /etc/profile + profile.d fire exactly "
            "once per session; every steady-state exec should omit this "
            "flag. The daemon turns login into an argv-form invocation "
            "(bash -l -c <cmd>) so the SDK does not wrap us in its own "
            "/bin/sh -lc shell."
        ),
    )
    args = parser.parse_args(argv)

    params: dict[str, Any] = {"cmd": args.cmd}
    if args.timeout_ms is not None:
        params["timeout_ms"] = args.timeout_ms
    if args.login:
        # Absent/false on the wire means non-login; true means
        # ``bash -l -c``. We omit the key when false to keep the wire
        # payload minimal rather than as a future-compat statement.
        params["login"] = True

    # Forward our own stdin to the daemon as msgpack `bin` in
    # ``params.stdin``. BaseEnvironment._pipe_stdin pipes the caller's
    # ``stdin_data`` into our stdin and closes the pipe, so a bounded
    # read here drains everything and returns on EOF. Without this
    # forwarding, ShellFileOperations.write_file (which does
    # ``cat > path`` with the file content on stdin) silently writes
    # 0-byte files because the daemon never sees the bytes. We skip
    # the read when stdin is a TTY so an accidental interactive
    # invocation doesn't block waiting for the user to type EOF.
    stdin_bytes = b""
    if not sys.stdin.isatty():
        try:
            stdin_bytes = sys.stdin.buffer.read(_MAX_FRAME_BYTES + 1)
        except (OSError, ValueError):
            # Closed stdin / no buffer attribute (rare in CPython, but
            # defensible) -> just send no stdin field.
            stdin_bytes = b""
    if len(stdin_bytes) > _MAX_FRAME_BYTES:
        sys.stderr.write(
            f"gondolin: stdin payload exceeds {_MAX_FRAME_BYTES} bytes; "
            f"refusing to forward (write a smaller chunk or stream it)\n"
        )
        return 1
    if stdin_bytes:
        params["stdin"] = stdin_bytes

    method = "exec_stream" if args.stream else "exec"
    request = {"id": 1, "method": method, "params": params}

    with _connect_with_diagnostic(args.socket) as sock:
        # Bound inter-frame silence so a stuck daemon (wedged vm.exec,
        # crashed handler mid-stream) surfaces a clean error instead of
        # blocking forever. BaseEnvironment's wall-clock SIGTERM would
        # eventually rescue us, but its diagnostic is generic; ours
        # names the socket and the timeout. Generous default — much
        # larger than any realistic per-chunk latency — plus the user's
        # own --timeout-ms if set, with a grace overhead.
        per_call_timeout = (
            (args.timeout_ms / 1000.0 + 30.0)
            if args.timeout_ms is not None
            else 600.0
        )
        sock.settimeout(per_call_timeout)
        if args.stream:
            return _run_streaming(sock, request)
        response = _send_request(sock, request)

    if "error" in response and response["error"] is not None:
        err = response["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        sys.stderr.write(f"gondolin: rpc error: {msg}\n")
        return 1

    result = response.get("result") or {}
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = int(result.get("exit_code", 1))
    policy_denied = bool(result.get("policy_denied", False))

    if stdout:
        _write_text(sys.stdout, stdout)
        sys.stdout.flush()
    if stderr:
        _write_text(sys.stderr, stderr)
    if policy_denied:
        # Sentinel goes after the VM stderr so the original message is
        # preserved verbatim; GondolinEnvironment matches on the marker.
        sys.stderr.write(f"\n{POLICY_DENIED_SENTINEL}\n")
    sys.stderr.flush()
    return exit_code


def _write_text(stream, data: Any) -> None:
    """Write a value coming off the wire to a text stream.

    msgpack's ``raw=False`` decode hands us ``str`` for msgpack ``str``
    frames and ``bytes`` for msgpack ``bin`` frames. Daemon's non-stream
    exec path returns Gondolin's already-string stdout/stderr, so this is
    typically a no-op string write — but we handle bytes too in case the
    daemon's protocol layer changes.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        # Tagged 'binary' on the wire — write the raw bytes to the
        # underlying buffer to preserve byte-for-byte content. Falls back
        # to the text write path for streams without .buffer (e.g. tests).
        try:
            stream.buffer.write(bytes(data))
            return
        except AttributeError:
            stream.write(bytes(data).decode("utf-8", errors="replace"))
            return
    stream.write(data)


def _run_streaming(sock: socket.socket, request: dict[str, Any]) -> int:
    """Send ``exec_stream`` and pump frames live to our stdout/stderr.

    Frame shapes (each frame is a length-prefixed msgpack object):
      ``{id, stream: {kind: "stdout"|"stderr", data}}``
      ``{id, result: {exit_code: N, ...}}``   ← final
      ``{id, error: {...}}``                  ← final on error

    Each stream frame is written to its respective pipe immediately so
    BaseEnvironment's select() drain can hand chunks to the agent UI as
    the command runs, rather than waiting for the whole exec to finish.
    """
    sock.sendall(_encode_frame(request))
    buf = bytearray()
    while True:
        try:
            frame, buf = _read_frame(buf, sock)
        except SystemExit as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

        if not isinstance(frame, dict):
            sys.stderr.write(
                f"gondolin: daemon sent non-dict frame: {type(frame).__name__}\n"
            )
            return 1

        if "stream" in frame:
            payload = frame["stream"] or {}
            kind = payload.get("kind", "stdout")
            data = payload.get("data", b"")
            stream = sys.stderr if kind == "stderr" else sys.stdout
            _write_text(stream, data)
            stream.flush()
            continue

        # Final frame.
        if frame.get("error") is not None:
            err = frame["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            sys.stderr.write(f"gondolin: rpc error: {msg}\n")
            sys.stderr.flush()
            return 1

        result = frame.get("result") or {}
        exit_code = int(result.get("exit_code", 1))
        policy_denied = bool(result.get("policy_denied", False))
        if policy_denied:
            sys.stderr.write(f"\n{POLICY_DENIED_SENTINEL}\n")
            sys.stderr.flush()
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
