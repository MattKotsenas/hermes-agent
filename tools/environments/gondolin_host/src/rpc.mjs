// Length-prefixed msgpack JSON-RPC server over a duplex stream pair.
//
// Frame format on the wire:
//   [ uint32 BE: payload length N ][ N bytes: msgpack-encoded JSON-RPC object ]
//
// Input: a sequence of such frames, each carrying one request object
//   `{ id, method, params }`. Output: a sequence of such frames, each
//   carrying one response object — either an intermediate
//   `{ id, stream: ... }` from a handler's streamWriter, or a terminal
//   `{ id, result: ... }` / `{ id, error: ... }`.
//
// Requests are dispatched serially through `handlers[method]`.
//
// Error codes follow JSON-RPC 2.0:
//   -32700 parse error
//   -32601 method not found
//   -32603 internal error (handler threw)
//
// Why msgpack + length prefix instead of newline-delimited JSON:
//   1. Binary safety. Stream chunks from `vm.exec(... stdout: "pipe")`
//      can be arbitrary bytes (npm install tarball stderr, test fixtures,
//      compiled output). JSON+UTF-8 corrupts non-UTF-8 byte sequences,
//      and JSON+base64 pays a 33% wire tax on every chunk for the same
//      bytes msgpack carries natively as bin8/bin32.
//   2. Speed on large payloads. Encode+decode is ~5-7x faster than
//      JSON.stringify(... + base64) once payloads cross ~256 KB. See
//      bench/protocol_compare.{py,mjs} for the measured numbers.
//   3. No framing search. A u32 length prefix means the parser does one
//      read of 4 bytes, one read of N bytes — no scanning for delimiters
//      and no escape handling. Streaming reassembly is trivially correct.
//
// `runRpcServer` returns a Promise that resolves when input ends.

import { encode, decode } from "@msgpack/msgpack";

// Hard cap on the size of any single frame payload. The daemon already
// caps per-exec output limits inside Gondolin; this is a defence-in-depth
// floor that prevents a hostile or buggy peer from making us allocate
// gigabytes for one frame's length prefix. 64 MB is comfortably above the
// largest reasonable single stdout/stderr chunk (Gondolin's default chunk
// size is ~64 KB) and far below host memory pressure.
const MAX_FRAME_BYTES = 64 * 1024 * 1024;

function writeFrame(output, obj) {
  const payload = encode(obj);
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  output.write(header);
  // payload is a Uint8Array; Node streams accept it directly.
  output.write(Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength));
}

export function runRpcServer({ input, output, handlers }) {
  return new Promise((resolve, reject) => {
    // Accumulating byte buffer. We append every incoming chunk and
    // process as many complete frames as we can find.
    let buf = Buffer.alloc(0);

    const handleFrame = async (payload) => {
      let req;
      try {
        req = decode(payload);
      } catch (e) {
        writeFrame(output, {
          id: null,
          error: { code: -32700, message: `parse error: ${e.message}` },
        });
        return;
      }
      const { id = null, method, params } = req ?? {};
      const handler = handlers[method];
      if (!handler) {
        writeFrame(output, {
          id,
          error: { code: -32601, message: `method not found: ${method}` },
        });
        return;
      }
      // Streaming context: handlers can call ctx.streamWriter(obj) to
      // push intermediate `{ id, stream: obj }` frames before the final
      // response. Old single-arg handlers ignore ctx and behave
      // unchanged.
      const ctx = {
        streamWriter: (frame) => writeFrame(output, { id, stream: frame }),
      };
      try {
        const result = await handler(params, ctx);
        writeFrame(output, { id, result });
      } catch (e) {
        writeFrame(output, {
          id,
          error: { code: -32603, message: e.message ?? String(e) },
        });
      }
    };

    // Serial dispatch via promise chain — keeps response order
    // deterministic and matches the "one VM, one in-flight exec" reality
    // of phase 2.
    let chain = Promise.resolve();

    input.on("data", (chunk) => {
      buf = buf.length === 0 ? Buffer.from(chunk) : Buffer.concat([buf, chunk]);
      // Drain as many full frames as the buffer currently holds. Each
      // iteration consumes one 4-byte header + N-byte payload. If we
      // don't have a full frame yet, break and wait for more bytes.
      while (true) {
        if (buf.length < 4) break;
        const n = buf.readUInt32BE(0);
        if (n > MAX_FRAME_BYTES) {
          // Defensive: a corrupt or hostile peer that claims a huge
          // frame. Close the connection with a parse error and stop
          // reading. We can't recover the stream once the framing is
          // suspect.
          writeFrame(output, {
            id: null,
            error: {
              code: -32700,
              message: `frame too large: ${n} > ${MAX_FRAME_BYTES}`,
            },
          });
          buf = Buffer.alloc(0);
          input.destroy(new Error("frame too large"));
          return;
        }
        if (buf.length < 4 + n) break;
        const payload = buf.subarray(4, 4 + n);
        buf = buf.subarray(4 + n);
        // Copy the payload before handing it to the async handler — the
        // backing buffer we just sliced gets replaced on the next data
        // event, and msgpack decode is synchronous so we're fine here,
        // but we want the handler chain to be free of buffer aliasing.
        const owned = Buffer.from(payload);
        chain = chain.then(() => handleFrame(owned));
      }
    });

    input.on("end", () => {
      chain.then(resolve).catch(reject);
    });
    input.on("error", reject);
  });
}
