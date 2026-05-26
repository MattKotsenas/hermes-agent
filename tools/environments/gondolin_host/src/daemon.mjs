// Gondolin host daemon.
//
// Spawned once per Hermes session. Owns exactly one Gondolin VM and exposes
// JSON-RPC over stdin/stdout. The Python side (Hermes) talks to this daemon
// instead of touching Gondolin's TS API directly.
//
// RPC methods:
//   init({ config })            - boot the VM with the given policy config
//   exec({ cmd, timeout_ms })   - run a shell command, return result
//   set_secret({ name, value }) - refresh a secret value (phase 2.5 path)
//   shutdown({})                - graceful teardown
//
// Wire format: line-delimited JSON-RPC 2.0 over stdin/stdout. One request
// per line in, one response per line out. See src/rpc.mjs for framing.

import { VM, createHttpHooks } from "@earendil-works/gondolin";
import { runRpcServer } from "./rpc.mjs";
import { loadPolicy } from "./hooks.mjs";

const QUIET = !!process.env.GONDOLIN_DAEMON_QUIET;
function log(...args) {
  if (!QUIET) console.error("[gondolin-host]", ...args);
}

// State: the daemon owns at most one VM.
let vm = null;
let policy = null; // (yaml) -> { allowedHosts, secrets }

const handlers = {
  async init(params) {
    if (vm) throw new Error("already initialized");
    const config = params?.config ?? {};
    policy = await loadPolicy(config.policy_script ?? null);
    const hooksInput = await policy(config);
    log("policy resolved:", {
      allowedHosts: hooksInput.allowedHosts,
      secrets: Object.keys(hooksInput.secrets ?? {}),
    });
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
    // Schedule process exit after the response flushes.
    setImmediate(() => process.exit(0));
    return { ok: true };
  },
};

const done = runRpcServer({
  input: process.stdin,
  output: process.stdout,
  handlers,
});

// SIGTERM/SIGINT: tear down VM before exiting.
async function gracefulShutdown(signal) {
  log(`received ${signal}, shutting down`);
  try { if (vm) await vm.close(); } catch {}
  process.exit(0);
}
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT", () => gracefulShutdown("SIGINT"));

done.catch((e) => {
  log("rpc server error:", e.message);
  process.exit(1);
});
