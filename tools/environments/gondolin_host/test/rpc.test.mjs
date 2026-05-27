// Test: line-delimited JSON-RPC framing.
//
// The daemon reads requests (one JSON object per line) from a stream and
// writes responses (one JSON object per line) to another stream. It must:
//   1. Pair requests and responses by id.
//   2. Handle multiple requests in flight (though MVP can be serial).
//   3. Report unknown method as a JSON-RPC error response.
//   4. Reject malformed JSON with an error response (no id available -> id=null).
//
// We don't boot a VM here — we inject a fake method handler so framing
// is testable in isolation.

import { test } from "node:test";
import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import { runRpcServer } from "../src/rpc.mjs";

function writeReq(stream, obj) {
  stream.write(JSON.stringify(obj) + "\n");
}

async function readNResponses(stream, n) {
  return new Promise((resolve, reject) => {
    const out = [];
    let buf = "";
    stream.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (!line.trim()) continue;
        out.push(JSON.parse(line));
        if (out.length === n) resolve(out);
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

test("returns parse-error with id=null for malformed JSON", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const serverDone = runRpcServer({ input: inp, output: outp, handlers: {} });

  inp.write("this is not json\n");
  const [resp] = await readNResponses(outp, 1);
  assert.equal(resp.id, null);
  assert.equal(resp.error.code, -32700); // JSON-RPC parse error

  inp.end();
  await serverDone;
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

// ---- streamWriter: intermediate frames before the final response -----------
//
// For streaming exec, the handler needs to push stdout chunks as they arrive
// from the VM rather than buffering everything until the command exits.
// We extend the RPC contract: handlers receive a `streamWriter` second arg.
// Calling streamWriter(obj) emits `{ id, stream: obj }` lines. The handler's
// eventual return value becomes the final `{ id, result }` frame as before.
// Handlers that don't use streamWriter behave exactly like today (back-compat).

test("streamWriter emits intermediate {stream} frames tagged with the request id", async () => {
  const inp = new PassThrough();
  const outp = new PassThrough();
  const handlers = {
    chunked: async (_params, ctx) => {
      ctx.streamWriter({ kind: "stdout", data: "hello\n" });
      ctx.streamWriter({ kind: "stdout", data: "world\n" });
      ctx.streamWriter({ kind: "stderr", data: "warn\n" });
      return { exit_code: 0 };
    },
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 99, method: "chunked", params: {} });
  const frames = await readNResponses(outp, 4);  // 3 stream + 1 final

  // First three are stream frames in order.
  assert.deepEqual(frames[0], { id: 99, stream: { kind: "stdout", data: "hello\n" } });
  assert.deepEqual(frames[1], { id: 99, stream: { kind: "stdout", data: "world\n" } });
  assert.deepEqual(frames[2], { id: 99, stream: { kind: "stderr", data: "warn\n" } });
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
      ctx.streamWriter({ kind: "stdout", data: "partial\n" });
      throw new Error("mid-stream failure");
    },
  };
  const serverDone = runRpcServer({ input: inp, output: outp, handlers });

  writeReq(inp, { id: 5, method: "boom" });
  const frames = await readNResponses(outp, 2);
  assert.deepEqual(frames[0], { id: 5, stream: { kind: "stdout", data: "partial\n" } });
  assert.equal(frames[1].id, 5);
  assert.equal(frames[1].result, undefined);
  assert.equal(frames[1].error.code, -32603);
  assert.match(frames[1].error.message, /mid-stream failure/);

  inp.end();
  await serverDone;
});
