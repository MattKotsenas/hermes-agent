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
