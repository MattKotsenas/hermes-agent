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
import { loadPolicy, buildHooksInput } from "./hooks.mjs";

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
// Gondolin's secretManager from createHttpHooks(). Lets us refresh secret
// values mid-session without restarting the VM. In stub mode we install a
// fake that records updates for test assertions.
let secretManager = null;

const handlers = {
  async init(params) {
    if (vm) throw new Error("already initialized");
    const config = params?.config ?? {};
    // Gondolin's SandboxServerOptions accepts `imagePath` as either a
    // directory path with kernel/initrd/rootfs, an image selector
    // ("name:tag" or build id resolved via builtin-image-registry.json),
    // or an explicit GuestAssets object. We surface it as a single string
    // knob here; null/undefined means "let Gondolin use its own default
    // (GONDOLIN_DEFAULT_IMAGE, currently alpine-base:latest)".
    const imagePath = config.image ?? null;

    // Optional VM resource caps. Defaults (1G memory, 2 cpus) are fine for
    // a developer laptop running one session; the knob exists for users
    // running many concurrent sessions on a memory-constrained host, who
    // can dial these down (e.g. 256M + 1 cpu fits ~30 VMs in 8 GB at the
    // cost of slower builds inside the guest).
    const memory = typeof config.memory === "string" ? config.memory : null;
    const cpus = Number.isInteger(config.cpus) && config.cpus > 0 ? config.cpus : null;

    // Optional host->guest workspace mount. Maps a real host directory into
    // the guest's filesystem via Gondolin's vfs.mounts (RealFSProvider). The
    // guest sees a normal mountpoint; reads/writes hit the host directory
    // through the sandboxfs FUSE bridge. We validate host_path exists at
    // init so config bugs surface here rather than from inside the VM.
    let workspaceMount = null;
    if (config.workspace_mount != null) {
      const wm = config.workspace_mount;
      const guestPath = wm.guest_path;
      const hostPath = wm.host_path;
      if (typeof guestPath !== "string" || !guestPath.startsWith("/")) {
        throw new Error(
          "workspace_mount.guest_path must be an absolute path string",
        );
      }
      if (typeof hostPath !== "string" || hostPath.length === 0) {
        throw new Error("workspace_mount.host_path must be a non-empty string");
      }
      try {
        const stat = fs.statSync(hostPath);
        if (!stat.isDirectory()) {
          throw new Error(
            `workspace_mount.host_path is not a directory: ${hostPath}`,
          );
        }
      } catch (e) {
        if (e.code === "ENOENT") {
          throw new Error(
            `workspace_mount.host_path does not exist: ${hostPath}`,
          );
        }
        throw e;
      }
      workspaceMount = { guestPath, hostPath };
    }

    if (STUB_VM) {
      vm = {
        async exec(cmd) {
          return { exitCode: 0, stdout: cmd + "\n", stderr: "" };
        },
        // Streaming exec: if cmd starts with "STREAM:", split the rest by
        // "|" and yield each segment as its own chunk; otherwise yield a
        // single chunk = cmd + "\n" (matches non-streaming behavior).
        // The real Gondolin vm.exec returns an ExecProcess that's awaitable
        // AND async-iterable; this stub mimics just the iterable surface
        // needed by exec_stream's chunk pump.
        //
        // Stub chunks are emitted as Buffers (binary-safe), mirroring the
        // real path where Gondolin yields Buffer chunks. Tests that
        // assert on the data payload should compare bytes, not strings.
        execStreaming(cmd) {
          // The daemon wraps every cmd in `bash -c '...'`. Strip that wrap
          // so the STREAM: marker still works for tests that drive the
          // daemon through the full bash-wrap path.
          const m = cmd.match(/^bash -c '(.*)'$/);
          const inner = m ? m[1].replace(/'\\''/g, "'") : cmd;
          const parts = inner.startsWith("STREAM:")
            ? inner.slice("STREAM:".length).split("|")
            : [inner];
          return {
            async *chunks() {
              for (const p of parts) {
                yield { kind: "stdout", data: Buffer.from(p, "utf8") };
              }
            },
            async exitCode() { return 0; },
          };
        },
        async close() {},
      };
      // Fake secretManager seeded from config.secrets so set_secret tests
      // can exercise the plumbing without a real createHttpHooks() call.
      // Mirrors the real Gondolin API: updateSecret throws on unknown name,
      // deleteSecret zeros the value.
      const stubSecrets = new Map();
      for (const [name, cfg] of Object.entries(config.secrets ?? {})) {
        stubSecrets.set(name, {
          value: cfg.value ?? "",
          hosts: Array.isArray(cfg.hosts) ? [...cfg.hosts] : [],
        });
      }
      secretManager = {
        listSecrets() {
          return Array.from(stubSecrets.entries()).map(([name, s]) => ({
            name, hosts: [...s.hosts], placeholder: "", deleted: s.value === "",
          }));
        },
        updateSecret(name, opts) {
          if (!stubSecrets.has(name)) {
            throw new Error(`unknown secret: ${name}`);
          }
          const cur = stubSecrets.get(name);
          if (opts.value !== undefined) cur.value = opts.value;
          if (opts.hosts !== undefined) cur.hosts = [...opts.hosts];
        },
        deleteSecret(name) {
          if (!stubSecrets.has(name)) {
            throw new Error(`unknown secret: ${name}`);
          }
          stubSecrets.get(name).value = "";
        },
        // Stub-only debug helper used by tests.
        _peek(name) {
          return stubSecrets.get(name);
        },
      };
      log("VM stub ready");
      // Echo back the resolved config so tests can assert what would
      // have been forwarded to a real VM.create() without booting one.
      const result = { ready: true };
      if (imagePath != null) result.imagePath = imagePath;
      if (workspaceMount != null) result.workspaceMount = workspaceMount;
      if (memory != null) result.memory = memory;
      if (cpus != null) result.cpus = cpus;
      // Compute secret diagnostics even in stub mode — they're host-side
      // and don't require a real VM. Lets stub-mode integration tests
      // exercise the diagnostic surface.
      try {
        const stubHooks = buildHooksInput(config);
        if (stubHooks.secretDiagnostics && stubHooks.secretDiagnostics.length) {
          result.secretDiagnostics = stubHooks.secretDiagnostics;
        }
      } catch {
        // Malformed config (e.g. missing hosts) — propagate as a normal
        // init failure on the real path; in stub mode we just skip the
        // diagnostics block so the test can still assert other fields.
      }
      return result;
    }

    const policy = await loadPolicy(config.policy_script ?? null);
    const hooksInput = await policy(config);
    log("policy resolved:", {
      allowedHosts: hooksInput.allowedHosts,
      secrets: Object.keys(hooksInput.secrets ?? {}),
      imagePath,
      workspaceMount,
    });
    const { VM, createHttpHooks, RealFSProvider } = await loadGondolin();
    const hooksResult = createHttpHooks(hooksInput);
    const { httpHooks, env } = hooksResult;
    secretManager = hooksResult.secretManager;
    const vmOptions = { httpHooks, env };
    if (imagePath != null) {
      // SandboxServerOptions hangs off VMOptions.sandbox.
      vmOptions.sandbox = { imagePath };
    }
    if (workspaceMount != null) {
      // vfs.mounts is a Record<guestPath, VirtualProvider>. RealFSProvider
      // exposes a host directory directly. Gondolin's sandboxfs init script
      // mounts the VFS provider tree at /data and binds the configured guest
      // paths into the rest of the filesystem (see Alpine ROOTFS_INIT_SCRIPT
      // and SandboxFsConfig.fuseBinds). For the agent, this means files
      // written under workspaceMount.guestPath inside the VM appear under
      // workspaceMount.hostPath on the host, and vice versa.
      vmOptions.vfs = {
        mounts: {
          [workspaceMount.guestPath]: new RealFSProvider(workspaceMount.hostPath),
        },
      };
    }
    if (memory != null) vmOptions.memory = memory;
    if (cpus != null) vmOptions.cpus = cpus;
    vm = await VM.create(vmOptions);
    log("VM ready");
    const result = { ready: true };
    if (hooksInput.secretDiagnostics && hooksInput.secretDiagnostics.length) {
      result.secretDiagnostics = hooksInput.secretDiagnostics;
    }
    return result;
  },

  async exec(params) {
    if (!vm) throw new Error("not initialized");
    const cmd = params?.cmd;
    if (typeof cmd !== "string") throw new Error("exec: 'cmd' must be a string");
    const timeoutMs = params?.timeout_ms ?? 180_000;
    const start = Date.now();
    // Wrap with bash -c so the user's command runs under bash (which the
    // BaseEnvironment session-snapshot prelude relies on: 'builtin cd',
    // 'declare -f', 'shopt', 'set +e/+u' are all bashisms). The Gondolin
    // helper image's default /bin/sh is BusyBox sh and would reject those.
    // Single-quote the cmd and escape any embedded single quotes the
    // standard way (POSIX trick: end-quote, escape, start-quote).
    const escaped = cmd.replace(/'/g, "'\\''");
    const wrapped = `bash -c '${escaped}'`;
    const result = await vm.exec(wrapped, { timeout: timeoutMs });
    return {
      exit_code: result.exitCode,
      stdout: result.stdout,
      stderr: result.stderr,
      duration_ms: Date.now() - start,
    };
  },

  // Streaming exec: same wire-level command, but stdout/stderr chunks are
  // pushed via ctx.streamWriter as they arrive from the VM. Final response
  // carries exit_code + duration only — accumulated bytes already streamed.
  //
  // Stub-mode VMs implement vm.execStreaming(cmd) returning
  // { chunks(): AsyncIterable<{kind, data}>, exitCode(): Promise<number> }.
  // Real Gondolin's vm.exec returns an ExecProcess that's both awaitable
  // and async-iterable — we adapt it via the same interface below.
  async exec_stream(params, ctx) {
    if (!vm) throw new Error("not initialized");
    const cmd = params?.cmd;
    if (typeof cmd !== "string") throw new Error("exec_stream: 'cmd' must be a string");
    const timeoutMs = params?.timeout_ms ?? 180_000;
    const start = Date.now();
    const escaped = cmd.replace(/'/g, "'\\''");
    const wrapped = `bash -c '${escaped}'`;

    let proc;
    if (typeof vm.execStreaming === "function") {
      // Stub path or any adapter that exposes a stream-shaped interface.
      proc = vm.execStreaming(wrapped, { timeout: timeoutMs });
    } else {
      // Real Gondolin: vm.exec with { stdout: "pipe" } returns an
      // ExecProcess that's async-iterable per chunk. The iterator yields
      // raw chunks (Buffers); we re-wrap each as {kind: "stdout", data}
      // with the bytes passed through unmodified. msgpack carries binary
      // data natively (bin8/bin32), so no encoding step is needed —
      // arbitrary process output (test fixtures with non-UTF-8 bytes,
      // compiled artefacts piped to stdout, etc.) survives the wire.
      const real = vm.exec(wrapped, { timeout: timeoutMs, stdout: "pipe", stderr: "pipe" });
      proc = {
        async *chunks() {
          // Gondolin emits chunks via for-await. We don't know which is
          // stdout vs stderr from the unified iterator without extra
          // metadata — fall back to tagging everything as stdout.
          // (Gondolin's stderr is interleaved in stdout when both are
          // "pipe"; users who want a strict split can use the non-stream
          // exec call.)
          for await (const chunk of real) {
            // Pass through Buffers verbatim. If Gondolin ever hands us
            // a string (back-compat), wrap it in a Buffer so the wire
            // shape is consistent.
            const data = Buffer.isBuffer(chunk)
              ? chunk
              : chunk instanceof Uint8Array
                ? Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength)
                : Buffer.from(String(chunk), "utf8");
            yield { kind: "stdout", data };
          }
        },
        async exitCode() {
          const finalResult = await real;
          return finalResult.exitCode;
        },
      };
    }

    let chunkCount = 0;
    for await (const c of proc.chunks()) {
      ctx.streamWriter(c);
      chunkCount++;
    }
    const exitCode = await proc.exitCode();
    return {
      exit_code: exitCode,
      chunks: chunkCount,
      duration_ms: Date.now() - start,
    };
  },

  async set_secret(params) {
    if (!vm) throw new Error("not initialized");
    if (!secretManager) {
      throw new Error("set_secret unsupported: no secretManager available");
    }
    const name = params?.name;
    if (typeof name !== "string" || !name) {
      throw new Error("set_secret: 'name' must be a non-empty string");
    }
    const opts = {};
    if (typeof params?.value === "string") opts.value = params.value;
    if (Array.isArray(params?.hosts)) opts.hosts = params.hosts;
    if (opts.value === undefined && opts.hosts === undefined) {
      throw new Error("set_secret: must provide at least 'value' or 'hosts'");
    }
    secretManager.updateSecret(name, opts);
    return { ok: true };
  },

  async _debug_get_secret(params) {
    // Stub-mode only: introspect the fake secretManager for test assertions.
    // No-op against a real Gondolin VM (real secretManager doesn't expose
    // the value back — for security).
    if (!secretManager || typeof secretManager._peek !== "function") {
      throw new Error("_debug_get_secret only available in stub mode");
    }
    const entry = secretManager._peek(params?.name);
    return { value: entry?.value, hosts: entry?.hosts };
  },

  async shutdown() {
    if (vm) {
      try {
        await vm.close();
      } catch (e) {
        log("vm.close error:", e.message);
      }
      vm = null;
      secretManager = null;
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
function dispatch(method, params, ctx) {
  const handler = handlers[method];
  if (!handler) {
    return Promise.reject(
      Object.assign(new Error(`method not found: ${method}`), { code: -32601 }),
    );
  }
  const next = dispatchChain.then(() => handler(params, ctx));
  // Don't propagate rejections through the chain — each call awaits its
  // own result, but a handler error shouldn't poison subsequent calls.
  dispatchChain = next.catch(() => {});
  return next;
}

// `runRpcServer` invokes handlers[method](params, ctx). We wrap that here
// so the per-VM dispatch lock applies across connections; the wrapper
// forwards params + ctx (the streamWriter facility) into dispatch().
const serializedHandlers = new Proxy(
  {},
  {
    get: (_t, method) => (params, ctx) => dispatch(method, params, ctx),
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
