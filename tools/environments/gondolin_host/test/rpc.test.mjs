// Test: length-prefixed msgpack JSON-RPC framing.
//
// The daemon reads requests (one frame = u32 BE length + msgpack payload)
// from a stream and writes responses in the same shape to another stream.
// It must:
//   1. Pair requests and responses by id.
//   2. Handle multiple requests in flight (though MVP is serial).
//   3. Report unknown method as a JSON-RPC error response.
//   4. Reject malformed payloads with a parse-error response (id=null).
//
// We don't boot a VM here — we inject fake method handlers so framing
// is testable in isolation.

import { test } from "node:test";
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { PassThrough } from "node:stream";
import { encode, decode } from "@msgpack/msgpack";
import { runRpcServer } from "../src/rpc.mjs";

function writeReq(stream, obj) {
  const payload = encode(obj);
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  stream.write(header);
  stream.write(Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength));
}

// Read exactly N framed msgpack responses from a stream.
async function readNResponses(stream, n) {
  return new Promise((resolve, reject) => {
    const out = [];
    let buf = Buffer.alloc(0);
    stream.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      while (true) {
        if (buf.length < 4) break;
        const len = buf.readUInt32BE(0);
        if (buf.length < 4 + len) break;
        const payload = buf.subarray(4, 4 + len);
        buf = buf.subarray(4 + len);
        try {
          out.push(decode(payload));
        } catch (e) {
          reject(e);
          return;
        }
        if (out.length === n) {
          resolve(out);
          return;
        }
      }
    });
    stream.on("error", reject);
    setTimeout(() => reject(new Error(`only got ${out.length}/${n}`)), 2000);
  });
}

test("pairs a single request and response by id", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    ping: async (params) => ({ pong: params?.echo ?? null }),
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 42, method: "ping", params: { echo: "hi" } });
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, 42);
  assert.deepEqual(resp.result, { pong: "hi" });
  assert.equal(resp.error, undefined);

  inp.end();
  await serverDone;
});

test("returns method-not-found for unknown methods", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const serverDone = runRpcServer({ input: inp, output: outp, handlers: {} });

  writeReq(inp, { id: 7, method: "no_such_method", params: {} });
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, 7);
  assert.equal(resp.result, undefined);
  assert.equal(resp.error.code, -32601); // JSON-RPC reserved for method not found
  assert.match(resp.error.message, /no_such_method/);

  inp.end();
  await serverDone;
});

test("returns parse-error with id=null for malformed msgpack payload", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const serverDone = runRpcServer({ input: inp, output: outp, handlers: {} });

  // Frame a chunk of bytes that isn't valid msgpack. Use a length prefix
  // that matches the payload length so the framing parser proceeds to
  // decode, hits a decode error, and replies with -32700.
  const junk = Buffer.from("this is not msgpack", "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(junk.length, 0);
  inp.write(Buffer.concat([header, junk]));
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, null);
  assert.equal(resp.error.code, -32700); // JSON-RPC parse error

  inp.end();
  await serverDone;
});

test("returns parse-error and tears down when a frame claims more bytes than the cap", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const serverDone = runRpcServer({ input: inp, output: outp, handlers: {} });

  // Header claims 1 GiB. The cap is 64 MiB; the framer should reject
  // before allocating anything.
  const header = Buffer.alloc(4);
  header.writeUInt32BE(1024 * 1024 * 1024, 0);
  inp.write(header);
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, null);
  assert.equal(resp.error.code, -32700);
  assert.match(resp.error.message, /frame too large/);

  // The framer destroys the input on cap violation; serverDone resolves
  // or rejects depending on whether destroy() lands as 'end' or 'error'.
  // Either way we don't want this test to hang.
  await serverDone.catch(() => {});
});

test("returns internal-error if a handler throws", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    boom: async () => { throw new Error("kaboom"); },
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 1, method: "boom" });
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, 1);
  assert.equal(resp.error.code, -32603); // JSON-RPC internal error
  assert.match(resp.error.message, /kaboom/);

  inp.end();
  await serverDone;
});

test("handles multiple sequential requests in order", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    add: async ({ a, b }) => ({ sum: a + b }),
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 1, method: "add", params: { a: 1, b: 2 } });
  writeReq(inp, { id: 2, method: "add", params: { a: 10, b: 20 } });
  writeReq(inp, { id: 3, method: "add", params: { a: 0, b: 0 } });

  const responses = await readNResponses(outp, 3);
  assert.deepEqual(responses.map(r => [r.id, r.result.sum]), [[1, 3], [2, 30], [3, 0]]);

  inp.end();
  await serverDone;
});

test("framer reassembles a payload split across multiple data chunks", async () => {
  // Real TCP/AF_UNIX delivery often splits a frame across multiple
  // 'data' events. The framer's accumulating-buffer pattern is the load
  // bearing piece — this test pokes it directly.
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = { echo: async (p) => ({ got: p }) };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  const payload = encode({ id: 17, method: "echo", params: { x: "split-me" } });
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  const wire = Buffer.concat([header, Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)]);

  // Send byte by byte. Slowest possible delivery, exercises the framer
  // every single read.
  for (let i = 0; i < wire.length; i++) {
    inp.write(wire.subarray(i, i + 1));
  }

  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, 17);
  assert.deepEqual(resp.result, { got: { x: "split-me" } });

  inp.end();
  await serverDone;
});

test("framer handles two back-to-back frames glued into one chunk", async () => {
  // Inverse of the previous test: TCP coalesces and we get two frames in
  // a single 'data' event. The while-loop in the framer must drain both.
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = { echo: async (p) => ({ got: p }) };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  const pieces = [
    { id: 1, method: "echo", params: { x: "first" } },
    { id: 2, method: "echo", params: { x: "second" } },
  ].map((req) => {
    const payload = encode(req);
    const header = Buffer.alloc(4);
    header.writeUInt32BE(payload.length, 0);
    return Buffer.concat([header, Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)]);
  });

  inp.write(Buffer.concat(pieces));

  const responses = await readNResponses(outp, 2);
  assert.deepEqual(responses.map((r) => [r.id, r.result.got.x]), [[1, "first"], [2, "second"]]);

  inp.end();
  await serverDone;
});

test("carries binary payloads verbatim through the framer (no UTF-8 corruption)", async () => {
  // Bytes 0x80-0xFF are not valid lone UTF-8 sequences. JSON+UTF-8 would
  // either reject or replace them with U+FFFD; msgpack bin8 carries them
  // unmodified. This test is the contract that stream chunks from
  // vm.exec (test fixtures, compiled output, etc.) round-trip cleanly.
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    bounce: async (params) => ({ data: params.data }),
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  const bin = Buffer.from([0xff, 0xfe, 0xfd, 0x00, 0x80, 0xc0, 0x01, 0x02]);
  writeReq(inp, { id: 1, method: "bounce", params: { data: bin } });

  const [resp] = await readNResponses(outp, 1);
  // msgpack decodes bin payloads to Uint8Array on the decode side.
  const got = resp.result.data;
  assert.ok(got instanceof Uint8Array, `got ${got?.constructor?.name}`);
  assert.deepEqual(Buffer.from(got), bin);

  inp.end();
  await serverDone;
});

// ---- streamWriter: intermediate frames before the final response -----------
//
// For streaming exec, the handler needs to push stdout chunks as they arrive
// from the VM rather than buffering everything until the command exits.
// We extend the RPC contract: handlers receive a `streamWriter` second arg.
// Calling streamWriter(obj) emits `{ id, stream: obj }` frames. The handler's
// eventual return value becomes the final `{ id, result }` frame as before.
// Handlers that don't use streamWriter behave exactly like today (back-compat).

test("streamWriter emits intermediate {stream} frames tagged with the request id", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    chunked: async (_params, ctx) => {
      ctx.streamWriter({ kind: "stdout", data: Buffer.from("hello\n", "utf8") });
      ctx.streamWriter({ kind: "stdout", data: Buffer.from("world\n", "utf8") });
      ctx.streamWriter({ kind: "stderr", data: Buffer.from("warn\n", "utf8") });
      return { exit_code: 0 };
    },
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 99, method: "chunked", params: {} });
  const frames = await readNResponses(outp, 4);  // 3 stream + 1 final

  // First three are stream frames in order. Data is carried as bin (decoded
  // as Uint8Array); compare bytes, not strings, so this stays binary-safe.
  assert.equal(frames[0].id, 99);
  assert.equal(frames[0].stream.kind, "stdout");
  assert.deepEqual(Buffer.from(frames[0].stream.data), Buffer.from("hello\n", "utf8"));
  assert.equal(frames[1].stream.kind, "stdout");
  assert.deepEqual(Buffer.from(frames[1].stream.data), Buffer.from("world\n", "utf8"));
  assert.equal(frames[2].stream.kind, "stderr");
  assert.deepEqual(Buffer.from(frames[2].stream.data), Buffer.from("warn\n", "utf8"));
  // Fourth is the final result.
  assert.equal(frames[3].id, 99);
  assert.deepEqual(frames[3].result, { exit_code: 0 });
  assert.equal(frames[3].stream, undefined);

  inp.end();
  await serverDone;
});

test("streamWriter is optional — handlers that ignore it still work (back-compat)", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    // Handlers from before the streamWriter change had signature (params)
    // only. They must keep working.
    legacy: async (params) => ({ echoed: params.x }),
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 1, method: "legacy", params: { x: 7 } });
  const [resp] = await readNResponses(outp, 1);
  assert.deepEqual(resp, { id: 1, result: { echoed: 7 } });

  inp.end();
  await serverDone;
});

test("streamWriter frames emitted after handler error are dropped (no result frame either way)", async () => {
  // Edge case: a handler that streams some chunks then throws. The thrown
  // error gets a normal {id, error} frame; the streamed chunks that
  // happened before the throw are already on the wire and that's fine.
  // What we must NOT do is emit both a {result} and an {error} frame.
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    boom: async (_params, ctx) => {
      ctx.streamWriter({ kind: "stdout", data: Buffer.from("partial\n", "utf8") });
      throw new Error("mid-stream failure");
    },
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 5, method: "boom" });
  const frames = await readNResponses(outp, 2);
  assert.equal(frames[0].id, 5);
  assert.equal(frames[0].stream.kind, "stdout");
  assert.deepEqual(Buffer.from(frames[0].stream.data), Buffer.from("partial\n", "utf8"));
  assert.equal(frames[1].id, 5);
  assert.equal(frames[1].result, undefined);
  assert.equal(frames[1].error.code, -32603);
  assert.match(frames[1].error.message, /mid-stream failure/);

  inp.end();
  await serverDone;
});
