// Apples-to-apples node-side comparison of JSON+base64 vs msgpack
// for the gondolin streaming protocol.
//
// Run: node bench_protocol_compare.mjs
//
// Mirrors bench_protocol_compare.py on the Python side: feeds the same
// kind of chunk corpora through both encoders and reports wire size +
// encode time. (Decode-on-daemon isn't a real hot path — the daemon
// only encodes — but we time it anyway for completeness.)

import { performance } from "node:perf_hooks";
import { Buffer } from "node:buffer";
import * as msgpack from "@msgpack/msgpack";

// ---- Protocol A: JSON + base64 + newline framing ----

function encodeJsonB64(chunks) {
  const out = [];
  for (const chunk of chunks) {
    const frame = {
      id: 1,
      stream: { kind: "stdout", b64: chunk.toString("base64") },
    };
    out.push(Buffer.from(JSON.stringify(frame) + "\n", "utf8"));
  }
  out.push(Buffer.from(JSON.stringify({ id: 1, result: { exit_code: 0 } }) + "\n", "utf8"));
  return Buffer.concat(out);
}

function decodeJsonB64(wire) {
  const decoded = [];
  let buf = wire;
  let exitCode = -1;
  while (buf.length > 0) {
    const nl = buf.indexOf(0x0a);
    if (nl < 0) break;
    const line = buf.slice(0, nl).toString("utf8");
    buf = buf.slice(nl + 1);
    const frame = JSON.parse(line);
    if (frame.stream) {
      decoded.push(Buffer.from(frame.stream.b64, "base64"));
    } else if (frame.result) {
      exitCode = frame.result.exit_code | 0;
      break;
    }
  }
  return { decoded, exitCode };
}

// ---- Protocol B: msgpack with u32 BE length prefix ----

function encodeMsgpack(chunks) {
  const out = [];
  for (const chunk of chunks) {
    const frame = { id: 1, stream: { kind: "stdout", data: chunk } };
    const packed = msgpack.encode(frame);  // Uint8Array
    const len = Buffer.alloc(4);
    len.writeUInt32BE(packed.length, 0);
    out.push(len);
    out.push(Buffer.from(packed.buffer, packed.byteOffset, packed.byteLength));
  }
  const final = msgpack.encode({ id: 1, result: { exit_code: 0 } });
  const len = Buffer.alloc(4);
  len.writeUInt32BE(final.length, 0);
  out.push(len);
  out.push(Buffer.from(final.buffer, final.byteOffset, final.byteLength));
  return Buffer.concat(out);
}

function decodeMsgpack(wire) {
  const decoded = [];
  let pos = 0;
  let exitCode = -1;
  while (pos < wire.length) {
    const n = wire.readUInt32BE(pos);
    pos += 4;
    const frame = msgpack.decode(wire.slice(pos, pos + n));
    pos += n;
    if (frame.stream) {
      decoded.push(Buffer.from(frame.stream.data));
    } else if (frame.result) {
      exitCode = frame.result.exit_code | 0;
      break;
    }
  }
  return { decoded, exitCode };
}

// ---- Corpora ----

function randBytes(n) {
  const b = Buffer.allocUnsafe(n);
  for (let i = 0; i < n; i++) b[i] = (Math.random() * 256) | 0;
  return b;
}

function splitInto(buf, chunkSize) {
  const chunks = [];
  for (let i = 0; i < buf.length; i += chunkSize) chunks.push(buf.slice(i, i + chunkSize));
  return chunks;
}

function genBinary(total, chunkSize = 15_000) {
  return splitInto(randBytes(total), chunkSize);
}

function genTextYes(total, chunkSize = 8192) {
  const s = "y\n".repeat(Math.floor(total / 2));
  return splitInto(Buffer.from(s.slice(0, total), "utf8"), chunkSize);
}

function genLineOriented(lineCount = 1000) {
  const parts = [];
  for (let i = 1; i <= lineCount; i++) parts.push(`line-${i}\n`);
  const payload = Buffer.from(parts.join(""), "utf8");
  const chunkSize = Math.max(1, Math.floor(payload.length / 149));
  return splitInto(payload, chunkSize);
}

function genSmallEcho() {
  return [Buffer.from("hello-world\n", "utf8")];
}

// ---- Driver ----

function median(arr) {
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

function bench(name, chunks, iters = 5) {
  const payloadBytes = chunks.reduce((s, c) => s + c.length, 0);
  console.log(`\n=== ${name} ===  (payload=${payloadBytes.toLocaleString()} B in ${chunks.length} chunks)`);
  console.log(
    `${"protocol".padEnd(14)} ${"wire B".padStart(12)} ${"wire/pl".padStart(8)} ${"enc_ms".padStart(8)} ${"dec_ms".padStart(8)} ${"total_ms".padStart(9)}`
  );

  for (const [label, enc, dec] of [
    ["json+b64", encodeJsonB64, decodeJsonB64],
    ["msgpack", encodeMsgpack, decodeMsgpack],
  ]) {
    const encs = [];
    const decs = [];
    let wireLen = 0;
    for (let i = 0; i < iters; i++) {
      const t0 = performance.now();
      const wire = enc(chunks);
      const t1 = performance.now();
      const { decoded } = dec(wire);
      const t2 = performance.now();
      encs.push(t1 - t0);
      decs.push(t2 - t1);
      wireLen = wire.length;
      // Correctness
      const joined = Buffer.concat(decoded);
      const joinedIn = Buffer.concat(chunks);
      if (!joined.equals(joinedIn)) {
        console.log(`  !! ${label} CORRUPTED ROUNDTRIP: got ${joined.length}B vs ${joinedIn.length}B`);
      }
    }
    const ratio = payloadBytes > 0 ? (wireLen / payloadBytes).toFixed(3) : "inf";
    const encMed = median(encs).toFixed(2);
    const decMed = median(decs).toFixed(2);
    const totMed = (Number(encMed) + Number(decMed)).toFixed(2);
    console.log(
      `${label.padEnd(14)} ${wireLen.toLocaleString().padStart(12)} ${String(ratio).padStart(8)} ${encMed.padStart(8)} ${decMed.padStart(8)} ${totMed.padStart(9)}`
    );
  }
}

bench("01 small text echo", genSmallEcho(), 500);
bench("02 256KB binary", genBinary(256 * 1024), 30);
bench("03 4MB binary", genBinary(4 * 1024 * 1024), 10);
bench("04 1MB text (yes)", genTextYes(1_000_000), 20);
bench("05 64KB line-oriented", genLineOriented(1000), 500);
