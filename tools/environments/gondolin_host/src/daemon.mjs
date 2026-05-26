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
    vm = await VM.create(vmOptions);
    log("VM ready");
    return { ready: true };
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
