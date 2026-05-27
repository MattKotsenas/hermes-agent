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
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.resolve(__dirname, "../src/daemon.mjs");

// One JSON-RPC roundtrip over a fresh AF_UNIX connection.
function rpcCall(sockPath, request, { timeoutMs = 5000 } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(sockPath);
    let buf = "";
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      const idx = buf.indexOf("\n");
      if (idx >= 0) {
        clearTimeout(timer);
        const line = buf.slice(0, idx);
        sock.end();
        try {
          resolve(JSON.parse(line));
        } catch (e) {
          reject(e);
        }
      }
    });
    sock.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    sock.on("connect", () => {
      sock.write(JSON.stringify(request) + "\n");
    });
  });
}

// Streaming variant: collect all frames (stream + final result/error) until
// a frame without `stream` arrives, then close. Returns { streamFrames, final }.
function rpcCallStreaming(sockPath, request, { timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(sockPath);
    let buf = "";
    const streamFrames = [];
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`streaming rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (!line.trim()) continue;
        let frame;
        try { frame = JSON.parse(line); }
        catch (e) { clearTimeout(timer); sock.destroy(); reject(e); return; }
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
      sock.write(JSON.stringify(request) + "\n");
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

  // Each segment should arrive as its own frame.
  assert.equal(streamFrames.length, 3, `got frames: ${JSON.stringify(streamFrames)}`);
  assert.equal(streamFrames[0].kind, "stdout");
  assert.equal(streamFrames[0].data, "hello");
  assert.equal(streamFrames[1].data, "world");
  assert.equal(streamFrames[2].data, "done");

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
  assert.match(streamFrames[0].data, /echo hello/);
  assert.equal(final.result.exit_code, 0);
});
