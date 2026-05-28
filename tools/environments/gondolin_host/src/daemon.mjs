// Gondolin host daemon.
//
// Spawned once per Hermes session. Owns exactly one Gondolin VM and exposes
// JSON-RPC over an AF_UNIX socket. Each Python-side wrapper invocation
// (gondolin_rpc_call.py) opens its own short-lived connection, sends one
// request, reads one response, closes.
//
// Concurrency: the underlying gondolin VM multiplexes up to
// DEFAULT_MAX_QUEUED_EXECS concurrent exec channels via SSH (see
// gondolin/src/qemu/ssh.js: "A guest SSH connection can spawn multiple
// exec channels concurrently"). We let steady-state methods (exec,
// exec_stream, set_secret) run concurrently and only serialize lifecycle
// methods (init, shutdown) so they don't race on the `vm` global. See
// dispatch() below for the mechanism.
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
//   exec_stream({ cmd, ... })   - exec with live stdout/stderr chunks
//   set_secret({ name, value }) - refresh a secret value
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

/**
 * Build the VirtualProvider for one extra_mounts entry.
 *
 * Three knobs:
 *   readonly      — wrap in ReadonlyProvider (rejects writes with EROFS).
 *                   Default true (mirrors docker -v $h:$g:ro).
 *   allowedFiles  — optional allowlist of absolute (provider-relative)
 *                   paths inside the mount that are visible. Everything
 *                   else is shadowed (ENOENT on read, omitted from
 *                   readdir, denySymlinkBypass blocks `ln -s sibling .`
 *                   tricks). When omitted, the whole directory is
 *                   exposed (legacy behavior, used for skills/ and the
 *                   workspace).
 *
 * Exported for unit testing — the construction logic is the security
 * surface for credential isolation and deserves direct coverage rather
 * than only-through-init.
 */
export async function buildExtraMountProvider(em) {
  const { RealFSProvider, ReadonlyProvider, ShadowProvider } =
    await loadGondolin();
  const real = new RealFSProvider(em.hostPath);
  let provider = real;
  if (Array.isArray(em.allowedFiles) && em.allowedFiles.length > 0) {
    // Invert the upstream denylist into an allowlist: shadow every path
    // that is NOT in the allowlist, EXCEPT the directory chain leading
    // to an allowed file (otherwise `readdir('/')` and traversal into
    // `subdir/` would ENOENT and the allowed file becomes unreachable).
    //
    // Algorithm: a path is shadowed iff (a) it's not in the allowlist,
    // AND (b) no allowed file lives under it. The second clause lets
    // `/`, `/.config`, `/.config/gh` all pass when allowed files like
    // `/.config/gh/hosts.yml` exist; only sibling leaves get ENOENT.
    const allowed = new Set(
      em.allowedFiles.map((p) => (p.startsWith("/") ? p : "/" + p)),
    );
    const allowedPrefixes = new Set();
    for (const p of allowed) {
      // Add every ancestor directory of p ("/.config/gh", "/.config", "/")
      // so readdir on any ancestor returns the path leading to p.
      let cur = p;
      while (true) {
        const idx = cur.lastIndexOf("/");
        if (idx <= 0) { allowedPrefixes.add("/"); break; }
        cur = cur.slice(0, idx);
        allowedPrefixes.add(cur);
      }
    }
    provider = new ShadowProvider(real, {
      shouldShadow: ({ path: p }) => {
        if (allowed.has(p)) return false;
        if (allowedPrefixes.has(p)) return false;
        // Path is a leaf (or descendant) that isn't allowed. Shadow it.
        return true;
      },
      writeMode: "deny",
    });
  }
  return em.readonly === false ? provider : new ReadonlyProvider(provider);
}

// State: the daemon owns at most one VM, shared across every connection.
let vm = null;
// Gondolin's secretManager from createHttpHooks(). Lets us refresh secret
// values mid-session without restarting the VM. In stub mode we install a
// fake that records updates for test assertions.
let secretManager = null;

// Each handler is tagged with its concurrency class. ``lifecycle``
// handlers touch the ``vm`` global directly (boot, teardown) and MUST
// run serially with respect to everything else, including other
// lifecycle calls. ``steady`` handlers ride on top of an established
// VM via APIs that already multiplex (vm.exec → SSH channels,
// secretManager.updateSecret → in-memory map). Tagging happens at
// registration (see end of this object) — one place, no separate set
// to drift out of sync, no untagged default. dispatch() looks up the
// tag and falls back to a runtime error if a handler was added
// without a classification.
const LIFECYCLE = "lifecycle";
const STEADY = "steady";

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
    // Per-session rootfs (root disk) size cap. Maps the shared
    // `terminal.container_disk` MB knob onto gondolin's VMOptions.rootfs.size
    // (qemu-syntax string). `null` means "let gondolin pick" (auto-sized
    // based on the image's content). Validate strictly here so a typo at
    // session-init surfaces with a clear error rather than as a confusing
    // VM-boot failure.
    let rootfsSize = null;
    if (config.rootfs_size_mb != null) {
      const mb = config.rootfs_size_mb;
      if (!Number.isFinite(mb) || mb <= 0 || mb !== Math.trunc(mb)) {
        throw new Error(
          `rootfs_size_mb must be a positive integer (got ${mb})`,
        );
      }
      rootfsSize = `${mb}M`;
    }

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

    // Optional additional host->guest mounts. Each entry is
    // { guest_path, host_path, readonly }. Used by Hermes to project
    // ~/.hermes/skills/ and individual credential files into the guest
    // (matches what docker/singularity already do via
    // tools/credential_files.py). Validation here is identical to
    // workspace_mount: paths are checked at init so config bugs surface
    // before the VM boots.
    const extraMounts = [];
    if (Array.isArray(config.extra_mounts)) {
      for (const entry of config.extra_mounts) {
        const guestPath = entry?.guest_path;
        const hostPath = entry?.host_path;
        const readonly = entry?.readonly !== false;  // default true
        const allowedFiles = entry?.allowed_files;
        if (typeof guestPath !== "string" || !guestPath.startsWith("/")) {
          throw new Error(
            "extra_mounts[].guest_path must be an absolute path string",
          );
        }
        if (typeof hostPath !== "string" || hostPath.length === 0) {
          throw new Error("extra_mounts[].host_path must be a non-empty string");
        }
        if (allowedFiles != null) {
          if (!Array.isArray(allowedFiles) || allowedFiles.some((p) => typeof p !== "string")) {
            throw new Error(
              "extra_mounts[].allowed_files must be an array of strings when set",
            );
          }
        }
        try {
          const stat = fs.statSync(hostPath);
          // Allow files too — credential files mount as individual files.
          if (!stat.isDirectory() && !stat.isFile()) {
            throw new Error(
              `extra_mounts[].host_path is not a file or directory: ${hostPath}`,
            );
          }
        } catch (e) {
          if (e.code === "ENOENT") {
            throw new Error(
              `extra_mounts[].host_path does not exist: ${hostPath}`,
            );
          }
          throw e;
        }
        extraMounts.push({ guestPath, hostPath, readonly, allowedFiles });
      }
    }

    if (STUB_VM) {
      vm = {
        async exec(cmd) {
          // Honor a "SLEEP:<ms>:" prefix so tests can assert on concurrent
          // dispatch (one long-running exec should not block a short one).
          // Strip the bash-c wrap the daemon adds so the marker survives.
          const m = cmd.match(/^bash -c '(.*)'$/);
          const inner = m ? m[1].replace(/'\\''/g, "'") : cmd;
          const sleepMatch = inner.match(/^SLEEP:(\d+):/);
          if (sleepMatch) {
            await new Promise((r) => setTimeout(r, Number(sleepMatch[1])));
          }
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
          // STREAM_SLOW:<delayMs>:<count> yields <count> small chunks with
          // <delayMs> between each, so a test can disconnect mid-stream
          // and observe whether the handler aborts (B15).
          const slowMatch = inner.match(/^STREAM_SLOW:(\d+):(\d+)/);
          if (slowMatch) {
            const delay = Number(slowMatch[1]);
            const count = Number(slowMatch[2]);
            return {
              async *chunks() {
                for (let i = 0; i < count; i++) {
                  await new Promise((r) => setTimeout(r, delay));
                  yield { kind: "stdout", data: Buffer.from(`chunk-${i}\n`, "utf8") };
                }
              },
              async exitCode() { return 0; },
            };
          }
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
      if (rootfsSize != null) result.rootfsSize = rootfsSize;
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

    if (config.policy_script) {
      // Visible boot signal: the user pinned a custom policy script;
      // log its absolute path before we run any of its code so a
      // mis-targeted path (or a surprise script left over from a
      // different config) shows up in the daemon log instead of
      // silently executing. SECURITY: see hooks.mjs:loadPolicy.
      log("loading user policy_script:", config.policy_script);
    }
    const policy = await loadPolicy(config.policy_script ?? null);
    const hooksInput = await policy(config);
    log("policy resolved:", {
      allowedHosts: hooksInput.allowedHosts,
      secrets: Object.keys(hooksInput.secrets ?? {}),
      imagePath,
      workspaceMount,
    });
    const { VM, createHttpHooks } = await loadGondolin();
    const hooksResult = createHttpHooks(hooksInput);
    const { httpHooks, env } = hooksResult;
    secretManager = hooksResult.secretManager;
    const vmOptions = { httpHooks, env };
    if (imagePath != null) {
      // SandboxServerOptions hangs off VMOptions.sandbox.
      vmOptions.sandbox = { imagePath };
    }
    const mounts = {};
    if (workspaceMount != null) {
      // vfs.mounts is a Record<guestPath, VirtualProvider>. The workspace
      // mount is read-write so the agent can save files; we use
      // buildExtraMountProvider with readonly=false to keep one
      // construction path for all mounts. Gondolin's sandboxfs init
      // script mounts the VFS provider tree at /data and binds the
      // configured guest paths into the rest of the filesystem (see
      // Alpine ROOTFS_INIT_SCRIPT and SandboxFsConfig.fuseBinds).
      mounts[workspaceMount.guestPath] = await buildExtraMountProvider({
        guestPath: workspaceMount.guestPath,
        hostPath: workspaceMount.hostPath,
        readonly: false,
      });
    }
    for (const em of extraMounts) {
      // Credential mounts: ReadonlyProvider for write rejection, optional
      // ShadowProvider for per-file allowlisting (set via allowed_files).
      // See buildExtraMountProvider for the construction rules.
      mounts[em.guestPath] = await buildExtraMountProvider(em);
    }
    if (Object.keys(mounts).length > 0) {
      vmOptions.vfs = { mounts };
    }
    if (memory != null) vmOptions.memory = memory;
    if (cpus != null) vmOptions.cpus = cpus;
    if (rootfsSize != null) {
      vmOptions.rootfs = { ...(vmOptions.rootfs || {}), size: rootfsSize };
    }
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
      // Real Gondolin: vm.exec returns an ExecProcess. The Symbol.asyncIterator
      // surface yields the merged stdout (all chunks tagged "string") which
      // loses the stderr/stdout split — pytest/npm/gcc and any tool that
      // diffs stderr would misclassify under --stream. Use ExecProcess.output()
      // instead: it returns AsyncIterable<OutputChunk> with each chunk
      // carrying { stream: "stdout"|"stderr", data: Buffer, text: string },
      // matching the non-stream exec's split exactly.
      const real = vm.exec(wrapped, { timeout: timeoutMs, stdout: "pipe", stderr: "pipe" });
      proc = {
        async *chunks() {
          for await (const chunk of real.output()) {
            // chunk.data is a Buffer; re-emit with the proper kind tag.
            // msgpack carries binary natively, so non-UTF-8 bytes
            // survive the wire unmodified.
            yield { kind: chunk.stream, data: chunk.data };
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
      // B15: if the client disconnected, the rpc.mjs context aborts the
      // signal — unwind here rather than streamWriter-ing into a
      // destroyed socket forever. Best-effort kill the underlying VM
      // exec so we don't leak guest pgrps.
      if (ctx.signal?.aborted) {
        if (typeof proc.kill === "function") {
          try { proc.kill(); } catch {}
        }
        throw ctx.signal.reason ?? new Error("client disconnected");
      }
      const ok = ctx.streamWriter(c);
      chunkCount++;
      if (ok === false) {
        // Socket send buffer is full. Without awaiting drain we'd
        // buffer the rest of the VM's output in Node heap, unbounded.
        // `yes | head -c 1G` from the guest would OOM the host
        // daemon. ctx.drain() returns a promise that resolves on the
        // next 'drain' event (added below). Cheap when consumers are
        // keeping up — `ok` is true most of the time.
        await ctx.drain();
      }
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

// Concurrency classification per handler. Single source of truth: edit
// here when adding/changing a handler, dispatch() reads from this map.
// A handler in `handlers` but absent here is a hard error at dispatch
// time — there's no implicit default so contributors must pick a class.
const HANDLER_CONCURRENCY = {
  init: LIFECYCLE,
  shutdown: LIFECYCLE,
  exec: STEADY,
  exec_stream: STEADY,
  set_secret: STEADY,
};

// In stub mode the daemon exposes a debug peek into the fake secret
// manager so secret-refresh tests can assert that updateSecret was
// called with the right values. This is NEVER registered against a
// real VM — its only consumer is the test fixture, and a real Gondolin
// secretManager has no equivalent (secret values are write-only by
// design). Gating on STUB_VM at registration time keeps test-shaped
// surface off the production daemon.
if (STUB_VM) {
  handlers._debug_get_secret = async function _debug_get_secret(params) {
    if (!secretManager || typeof secretManager._peek !== "function") {
      throw new Error("_debug_get_secret only available in stub mode");
    }
    const entry = secretManager._peek(params?.name);
    return { value: entry?.value, hosts: entry?.hosts };
  };
  HANDLER_CONCURRENCY._debug_get_secret = STEADY;

  // B15 regression scaffolding: peek at the inFlightSteady set so a
  // test can observe whether a handler is stuck (e.g. exec_stream
  // hanging on ctx.drain after a client disconnect). Subtracts 1
  // for the in-flight self — this call itself is registered on
  // inFlightSteady, so we never report it as a leak.
  handlers._debug_inflight_steady_count = async function () {
    return { count: Math.max(0, inFlightSteady.size - 1) };
  };
  HANDLER_CONCURRENCY._debug_inflight_steady_count = STEADY;
}

// ----- Socket transport -----

// Per-connection RPC dispatch. Earlier versions of this daemon serialized
// all dispatch through a single promise chain on the rationale that "the
// VM is single-threaded, one in-flight exec at a time." That rationale
// was wrong: the underlying gondolin sandbox supports up to
// DEFAULT_MAX_QUEUED_EXECS (currently 64) concurrent exec channels per
// VM via SSH multiplexing, and the comment in src/qemu/ssh.js says so
// explicitly ("A guest SSH connection can spawn multiple exec channels
// concurrently"). Serializing here broke any caller that needed to
// interleave execs against a single env — most notably
// ``tools/code_execution_tool.py``'s RPC poll loop, which deadlocked
// behind a blocking foreground ``python3 script.py`` execute because
// the poll-loop's request-reading ``ls``/``cat`` calls couldn't acquire
// the dispatch slot.
//
// We still need to serialize lifecycle methods (``init``, ``shutdown``,
// anything that touches the ``vm`` global before it's assigned or
// during teardown). The classification lives in HANDLER_CONCURRENCY
// above so adding a new handler forces an explicit pick.
let lifecycleChain = Promise.resolve();
// B14: track in-flight steady-state requests so shutdown can wait for
// them to settle before tearing down the VM and calling process.exit.
// Without this, an exec/exec_stream on connection A is silently
// orphaned when shutdown arrives on connection B — the caller sees
// the socket die with no response and has to guess at the cause.
const inFlightSteady = new Set();
function dispatch(method, params, ctx) {
  const handler = handlers[method];
  if (!handler) {
    return Promise.reject(
      Object.assign(new Error(`method not found: ${method}`), { code: -32601 }),
    );
  }
  const concurrency = HANDLER_CONCURRENCY[method];
  if (concurrency !== LIFECYCLE && concurrency !== STEADY) {
    // A handler exists but has no concurrency tag. Treating it as
    // either default would be a guess; refuse instead so the
    // contributor has to make the call explicitly.
    return Promise.reject(
      Object.assign(
        new Error(
          `handler ${method} is missing a HANDLER_CONCURRENCY classification`,
        ),
        { code: -32603 },
      ),
    );
  }
  if (concurrency === LIFECYCLE) {
    // Serialize lifecycle transitions against everything else so we
    // don't race on the ``vm`` global. Steady-state methods that arrive
    // mid-init/teardown will still queue behind the chain.
    //
    // For shutdown specifically, wait for in-flight steady-state RPCs
    // to settle before invoking the handler — otherwise the handler
    // calls process.exit() while exec/exec_stream are still pending on
    // other connections, orphaning them (B14).
    const settled = method === "shutdown"
      ? Promise.allSettled([...inFlightSteady])
      : Promise.resolve();
    const next = lifecycleChain
      .then(() => settled)
      .then(() => handler(params, ctx));
    lifecycleChain = next.catch(() => {});
    return next;
  }
  // Steady-state methods (exec, exec_stream, set_secret, ...) wait for
  // the most recent lifecycle transition to settle but then run free.
  // ``vm.exec`` returns an awaitable that the underlying gondolin API
  // multiplexes through SSH; concurrent dispatch is the supported shape.
  // Track the promise in inFlightSteady so a concurrent shutdown can
  // wait for it (B14). Remove on settle so the set doesn't grow.
  const p = lifecycleChain.then(() => handler(params, ctx));
  inFlightSteady.add(p);
  const cleanup = () => inFlightSteady.delete(p);
  p.then(cleanup, cleanup);
  return p;
}

// `runRpcServer` invokes handlers[method](params, ctx). We wrap that here
// so the lifecycle ordering applies across connections; the wrapper
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
//
// Only fire when invoked as the main script. Tests that import this file
// for its exported helpers (buildExtraMountProvider, dispatch handlers)
// must not trigger argv parsing / server startup.

import { pathToFileURL } from "node:url";
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
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

  // eslint-disable-next-line no-inner-declarations
  async function gracefulShutdown(signal) {
    log(`received ${signal}, shutting down`);
    try { server.close(); } catch {}
    try { if (vm) await vm.close(); } catch {}
    try { fs.unlinkSync(values.socket); } catch {}
    process.exit(0);
  }
  process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
  process.on("SIGINT", () => gracefulShutdown("SIGINT"));
}
