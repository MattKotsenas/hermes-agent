#!/usr/bin/env python3
"""Apples-to-apples comparison of two streaming protocols for Gondolin:

  JSON+base64 : {"id": N, "stream": {"kind": "stdout", "b64": "<base64>"}}
  msgpack-rpc : msgpack-packed equivalent with `data` as msgpack bin (raw bytes)

Strategy:
  - Generate representative chunk corpora (binary 256KB/4MB, text 1MB, line-oriented 64KB).
  - For each corpus, simulate the daemon→wrapper pipeline:
      ENCODE side  : take chunk bytes, wrap in protocol envelope, serialize, framing.
      DECODE side  : read frame, deserialize, extract bytes payload.
  - Time encode+decode end-to-end. Repeat N iterations for stability.

We measure both implementations on the SAME chunk corpora, including raw
binary bytes (no UTF-8 corruption, because we're simulating the
post-fix daemon). The point is to isolate the protocol cost from the
Gondolin-side bug.
"""
from __future__ import annotations

import base64
import io
import json
import os
import statistics
import struct
import sys
import time

try:
    import msgpack  # type: ignore
except ImportError:
    sys.exit("FATAL: msgpack not installed")


# -------- Protocol A: JSON + base64 with newline framing --------

def encode_json_b64(chunks: list[bytes]) -> bytes:
    """Daemon side: turn a list of chunk bytes into a stream of newline-delimited JSON frames."""
    out = io.BytesIO()
    for i, chunk in enumerate(chunks):
        frame = {
            "id": 1,
            "stream": {
                "kind": "stdout",
                "b64": base64.b64encode(chunk).decode("ascii"),
            },
        }
        out.write(json.dumps(frame).encode("utf-8"))
        out.write(b"\n")
    # Terminal result frame
    out.write(json.dumps({"id": 1, "result": {"exit_code": 0}}).encode("utf-8"))
    out.write(b"\n")
    return out.getvalue()


def decode_json_b64(wire: bytes) -> tuple[list[bytes], int]:
    """Wrapper side: parse newline-delimited JSON frames, decode b64 data."""
    decoded: list[bytes] = []
    buf = bytearray(wire)
    exit_code = -1
    while buf:
        nl = buf.find(b"\n")
        if nl < 0:
            break
        line = bytes(buf[:nl])
        del buf[:nl + 1]
        frame = json.loads(line.decode("utf-8"))
        if "stream" in frame:
            decoded.append(base64.b64decode(frame["stream"]["b64"]))
        elif "result" in frame:
            exit_code = int(frame["result"].get("exit_code", -1))
            break
    return decoded, exit_code


# -------- Protocol B: msgpack with u32-length prefix framing --------

def encode_msgpack(chunks: list[bytes]) -> bytes:
    """Daemon side: turn chunks into length-prefixed msgpack frames.
    Frame: [u32 BE length][msgpack payload]
    """
    out = io.BytesIO()
    for chunk in chunks:
        frame = {
            "id": 1,
            "stream": {"kind": "stdout", "data": chunk},  # data is bytes -> msgpack bin
        }
        packed = msgpack.packb(frame, use_bin_type=True)
        out.write(struct.pack(">I", len(packed)))
        out.write(packed)
    final = msgpack.packb({"id": 1, "result": {"exit_code": 0}}, use_bin_type=True)
    out.write(struct.pack(">I", len(final)))
    out.write(final)
    return out.getvalue()


def decode_msgpack(wire: bytes) -> tuple[list[bytes], int]:
    decoded: list[bytes] = []
    pos = 0
    exit_code = -1
    while pos < len(wire):
        (n,) = struct.unpack_from(">I", wire, pos)
        pos += 4
        frame = msgpack.unpackb(wire[pos:pos + n], raw=False)
        pos += n
        if "stream" in frame:
            decoded.append(frame["stream"]["data"])  # already bytes
        elif "result" in frame:
            exit_code = int(frame["result"].get("exit_code", -1))
            break
    return decoded, exit_code


# -------- Corpora --------

def gen_binary(total: int, chunk_size: int = 15_000) -> list[bytes]:
    # Gondolin emits ~15KB chunks per the earlier bench. Replicate that.
    blob = os.urandom(total)
    return [blob[i:i + chunk_size] for i in range(0, len(blob), chunk_size)]


def gen_text_yes(total: int, chunk_size: int = 8_192) -> list[bytes]:
    # Pattern of `yes | head -c N` is "y\n" * (N/2). Realistic 8KB chunks.
    payload = (b"y\n" * (total // 2))[:total]
    return [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]


def gen_line_oriented(line_count: int = 1000) -> list[bytes]:
    # 1000 lines of "line-N\n" — average ~9 bytes; Gondolin chunks aggressively here.
    # From the real bench: 149 chunks for 1000 lines, mean ~60B per chunk.
    payload = b"".join(f"line-{i}\n".encode() for i in range(1, line_count + 1))
    chunk_size = max(1, len(payload) // 149)
    return [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]


def gen_small_echo() -> list[bytes]:
    return [b"hello-world\n"]


# -------- Driver --------

def bench(name: str, chunks: list[bytes], iters: int = 5) -> None:
    payload_bytes = sum(len(c) for c in chunks)
    n_chunks = len(chunks)
    print(f"\n=== {name} ===  (payload={payload_bytes:,} B in {n_chunks} chunks)")
    print(f"{'protocol':<14} {'wire B':>12} {'wire/pl':>8} {'enc_ms':>8} {'dec_ms':>8} {'total_ms':>9}")

    for label, enc, dec in [
        ("json+b64", encode_json_b64, decode_json_b64),
        ("msgpack", encode_msgpack, decode_msgpack),
    ]:
        encs = []
        decs = []
        wire_len = None
        for _ in range(iters):
            t0 = time.perf_counter()
            wire = enc(chunks)
            t1 = time.perf_counter()
            recovered, ec = dec(wire)
            t2 = time.perf_counter()
            encs.append((t1 - t0) * 1000)
            decs.append((t2 - t1) * 1000)
            wire_len = len(wire)
            # Correctness check
            joined = b"".join(recovered)
            joined_in = b"".join(chunks)
            if joined != joined_in:
                print(f"  !! {label} CORRUPTED ROUNDTRIP: got {len(joined)}B vs {len(joined_in)}B")
        enc_med = statistics.median(encs)
        dec_med = statistics.median(decs)
        ratio = wire_len / payload_bytes if payload_bytes else float("inf")
        print(f"{label:<14} {wire_len:>12,} {ratio:>8.3f} {enc_med:>8.2f} {dec_med:>8.2f} {enc_med+dec_med:>9.2f}")


def main():
    print(f"msgpack version: {msgpack.version}")
    print(f"python: {sys.version.split()[0]}")
    bench("01 small text echo", gen_small_echo(), iters=200)
    bench("02 256KB binary",   gen_binary(256 * 1024), iters=30)
    bench("03 4MB binary",     gen_binary(4 * 1024 * 1024), iters=10)
    bench("04 1MB text (yes)", gen_text_yes(1_000_000), iters=20)
    bench("05 64KB line-oriented", gen_line_oriented(1000), iters=200)


if __name__ == "__main__":
    main()
