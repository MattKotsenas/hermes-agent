// Integration test: full daemon end-to-end with a real Gondolin VM.
//
// Boots a real VM via the daemon over the AF_UNIX socket transport,
// runs an exec, validates output. Two tests: a happy-path echo, and a
// credential-injection check that confirms the guest sees a placeholder
// rather than the host-side secret.
//
// Skipped automatically if QEMU/KVM aren't available (CI fallback,
// dev machine without /dev/kvm). For socket-transport tests that don't
// need a VM, see socket_transport.test.mjs.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.resolve(__dirname, "../src/daemon.mjs");

const canRun = await canRunVm();
const skipReason = canRun ? false : "no KVM/qemu";

async function canRunVm() {
  if (!existsSync("/dev/kvm")) return false;
  return new Promise((resolve) => {
    const which = spawn("which", ["qemu-system-x86_64"]);
    which.on("exit", (code) => resolve(code === 0));
  });
}

function rpcCall(sockPath, request, { timeoutMs = 120_000 } = {}) {
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
        sock.end();
        try { resolve(JSON.parse(buf.slice(0, idx))); }
        catch (e) { reject(e); }
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

class DaemonHarness {
  constructor() {
    this.proc = null;
    this.tmp = null;
    this.sockPath = null;
  }
  async start() {
    this.tmp = mkdtempSync(path.join(os.tmpdir(), "gondolin-int-"));
    this.sockPath = path.join(this.tmp, "d.sock");
    this.proc = spawn("node", [DAEMON, "--socket", this.sockPath], {
      stdio: ["ignore", "ignore", "pipe"],
      env: { ...process.env, GONDOLIN_DAEMON_QUIET: "1" },
    });
    this.proc.stderr.on("data", () => {});
    await waitForSocket(this.sockPath);
  }
  call(method, params, timeoutMs) {
    return rpcCall(this.sockPath, { id: Date.now(), method, params }, { timeoutMs });
  }
  async stop() {
    if (!this.proc) return;
    try { this.proc.kill("SIGTERM"); } catch {}
    await new Promise((resolve) => {
      if (this.proc.exitCode != null) return resolve();
      this.proc.once("exit", resolve);
      setTimeout(() => {
        try { this.proc.kill("SIGKILL"); } catch {}
        resolve();
      }, 5000);
    });
    if (this.tmp) rmSync(this.tmp, { recursive: true, force: true });
  }
}

test("daemon: init → exec → shutdown end-to-end", { skip: skipReason }, async () => {
  const h = new DaemonHarness();
  await h.start();
  try {
    const init = await h.call("init", { config: {} }, 120_000);
    assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);
    assert.equal(init.result.ready, true);

    const r = await h.call("exec", { cmd: "echo hello-from-vm" }, 60_000);
    assert.equal(r.error, undefined, `exec failed: ${JSON.stringify(r.error)}`);
    assert.equal(r.result.exit_code, 0);
    assert.match(r.result.stdout, /hello-from-vm/);

    const s = await h.call("shutdown", {}, 30_000);
    assert.equal(s.error, undefined);
    assert.equal(s.result.ok, true);
  } finally {
    await h.stop();
  }
});

test("daemon: credential injection works through the RPC layer", { skip: skipReason }, async () => {
  const h = new DaemonHarness();
  await h.start();
  try {
    process.env.TEST_FAKE_SECRET = "real-secret-zzz-12345";
    const init = await h.call("init", {
      config: {
        allowed_hosts: ["*"],
        secrets: {
          TEST_FAKE_SECRET: {
            hosts: ["api.github.com"],
            from_env: "TEST_FAKE_SECRET",
          },
        },
      },
    }, 120_000);
    assert.equal(init.error, undefined);

    const r = await h.call("exec", {
      cmd: `echo "guest_value=$TEST_FAKE_SECRET" "guest_len=$(echo -n $TEST_FAKE_SECRET | wc -c)"`,
    }, 60_000);
    assert.equal(r.result.exit_code, 0);
    assert.doesNotMatch(r.result.stdout, /real-secret-zzz/, "real secret leaked to guest!");
    assert.match(r.result.stdout, /guest_len=\d+/);

    await h.call("shutdown", {}, 30_000);
  } finally {
    delete process.env.TEST_FAKE_SECRET;
    await h.stop();
  }
});
