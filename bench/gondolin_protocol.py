#!/usr/bin/env python3
"""Bench: measure Gondolin streaming protocol characteristics for the
JSON-RPC-vs-binary-framing decision.

For each workload:
  - bytes returned via stream
  - chunk count
  - p50/p99 chunk size
  - per-chunk wrapper-side overhead (json.loads + sys.stdout.write)
  - whether binary output survives the round trip

Workloads:
  1. echo small text (baseline)
  2. cat 256 KB random binary (the "oops" case)
  3. cat 4 MB random binary (the "really oops" case)
  4. yes | head -c 1M  (a pathological-ish text stream)

Requires a working Gondolin KVM env. Run from the repo root.
"""
from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

if not os.path.exists("/dev/kvm"):
    print("SKIP: /dev/kvm not present", file=sys.stderr)
    sys.exit(0)

from tools.environments.gondolin import GondolinEnvironment


def _send_recv_stream(sock_path: str, cmd: str) -> dict:
    """Drive the daemon directly with exec_stream and capture wire stats.

    Returns:
      total_bytes_wire     : bytes read from socket (raw frame bytes)
      total_bytes_payload  : sum of decoded stdout/stderr lengths
      chunks               : count of stream frames
      chunk_sizes_payload  : list of payload sizes per chunk (bytes)
      wall_ms              : end-to-end wall clock
      json_decode_us_total : total μs spent in json.loads
      exit_code            : final exit code
      stdout_bytes_first16 : first 16 raw bytes of reassembled stdout (for binary check)
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    req = json.dumps({"id": 1, "method": "exec_stream", "params": {"cmd": cmd, "timeout_ms": 300_000}}) + "\n"
    s.sendall(req.encode("utf-8"))

    total_wire = 0
    total_payload = 0
    sizes = []
    decode_us = 0.0
    stdout_assembled = bytearray()
    exit_code = -1
    buf = bytearray()
    t0 = time.perf_counter()
    while True:
        # Read until next newline
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                buf += b""
                break
            total_wire += len(chunk)
            buf.extend(chunk)
        if b"\n" not in buf:
            break
        line, _, rest = bytes(buf).partition(b"\n")
        buf = bytearray(rest)
        td0 = time.perf_counter()
        frame = json.loads(line.decode("utf-8"))
        decode_us += (time.perf_counter() - td0) * 1e6
        if "stream" in frame:
            data = (frame["stream"] or {}).get("data", "")
            data_bytes = data.encode("utf-8", errors="replace")
            sizes.append(len(data_bytes))
            total_payload += len(data_bytes)
            if (frame["stream"] or {}).get("kind", "stdout") == "stdout":
                stdout_assembled.extend(data_bytes)
        else:
            result = frame.get("result") or {}
            exit_code = int(result.get("exit_code", -1))
            break
    wall_ms = (time.perf_counter() - t0) * 1000
    s.close()

    return {
        "total_bytes_wire": total_wire,
        "total_bytes_payload": total_payload,
        "chunks": len(sizes),
        "chunk_sizes_payload": sizes,
        "wall_ms": wall_ms,
        "json_decode_us_total": decode_us,
        "exit_code": exit_code,
        "stdout_first16": bytes(stdout_assembled[:16]),
    }


def _summary(name: str, stats: dict, expected_first_bytes: bytes | None = None):
    sizes = stats["chunk_sizes_payload"] or [0]
    print(f"=== {name} ===")
    print(f"  exit_code           : {stats['exit_code']}")
    print(f"  wall_ms             : {stats['wall_ms']:.1f}")
    print(f"  chunks              : {stats['chunks']}")
    print(f"  bytes wire (incl frame overhead) : {stats['total_bytes_wire']:>10}")
    print(f"  bytes payload (decoded utf-8)    : {stats['total_bytes_payload']:>10}")
    if stats["total_bytes_payload"] > 0:
        ratio = stats["total_bytes_wire"] / stats["total_bytes_payload"]
        print(f"  wire/payload ratio  : {ratio:.3f}x   (1.0 = perfect, >1 = framing overhead)")
    sorted_sizes = sorted(sizes)
    p50 = sorted_sizes[len(sorted_sizes)//2]
    p99 = sorted_sizes[min(len(sorted_sizes)-1, int(len(sorted_sizes)*0.99))]
    mean = statistics.mean(sizes)
    print(f"  chunk size p50/p99/mean : {p50}/{p99}/{mean:.0f} bytes")
    print(f"  json.loads total μs : {stats['json_decode_us_total']:.0f}")
    if stats["chunks"]:
        print(f"  json.loads per chunk μs : {stats['json_decode_us_total']/stats['chunks']:.1f}")
    if expected_first_bytes is not None:
        match = stats["stdout_first16"][:len(expected_first_bytes)] == expected_first_bytes
        print(f"  binary roundtrip OK : {match}  (got {stats['stdout_first16'][:len(expected_first_bytes)]!r} vs expected {expected_first_bytes!r})")
    print()


def main():
    with tempfile.TemporaryDirectory() as td:
        # Prepare binary fixtures host-side, then push into the sandbox via
        # the bind-mount so the in-VM `cat` works on real bytes.
        sandbox = Path(td) / "sandbox"
        sandbox.mkdir()
        workspace = sandbox / "workspace"
        workspace.mkdir()
        # Build deterministic binaries with known first bytes (JPEG SOI + random tail)
        jpeg_soi = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01"
        small_bin = jpeg_soi + os.urandom(256 * 1024 - len(jpeg_soi))
        big_bin = jpeg_soi + os.urandom(4 * 1024 * 1024 - len(jpeg_soi))
        (workspace / "small.bin").write_bytes(small_bin)
        (workspace / "big.bin").write_bytes(big_bin)

        env = GondolinEnvironment(
            sandbox_dir=str(sandbox),
            cwd="/workspace",
            timeout=300,
            init_timeout=180.0,
        )
        try:
            sock_path = env.sock_path  # type: ignore[attr-defined]
            print(f"socket: {sock_path}")

            # Warm — make sure bash is alive in the VM
            warm = _send_recv_stream(sock_path, "true")
            print(f"warm exit={warm['exit_code']}, wall_ms={warm['wall_ms']:.1f}\n")

            _summary("01 echo small text",
                     _send_recv_stream(sock_path, "echo hello-world"))
            _summary("02 cat 256KB binary",
                     _send_recv_stream(sock_path, "cat /workspace/small.bin"),
                     expected_first_bytes=jpeg_soi[:8])
            _summary("03 cat 4MB binary",
                     _send_recv_stream(sock_path, "cat /workspace/big.bin"),
                     expected_first_bytes=jpeg_soi[:8])
            _summary("04 1MB text stream (yes|head)",
                     _send_recv_stream(sock_path, "yes | head -c 1000000"))
            _summary("05 line-oriented 64KB",
                     _send_recv_stream(sock_path, "for i in $(seq 1 1000); do echo line-$i; done"))
        finally:
            env.cleanup()


if __name__ == "__main__":
    main()
