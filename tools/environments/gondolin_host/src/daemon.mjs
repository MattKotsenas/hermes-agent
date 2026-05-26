// Gondolin host daemon.
//
// Spawned once per Hermes session. Owns exactly one Gondolin VM and exposes
// JSON-RPC over an AF_UNIX socket. Each Python-side wrapper invocation
// (gondolin_rpc_call.py) opens its own short-lived connection, sends one
// request, reads one response, closes. The daemon serializes inbound
// requests against the single VM, so concurrent connections are safe but
// effectively single-threaded — matching "one VM, one in-flight exec."
//
// CLI:
//   node daemon.mjs --socket <path>
//
// Env switches:
//   GONDOLIN_DAEMON_QUIET=1     suppress stderr logging
//   GONDOLIN_DAEMON_STUB_VM=1   skip real VM boot, echo cmds back (for tests)
//
// RPC methods:
//   init({ config })            - boot the VM with the given policy config
//   exec({ cmd, timeout_ms })   - run a shell command, return result
//   set_secret({ name, value }) - refresh a secret value (phase 2.5)
//   shutdown({})                - graceful teardown, daemon exits

import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";

import { runRpcServer } from "./rpc.mjs";
import { loadPolicy } from "./hooks.mjs";

const QUIET = !!process.env.GONDOLIN_DAEMON_QUIET;
const STUB_VM = !!process.env.GONDOLIN_DAEMON_STUB_VM;

function log(...args) {
  if (!QUIET) console.error("[gondolin-host]", ...args);
}

// Lazy import: only pull the heavyweight Gondolin module when we actually
// need a real VM. Tests that set GONDOLIN_DAEMON_STUB_VM=1 never load it.
async function loadGondolin() {
  return await import("@earendil-works/gondolin");
}

// State: the daemon owns at most one VM, shared across every connection.
let vm = null;

const handlers = {
  async init(params) {
    if (vm) throw new Error("already initialized");
    const config = params?.config ?? {};

    if (STUB_VM) {
      vm = {
        async exec(cmd) {
          return { exitCode: 0, stdout: cmd + "\n", stderr: "" };
        },
        async close() {},
      };
      log("VM stub ready");
      return { ready: true };
    }

    const policy = await loadPolicy(config.policy_script ?? null);
    const hooksInput = await policy(config);
    log("policy resolved:", {
      allowedHosts: hooksInput.allowedHosts,
      secrets: Object.keys(hooksInput.secrets ?? {}),
    });
    const { VM, createHttpHooks } = await loadGondolin();
    const { httpHooks, env } = createHttpHooks(hooksInput);
    vm = await VM.create({ httpHooks, env });
    log("VM ready");
    return { ready: true };
  },

  async exec(params) {
    if (!vm) throw new Error("not initialized");
    const cmd = params?.cmd;
    if (typeof cmd !== "string") throw new Error("exec: 'cmd' must be a string");
    const timeoutMs = params?.timeout_ms ?? 180_000;
    const start = Date.now();
    const result = await vm.exec(cmd, { timeout: timeoutMs });
    return {
      exit_code: result.exitCode,
      stdout: result.stdout,
      stderr: result.stderr,
      duration_ms: Date.now() - start,
    };
  },

  async set_secret(_params) {
    // Phase 2.5: mid-session secret refresh.
    // Gondolin's createHttpHooks() input isn't documented as mutable
    // post-init; need to investigate the low-level API before wiring this.
    throw new Error("set_secret: not implemented in phase 2");
  },

  async shutdown() {
    if (vm) {
      try {
        await vm.close();
      } catch (e) {
        log("vm.close error:", e.message);
      }
      vm = null;
    }
    setImmediate(() => process.exit(0));
    return { ok: true };
  },
};

// ----- Socket transport -----

// Serialize RPC dispatch across connections. The VM is single-threaded
// (one in-flight exec at a time), and even concurrent stubbed exec calls
// would race on the `vm` global during init/shutdown. A simple promise
// chain is sufficient: each new request awaits the previous one.
let dispatchChain = Promise.resolve();
function dispatch(method, params) {
  const handler = handlers[method];
  if (!handler) {
    return Promise.reject(
      Object.assign(new Error(`method not found: ${method}`), { code: -32601 }),
    );
  }
  const next = dispatchChain.then(() => handler(params));
  // Don't propagate rejections through the chain — each call awaits its
  // own result, but a handler error shouldn't poison subsequent calls.
  dispatchChain = next.catch(() => {});
  return next;
}

// Wrapped handlers that go through the serializer. We pass these to
// runRpcServer per connection rather than the raw handlers above, so
// the serialization is uniform across every transport entry point.
const serializedHandlers = new Proxy(
  {},
  {
    get: (_t, method) => (params) => dispatch(method, params),
  },
);

function startSocketServer(sockPath) {
  // Clean up any stale socket from a previous crash.
  try {
    fs.unlinkSync(sockPath);
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  fs.mkdirSync(path.dirname(sockPath), { recursive: true });

  const server = net.createServer((conn) => {
    // One JSON-RPC server per connection, both sides line-delimited.
    // Connection closes when either side ends or when the wrapper hangs up
    // after one request — that's normal, not an error.
    conn.on("error", (e) => log("conn error:", e.message));
    runRpcServer({
      input: conn,
      output: conn,
      handlers: serializedHandlers,
    }).catch((e) => log("rpc server error:", e.message));
  });

  server.on("error", (e) => {
    log("server error:", e.message);
    process.exit(1);
  });

  server.listen(sockPath, () => {
    // 0600 — only the running user can connect.
    try {
      fs.chmodSync(sockPath, 0o600);
    } catch (e) {
      log("chmod sock failed:", e.message);
    }
    log("listening on", sockPath);
  });

  return server;
}

// ----- Entry point -----

const { values } = parseArgs({
  options: {
    socket: { type: "string" },
  },
  allowPositionals: false,
});

if (!values.socket) {
  console.error("usage: node daemon.mjs --socket <path>");
  process.exit(2);
}

const server = startSocketServer(values.socket);

async function gracefulShutdown(signal) {
  log(`received ${signal}, shutting down`);
  try { server.close(); } catch {}
  try { if (vm) await vm.close(); } catch {}
  try { fs.unlinkSync(values.socket); } catch {}
  process.exit(0);
}
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
