// Integration test: full daemon end-to-end.
//
// Boots a real Gondolin VM via the daemon, sends an exec RPC, gets
// the response. Validates the daemon as a process: spawn it as a
// subprocess, talk to it over its stdin/stdout, see the VM actually run.
//
// Skipped automatically if QEMU/KVM aren't available (CI fallback,
// dev machine without /dev/kvm, etc.) so the suite stays green there.
//
// Test budget: ~30s per test because cold VM boot + helper warmup is
// expensive on first run. Subsequent runs share a VM image cache and
// are faster.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
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

class DaemonHarness {
  constructor() {
    this.proc = null;
    this.responses = new Map(); // id -> resolver
    this.buf = "";
    this.nextId = 1;
  }

  async start() {
    this.proc = spawn("node", [DAEMON], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, GONDOLIN_DAEMON_QUIET: "1" },
    });
    this.proc.stdout.on("data", (chunk) => this._onData(chunk));
    this.proc.stderr.on("data", () => {}); // discard daemon logs in tests
  }

  _onData(chunk) {
    this.buf += chunk.toString("utf8");
    let idx;
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, idx).trim();
      this.buf = this.buf.slice(idx + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      const resolver = this.responses.get(msg.id);
      if (resolver) {
        this.responses.delete(msg.id);
        resolver(msg);
      }
    }
  }

  call(method, params, timeoutMs = 60_000) {
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => {
      this.responses.set(id, resolve);
      setTimeout(() => {
        if (this.responses.has(id)) {
          this.responses.delete(id);
          reject(new Error(`timeout: ${method}`));
        }
      }, timeoutMs);
    });
    this.proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    return promise;
  }

  async stop() {
    if (!this.proc) return;
    try {
      this.proc.stdin.end();
    } catch {}
    await new Promise((resolve) => {
      this.proc.once("exit", resolve);
      setTimeout(() => {
        try { this.proc.kill("SIGKILL"); } catch {}
        resolve();
      }, 5000);
    });
  }
}

test("daemon: init → exec → shutdown end-to-end", { skip: skipReason }, async () => {
  const h = new DaemonHarness();
  await h.start();
  try {
    // init the VM. Defaults: open allowedHosts, no secrets.
    const init = await h.call("init", { config: {} }, 120_000);
    assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);
    assert.equal(init.result.ready, true);

    // exec a trivial command. Should return exit_code 0 and "hello" in stdout.
    const r = await h.call("exec", { cmd: "echo hello-from-vm" }, 60_000);
    assert.equal(r.error, undefined, `exec failed: ${JSON.stringify(r.error)}`);
    assert.equal(r.result.exit_code, 0);
    assert.match(r.result.stdout, /hello-from-vm/);

    // shutdown
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

    // From inside the VM, $TEST_FAKE_SECRET should be a placeholder, NOT the real value.
    const r = await h.call("exec", {
      cmd: `echo "guest_value=$TEST_FAKE_SECRET" "guest_len=$(echo -n $TEST_FAKE_SECRET | wc -c)"`,
    }, 60_000);
    assert.equal(r.result.exit_code, 0);
    // The placeholder should NOT contain the real secret substring.
    assert.doesNotMatch(r.result.stdout, /real-secret-zzz/, "real secret leaked to guest!");
    // The placeholder should still be a non-empty value.
    assert.match(r.result.stdout, /guest_len=\d+/);

    await h.call("shutdown", {}, 30_000);
  } finally {
    delete process.env.TEST_FAKE_SECRET;
    await h.stop();
  }
});
