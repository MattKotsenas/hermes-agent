// Integration test: daemon accepts multiple AF_UNIX connections.
//
// The daemon owns ONE Gondolin VM but must serve MANY wrapper invocations
// (the Python rpc_call.py wrapper is per-terminal-call). Each connection
// is short-lived: connect, send one exec, read one response, close.
//
// This test stubs init/exec out (no VM) and validates the socket
// transport: spawn the daemon with --socket <path>, open three sequential
// connections, send a ping handler call on each, get correct responses.
//
// Stateful handler: confirm VM state persists across connections by
// incrementing a counter in the daemon and reading it back. Phase 2's
// per-session VM model depends on this.

import { test } from "node:test";
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { encode, decode } from "@msgpack/msgpack";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.resolve(__dirname, "../src/daemon.mjs");

// Encode a single request as a length-prefixed msgpack frame.
function encodeFrame(obj) {
  const payload = encode(obj);
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  return Buffer.concat([
    header,
    Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength),
  ]);
}

// Iteratively pop complete frames out of a growing byte buffer. Returns
// { frames, rest } so the caller can keep accumulating leftover bytes.
function drainFrames(buf) {
  const frames = [];
  let rest = buf;
  while (rest.length >= 4) {
    const n = rest.readUInt32BE(0);
    if (rest.length < 4 + n) break;
    const payload = rest.subarray(4, 4 + n);
    frames.push(decode(payload));
    rest = rest.subarray(4 + n);
  }
  return { frames, rest };
}

// One JSON-RPC roundtrip over a fresh AF_UNIX connection.
function rpcCall(sockPath, request, { timeoutMs = 5000 } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(sockPath);
    let buf = Buffer.alloc(0);
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      const { frames, rest } = drainFrames(buf);
      buf = rest;
      if (frames.length > 0) {
        clearTimeout(timer);
        sock.end();
        resolve(frames[0]);
      }
    });
    sock.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    sock.on("connect", () => {
      sock.write(encodeFrame(request));
    });
  });
}

// Streaming variant: collect all frames (stream + final result/error) until
// a frame without `stream` arrives, then close. Returns { streamFrames, final }.
function rpcCallStreaming(sockPath, request, { timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(sockPath);
    let buf = Buffer.alloc(0);
    const streamFrames = [];
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`streaming rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      let frames;
      try {
        ({ frames, rest: buf } = drainFrames(buf));
      } catch (e) {
        clearTimeout(timer);
        sock.destroy();
        reject(e);
        return;
      }
      for (const frame of frames) {
        if (frame.stream !== undefined) {
          streamFrames.push(frame.stream);
        } else {
          clearTimeout(timer);
          sock.end();
          resolve({ streamFrames, final: frame });
          return;
        }
      }
    });
    sock.on("error", (err) => { clearTimeout(timer); reject(err); });
    sock.on("connect", () => {
      sock.write(encodeFrame(request));
    });
  });
}

async function waitForSocket(sockPath, timeoutMs = 5000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      await new Promise((resolve, reject) => {
        const s = net.createConnection(sockPath);
        s.on("connect", () => { s.end(); resolve(); });
        s.on("error", reject);
      });
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  throw new Error(`socket ${sockPath} never came up`);
}

test("daemon: socket transport, multiple connections, shared VM state", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-sock-test-"));
  const sockPath = path.join(tmp, "d.sock");

  // Run with stubbed VM via env override; daemon should treat any
  // GONDOLIN_DAEMON_STUB_VM=1 invocation as "skip real VM boot, fake
  // exec to echo cmd back" so this test stays VM-free.
  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  let stderr = "";
  proc.stderr.on("data", (c) => { stderr += c.toString(); });

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
    if (stderr) console.error("daemon stderr:", stderr);
  });

  await waitForSocket(sockPath);

  // 1. init (stubbed)
  const init = await rpcCall(sockPath, { id: 1, method: "init", params: { config: {} } });
  assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);
  assert.equal(init.result.ready, true);

  // 2. Three sequential exec calls, each on a fresh connection.
  for (let i = 0; i < 3; i++) {
    const resp = await rpcCall(sockPath, {
      id: 100 + i,
      method: "exec",
      params: { cmd: `echo iter-${i}` },
    });
    assert.equal(resp.error, undefined);
    assert.equal(resp.result.exit_code, 0);
    assert.match(resp.result.stdout, new RegExp(`echo iter-${i}`));
    assert.equal(resp.id, 100 + i, "id must round-trip");
  }

  // 3. shutdown
  const sd = await rpcCall(sockPath, { id: 999, method: "shutdown", params: {} });
  assert.equal(sd.error, undefined);
  assert.equal(sd.result.ok, true);

  // Daemon should exit shortly.
  await new Promise((resolve) => {
    if (proc.exitCode != null) return resolve();
    proc.once("exit", resolve);
    setTimeout(resolve, 2000);
  });
});

test("daemon: concurrent execs are not serialized (steady-state dispatch)", async (t) => {
  // This is the canonical regression test for the daemon's dispatcher
  // concurrency model (see HANDLER_CONCURRENCY in daemon.mjs). It lives
  // here rather than in daemon.integration.test.mjs because it doesn't
  // need a real KVM-backed VM — the stubbed VM is enough to exercise
  // the dispatch layer, and gating on /dev/kvm would mean CI without
  // hardware accel runs no coverage of the concurrency contract.
  //
  // Regression: an earlier daemon implementation chained ALL dispatch
  // (including exec) through a single promise chain on the rationale
  // that "the VM is single-threaded." That was wrong — the underlying
  // gondolin sandbox supports up to DEFAULT_MAX_QUEUED_EXECS concurrent
  // exec channels per VM via SSH multiplexing, and serializing here
  // deadlocked any caller that needed to interleave execs against a
  // single env (most notably code_execution_tool.py's RPC poll loop
  // running alongside a blocking foreground `python3 script.py`).
  //
  // This test fires two concurrent stubbed execs against one daemon —
  // a long one (500ms) and a short one (~immediate). If dispatch is
  // serialized the short exec waits for the long one (~500ms); if
  // dispatch is concurrent the short returns essentially instantly
  // while the long is still in flight.

  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-conc-test-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  let stderr = "";
  proc.stderr.on("data", (c) => { stderr += c.toString(); });

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
    if (stderr) console.error("daemon stderr:", stderr);
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, { id: 1, method: "init", params: { config: {} } });
  assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);

  // Fire both execs concurrently. The long one sleeps 500ms inside the
  // stub; the short one returns immediately. Measure when each one
  // resolves relative to start.
  const t0 = Date.now();
  const longPromise = rpcCall(sockPath, {
    id: 100,
    method: "exec",
    params: { cmd: "SLEEP:500:long" },
  }).then((r) => ({ kind: "long", at: Date.now() - t0, resp: r }));
  // Tiny stagger so the long exec definitely arrives first. Without it
  // there's a small window where both arrive in the same event-loop tick
  // and ordering depends on connect() timing.
  await new Promise((r) => setTimeout(r, 20));
  const shortPromise = rpcCall(sockPath, {
    id: 101,
    method: "exec",
    params: { cmd: "quick" },
  }).then((r) => ({ kind: "short", at: Date.now() - t0, resp: r }));

  const [first, second] = await Promise.all([
    Promise.race([longPromise, shortPromise]),
    Promise.race([
      longPromise.then((v) => ({ tag: "long", v })),
      shortPromise.then((v) => ({ tag: "short", v })),
    ]).then(async (winner) => (winner.tag === "short" ? longPromise : shortPromise)),
  ]);

  // The short call MUST land before the long call. If dispatch is
  // serialized it'd be the other way around — short waits for long.
  assert.equal(first.kind, "short",
    `expected short exec to resolve first; got ${first.kind} at ${first.at}ms ` +
    `(short=${shortPromise.then((s) => s.at)}, long=${longPromise.then((l) => l.at)})`);
  assert.equal(second.kind, "long");

  // And the short call must resolve well before the long one's 500ms
  // sleep would have finished, proving it was not queued behind it.
  assert.ok(first.at < 300,
    `short exec took ${first.at}ms but should be <<300ms; ` +
    `dispatch is likely re-serialized`);
  assert.ok(second.at >= 500,
    `long exec returned at ${second.at}ms but should be >=500ms (the sleep)`);

  // Both should succeed.
  assert.equal(first.resp.error, undefined);
  assert.equal(second.resp.error, undefined);

  const sd = await rpcCall(sockPath, { id: 999, method: "shutdown", params: {} });
  assert.equal(sd.error, undefined);
});

test("daemon: connection closing mid-exec doesn't kill the daemon", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-sock-test-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => proc.once("exit", r));
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  await rpcCall(sockPath, { id: 1, method: "init", params: { config: {} } });

  // Open a connection and immediately destroy it without sending anything.
  await new Promise((resolve) => {
    const s = net.createConnection(sockPath);
    s.on("connect", () => { s.destroy(); resolve(); });
    s.on("error", () => resolve());
  });

  // Daemon should still respond on a fresh connection.
  const resp = await rpcCall(sockPath, {
    id: 2,
    method: "exec",
    params: { cmd: "echo still-alive" },
  });
  assert.equal(resp.error, undefined);
  assert.match(resp.result.stdout, /still-alive/);
});


test("daemon: init forwards config.image to VM as imagePath", async (t) => {
  // In stub mode, the daemon echoes back the resolved image selector
  // in the init result so we can assert it without booting a real VM.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-img-test-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  // Caller passes a custom image selector.
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: { image: "ubuntu-noble:latest" } },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.equal(
    init.result.imagePath,
    "ubuntu-noble:latest",
    "stub-mode init must echo the resolved image selector",
  );
});


test("daemon: init without config.image leaves imagePath undefined", async (t) => {
  // When no image is configured, the daemon must NOT inject one — Gondolin
  // gets to fall back to its own default (GONDOLIN_DEFAULT_IMAGE).
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-img-test-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: {} },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.equal(init.result.imagePath, undefined,
    "no image config -> no imagePath in init result (Gondolin uses its own default)");
});


test("daemon: init forwards config.workspace_mount to VM as vfs.mounts", async (t) => {
  // The Python side passes workspace_mount = { guest_path, host_path } to
  // wire a real host directory into the guest filesystem via Gondolin's
  // vfs.mounts + RealFSProvider. In stub mode the daemon echoes back what
  // it would have configured, so we can assert the mapping without booting.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-mount-test-"));
  const sockPath = path.join(tmp, "d.sock");
  const hostDir = path.join(tmp, "workspace");
  mkdirSync(hostDir, { recursive: true });

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: {
      config: {
        workspace_mount: { guest_path: "/workspace", host_path: hostDir },
      },
    },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.deepEqual(
    init.result.workspaceMount,
    { guestPath: "/workspace", hostPath: hostDir },
    "stub-mode init must echo the resolved workspace mount",
  );
});


test("daemon: init rejects workspace_mount whose host_path doesn't exist", async (t) => {
  // Fail fast at init: if the host directory doesn't exist, the user has a
  // config bug or a permissions problem. Surface it as an init error rather
  // than letting it through and erroring obscurely from inside the guest.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-mount-bad-"));
  const sockPath = path.join(tmp, "d.sock");
  const missing = path.join(tmp, "does", "not", "exist");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: {
      config: {
        workspace_mount: { guest_path: "/workspace", host_path: missing },
      },
    },
  });
  assert.ok(init.error, "init must error when host_path is missing");
  assert.match(init.error.message, /host_path/);
});


test("daemon: init without workspace_mount leaves workspaceMount undefined", async (t) => {
  // No mount config -> no VFS wiring. (Gondolin still runs; the guest just
  // doesn't get a host-backed /workspace.)
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-mount-none-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: {} },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.equal(init.result.workspaceMount, undefined,
    "no workspace_mount config -> no workspaceMount in init result");
});


// ---- set_secret RPC ----------------------------------------------------
//
// Mid-session secret refresh. The daemon owns Gondolin's secretManager
// (from createHttpHooks); set_secret routes through it. In stub mode the
// daemon keeps a fake secretManager that records updates so tests can
// assert the plumbing without a real VM.

test("daemon: set_secret updates a configured secret after init", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-setsec-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  // Init with one secret so the secretManager has something to update.
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: {
      config: {
        secrets: {
          GITHUB_TOKEN: {
            hosts: ["github.com"],
            value: "original-token",
          },
        },
      },
    },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);

  // Update the value.
  const upd = await rpcCall(sockPath, {
    id: 2,
    method: "set_secret",
    params: { name: "GITHUB_TOKEN", value: "rotated-token" },
  });
  assert.equal(upd.error, undefined, `set_secret should succeed: ${JSON.stringify(upd.error)}`);
  assert.equal(upd.result.ok, true);

  // Confirm via the debug echo: stub mode exposes the current resolved
  // value for a named secret so tests can assert without booting a VM.
  const peek = await rpcCall(sockPath, {
    id: 3,
    method: "_debug_get_secret",
    params: { name: "GITHUB_TOKEN" },
  });
  assert.equal(peek.error, undefined);
  assert.equal(peek.result.value, "rotated-token");
});


test("daemon: set_secret on unknown name returns an error", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-setsec-unk-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: {} },  // no secrets configured
  });

  const upd = await rpcCall(sockPath, {
    id: 2,
    method: "set_secret",
    params: { name: "NEVER_DEFINED", value: "x" },
  });
  assert.ok(upd.error, "set_secret on unknown name must error");
  assert.match(upd.error.message, /NEVER_DEFINED|unknown|not.*found/i);
});


test("daemon: set_secret before init fails clearly", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-setsec-pre-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  const upd = await rpcCall(sockPath, {
    id: 1,
    method: "set_secret",
    params: { name: "X", value: "y" },
  });
  assert.ok(upd.error, "set_secret pre-init must error");
  assert.match(upd.error.message, /not initialized|init/i);
});


// ---- VM resource caps --------------------------------------------------
//
// Each Gondolin VM costs ~256-512 MB of host memory at default settings.
// Letting users cap per-VM memory + cpus is the lever for "I want to run
// 10 sessions on a 16GB laptop." Forward config.memory and config.cpus to
// Gondolin's VMOptions.memory / VMOptions.cpus.

test("daemon: init forwards config.memory and config.cpus to VM", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-vmres-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: { memory: "256M", cpus: 1 } },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.equal(init.result.memory, "256M");
  assert.equal(init.result.cpus, 1);
});


test("daemon: init forwards config.rootfs_size_mb to VMOptions.rootfs.size", async (t) => {
  // The shared `terminal.container_disk` knob (MB int, default 50GB) caps
  // the rootfs image used by docker/singularity/modal/daytona. Gondolin's
  // equivalent is VMOptions.rootfs.size (qemu suffix string, e.g. "50G").
  // Python translates container_disk MB → rootfs_size_mb on the wire and
  // the daemon converts it to the qemu shape. Verifies the round-trip in
  // stub mode by echoing the resolved rootfs size back in the init result.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-rootfs-size-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);

  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: { rootfs_size_mb: 20480 } },  // 20 GB
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  // The daemon converts MB → qemu-suffixed string. 20480 MB → "20480M"
  // (gondolin's parser accepts the bare MB form).
  assert.equal(init.result.rootfsSize, "20480M");
});


test("daemon: init without rootfs_size_mb leaves rootfs unset (gondolin defaults)", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-rootfs-default-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: {} },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.rootfsSize, undefined);
});


test("daemon: init rejects non-positive rootfs_size_mb", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-rootfs-invalid-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: { rootfs_size_mb: -5 } },
  });
  assert.notEqual(init.error, undefined);
  assert.match(init.error.message || String(init.error), /rootfs_size_mb/);
});


test("daemon: init without resource caps leaves memory/cpus undefined", async (t) => {
  // Unset means "let Gondolin pick its defaults" (1G memory, 2 cpus).
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-vmres-default-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1,
    method: "init",
    params: { config: {} },
  });
  assert.equal(init.error, undefined);
  assert.equal(init.result.ready, true);
  assert.equal(init.result.memory, undefined);
  assert.equal(init.result.cpus, undefined);
});

// ---- exec_stream: chunked stdout/stderr before final exit_code -------------
//
// Long-running commands (test suites, build scripts) shouldn't have to buffer
// all output until exit. exec_stream uses the RPC streamWriter to push
// chunks as they arrive. Stub-mode test: the stub vm.exec recognizes a
// "STREAM:" prefix and emits one chunk per "|" segment, then exits.

test("daemon: exec_stream emits chunked stdout frames then a final result", async (t) => {
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-stream-test-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, GONDOLIN_DAEMON_QUIET: "1", GONDOLIN_DAEMON_STUB_VM: "1" },
  });
  proc.stderr.on("data", () => {});

  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, { id: 1, method: "init", params: { config: {} } });
  assert.equal(init.error, undefined);

  // STREAM: prefix is a stub-mode marker — chunks separated by '|'.
  const { streamFrames, final } = await rpcCallStreaming(sockPath, {
    id: 42,
    method: "exec_stream",
    params: { cmd: "STREAM:hello|world|done" },
  });

  // Each segment should arrive as its own frame. Stream data is now
  // carried as msgpack bin (decoded to Uint8Array); decode to string to
  // assert against the stub's per-segment payload.
  assert.equal(streamFrames.length, 3, `got frames: ${JSON.stringify(streamFrames.map((f) => ({ kind: f.kind, len: f.data?.length })))}`);
  assert.equal(streamFrames[0].kind, "stdout");
  assert.equal(Buffer.from(streamFrames[0].data).toString("utf8"), "hello");
  assert.equal(Buffer.from(streamFrames[1].data).toString("utf8"), "world");
  assert.equal(Buffer.from(streamFrames[2].data).toString("utf8"), "done");

  // Final frame: exit_code only (stdout/stderr accumulated by the wrapper).
  assert.equal(final.id, 42);
  assert.equal(final.error, undefined);
  assert.equal(final.result.exit_code, 0);
});

test("daemon: exec_stream falls back to a single chunk for non-STREAM stub commands", async (t) => {
  // Plain commands in stub mode should still work end-to-end — they just
  // produce one chunk (the echoed cmd) plus a final result. Confirms that
  // exec_stream is a drop-in for exec on the daemon side without forcing
  // every caller to format their cmds with STREAM: markers.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-stream-test2-"));
  const sockPath = path.join(tmp, "d.sock");

  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, GONDOLIN_DAEMON_QUIET: "1", GONDOLIN_DAEMON_STUB_VM: "1" },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, { id: 1, method: "init", params: { config: {} } });
  assert.equal(init.error, undefined);

  const { streamFrames, final } = await rpcCallStreaming(sockPath, {
    id: 9,
    method: "exec_stream",
    params: { cmd: "echo hello" },
  });
  assert.equal(streamFrames.length, 1);
  assert.match(Buffer.from(streamFrames[0].data).toString("utf8"), /echo hello/);
  assert.equal(final.result.exit_code, 0);
});


// rpcCall variants that surface socket close as a distinct
// outcome (instead of waiting until timeout) so the test can tell the
// difference between "daemon answered cleanly" and "daemon killed the
// connection mid-flight."
function rpcCallObservingClose(sockPath, request, { timeoutMs = 5000 } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(sockPath);
    let buf = Buffer.alloc(0);
    let resolved = false;
    const timer = setTimeout(() => {
      if (resolved) return;
      resolved = true;
      sock.destroy();
      reject(new Error(`rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      const { frames, rest } = drainFrames(buf);
      buf = rest;
      if (frames.length > 0 && !resolved) {
        resolved = true;
        clearTimeout(timer);
        sock.end();
        resolve({ kind: "frame", frame: frames[0] });
      }
    });
    sock.on("close", () => {
      if (resolved) return;
      resolved = true;
      clearTimeout(timer);
      // No frame arrived before close — this is the B14 bug shape.
      resolve({ kind: "closed_without_frame" });
    });
    sock.on("error", (err) => {
      if (resolved) return;
      // ECONNRESET / EPIPE on a daemon process.exit mid-request shows up
      // here. Treat it the same as a close without frame.
      resolved = true;
      clearTimeout(timer);
      resolve({ kind: "closed_without_frame", error: err.code });
    });
    sock.on("connect", () => {
      sock.write(encodeFrame(request));
    });
  });
}


test("daemon: shutdown waits for in-flight steady-state RPCs on other connections", async (t) => {
  // The bug: dispatch() classifies exec/exec_stream as STEADY, which
  // means they wait for lifecycleChain to settle but DON'T register
  // themselves on it. shutdown() (LIFECYCLE) checks lifecycleChain,
  // sees it's resolved, and immediately calls process.exit(0) — even
  // if other connections still have exec calls in flight. Those
  // callers see the socket die mid-request and have to interpret the
  // close as an opaque transport error.
  //
  // Reproduction: connection A fires a 1s SLEEP exec; ~50ms later
  // connection B fires shutdown. Without the fix, A's rpcCall sees
  // the socket close with no response (kind: "closed_without_frame")
  // and exits early. With the fix, A's rpcCall gets a proper exec
  // result frame back before the daemon dies.
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-b14-test-"));
  const sockPath = path.join(tmp, "d.sock");
  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCallObservingClose(sockPath, {
    id: 1, method: "init", params: { config: {} },
  });
  assert.equal(init.kind, "frame", `init failed: ${JSON.stringify(init)}`);

  // Connection A: a slow exec that takes ~1s inside the stub. Fire it
  // and capture the outcome.
  const longExec = rpcCallObservingClose(sockPath, {
    id: 2, method: "exec", params: { cmd: "SLEEP:1000:slow" },
  }, { timeoutMs: 5000 });

  // Give the long exec a moment to land on the daemon and start its
  // SLEEP. (The handler awaits a setTimeout under the hood.)
  await new Promise((r) => setTimeout(r, 100));

  // Connection B: shutdown. Fire it concurrently — under the bug, the
  // daemon answers shutdown immediately and kills the process before
  // the long exec returns.
  const shutdown = rpcCallObservingClose(sockPath, {
    id: 3, method: "shutdown", params: {},
  }, { timeoutMs: 5000 });

  // Wait for BOTH to settle.
  const [longResult, shutdownResult] = await Promise.all([longExec, shutdown]);

  // Shutdown itself should always succeed.
  assert.equal(shutdownResult.kind, "frame",
    `shutdown should return a frame: ${JSON.stringify(shutdownResult)}`);

  // The actual B14 assertion: the long exec must get a structured
  // response before the daemon dies. Under the bug, kind ===
  // "closed_without_frame".
  assert.equal(longResult.kind, "frame",
    "in-flight exec was orphaned by shutdown. The daemon killed " +
    "the connection before responding to the exec. shutdown must wait " +
    "for in-flight steady-state RPCs to settle before tearing down the " +
    "VM and exiting. Got: " + JSON.stringify(longResult));
  // And the response must actually be the exec result (not a generic
  // "daemon shutting down" error injected late).
  assert.equal(longResult.frame.id, 2);
  assert.equal(longResult.frame.error, undefined,
    `exec returned error instead of result: ${JSON.stringify(longResult.frame)}`);
  assert.equal(longResult.frame.result.exit_code, 0);
});


test("daemon: exec_stream aborts when the client disconnects mid-stream", async (t) => {
  // Bug: ctx.drain() in rpc.mjs awaits output.once("drain") but the
  // 'drain' event never fires on a destroyed/closed stream. If a
  // client crashes or quits while exec_stream is mid-pump, the
  // handler awaits a Promise that will never resolve. The underlying
  // ExecProcess (in real Gondolin, an SSH channel + guest pgrp) stays
  // alive forever, and the promise leaks in inFlightSteady — exhausting
  // the dispatcher's queue cap (DEFAULT_MAX_QUEUED_EXECS=64) over time.
  //
  // Reproduction: open conn A and fire a slow STREAM exec
  // (STREAM_SLOW:30ms x 50 chunks = ~1.5s of streaming). Wait until
  // a few chunks have arrived, then destroy A's socket. Open conn B
  // shortly after and query _debug_inflight_steady_count: with the
  // bug it returns 1 (the orphaned exec_stream is still pending);
  // with the fix it returns 0 (the handler caught the disconnect
  // and unwound).
  const tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-b15-test-"));
  const sockPath = path.join(tmp, "d.sock");
  const proc = spawn("node", [DAEMON, "--socket", sockPath], {
    stdio: ["ignore", "ignore", "pipe"],
    env: {
      ...process.env,
      GONDOLIN_DAEMON_QUIET: "1",
      GONDOLIN_DAEMON_STUB_VM: "1",
    },
  });
  proc.stderr.on("data", () => {});
  t.after(async () => {
    try { proc.kill("SIGTERM"); } catch {}
    await new Promise((r) => {
      if (proc.exitCode != null) return r();
      proc.once("exit", r);
      setTimeout(r, 2000);
    });
    rmSync(tmp, { recursive: true, force: true });
  });

  await waitForSocket(sockPath);
  const init = await rpcCall(sockPath, {
    id: 1, method: "init", params: { config: {} },
  });
  assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);

  // Fire a slow exec_stream on its own connection — we want to be
  // able to destroy *just this* socket.
  const slowSock = net.createConnection(sockPath);
  const slowChunks = [];
  await new Promise((r) => slowSock.on("connect", r));
  slowSock.on("data", (chunk) => slowChunks.push(chunk));
  slowSock.write(encodeFrame({
    id: 100,
    method: "exec_stream",
    // 100ms x 100 chunks = ~10s total runtime — gives the disconnect
    // path plenty of time to manifest. The 3s post-disconnect wait
    // below is much shorter than the natural completion time.
    params: { cmd: "STREAM_SLOW:100:100" },
  }));

  // Wait until at least one chunk has been received so we know the
  // handler is in the middle of the pump (not still waiting on init).
  const deadline = Date.now() + 5000;
  while (slowChunks.length === 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 20));
  }
  assert.ok(slowChunks.length > 0, "no stream chunks arrived — test setup wrong");

  // Rip the socket mid-stream. The pump is now waiting on the next
  // setTimeout(30ms) inside the stub, and will try to streamWriter()
  // the next chunk to a destroyed pipe.
  slowSock.destroy();

  // Give the daemon a moment to either (a) detect the close and
  // unwind, or (b) hang forever waiting on output.once("drain"). The
  // sleep is generous (3s) so even on slow CI the pump has time to
  // try writing the remaining chunks.
  await new Promise((r) => setTimeout(r, 3000));

  // Query the in-flight counter via a fresh connection.
  const debug = await rpcCall(sockPath, {
    id: 9999, method: "_debug_inflight_steady_count", params: {},
  });
  assert.equal(debug.error, undefined,
    `debug call failed: ${JSON.stringify(debug.error)}`);
  assert.equal(debug.result.count, 0,
    "exec_stream leaked after client disconnect. " +
    `Expected 0 in-flight steady-state handlers, got ${debug.result.count}. ` +
    "ctx.drain() must reject (or the handler must observe a signal) " +
    "when the client socket closes, so the pump can unwind instead " +
    "of hanging on output.once('drain') forever.");
});

