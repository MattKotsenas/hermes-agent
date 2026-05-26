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
