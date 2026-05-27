// Line-delimited JSON-RPC server over a duplex stream pair.
//
// One JSON object per line on input → one JSON object per line on output.
// Requests are dispatched serially through `handlers[method]`.
//
// Error codes follow JSON-RPC 2.0:
//   -32700 parse error
//   -32601 method not found
//   -32603 internal error (handler threw)
//
// `runRpcServer` returns a Promise that resolves when input ends.

export function runRpcServer({ input, output, handlers }) {
  return new Promise((resolve, reject) => {
    let buf = "";

    const write = (resp) => {
      output.write(JSON.stringify(resp) + "\n");
    };

    const handleLine = async (line) => {
      let req;
      try {
        req = JSON.parse(line);
      } catch (e) {
        write({ id: null, error: { code: -32700, message: `parse error: ${e.message}` } });
        return;
      }
      const { id = null, method, params } = req;
      const handler = handlers[method];
      if (!handler) {
        write({ id, error: { code: -32601, message: `method not found: ${method}` } });
        return;
      }
      // Streaming context: handlers can call ctx.streamWriter(obj) to push
      // intermediate `{ id, stream: obj }` frames before the final response.
      // Old single-arg handlers ignore ctx and behave unchanged.
      const ctx = {
        streamWriter: (frame) => write({ id, stream: frame }),
      };
      try {
        const result = await handler(params, ctx);
        write({ id, result });
      } catch (e) {
        write({ id, error: { code: -32603, message: e.message ?? String(e) } });
      }
    };

    // Serial dispatch via promise chain — keeps response order deterministic
    // and matches the "one VM, one in-flight exec" reality of phase 2.
    let chain = Promise.resolve();

    input.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        chain = chain.then(() => handleLine(line));
      }
    });

    input.on("end", () => {
      chain.then(resolve).catch(reject);
    });
    input.on("error", reject);
  });
}
