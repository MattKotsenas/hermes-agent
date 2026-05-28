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
import { Buffer } from "node:buffer";
import { spawn } from "node:child_process";
import net from "node:net";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import { encode, decode } from "@msgpack/msgpack";

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
    let buf = Buffer.alloc(0);
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`rpc timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (buf.length < 4) return;
      const n = buf.readUInt32BE(0);
      if (buf.length < 4 + n) return;
      clearTimeout(timer);
      sock.end();
      try {
        resolve(decode(buf.subarray(4, 4 + n)));
      } catch (e) {
        reject(e);
      }
    });
    sock.on("error", (err) => { clearTimeout(timer); reject(err); });
    sock.on("connect", () => {
      const payload = encode(request);
      const header = Buffer.alloc(4);
      header.writeUInt32BE(payload.length, 0);
      sock.write(header);
      sock.write(Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength));
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

test("daemon: read-only extra_mounts let the guest read host files but reject writes", { skip: skipReason }, async () => {
  // Spike for the skill-projection problem: docker/singularity bind-mount
  // ~/.hermes/skills/ read-only into the sandbox. Does gondolin's
  // ReadonlyProvider(RealFSProvider(host)) layered into vfs.mounts give
  // us the same property?  Two assertions matter:
  //   1. The guest can stat + cat host files at the mount point.
  //   2. Write attempts surface as EROFS (Read-only file system).

  const tmpHost = mkdtempSync(path.join(os.tmpdir(), "gondolin-ro-mount-"));
  // Drop a file in the host directory before boot.
  const fs2 = await import("node:fs");
  fs2.writeFileSync(path.join(tmpHost, "hello.txt"), "from-host-readonly\n", "utf8");

  const h = new DaemonHarness();
  await h.start();
  try {
    const init = await h.call("init", {
      config: {
        extra_mounts: [
          { guest_path: "/mnt/ro", host_path: tmpHost, readonly: true },
        ],
      },
    }, 120_000);
    assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);

    // (1) Read the host file from the guest.
    const r1 = await h.call("exec", {
      cmd: "cat /mnt/ro/hello.txt",
    }, 60_000);
    assert.equal(r1.error, undefined);
    assert.equal(r1.result.exit_code, 0, `cat stderr: ${r1.result.stderr}`);
    assert.match(r1.result.stdout, /from-host-readonly/);

    // (2) Attempt to write at the same mount — must fail.
    const r2 = await h.call("exec", {
      cmd: "echo guest-write > /mnt/ro/hello.txt; echo rc=$?",
    }, 60_000);
    assert.equal(r2.error, undefined);
    // The shell's `>` redirect fails; echo rc=N reports the failure.
    // Different kernels/libcs map EROFS to slightly different shell
    // messages, but `rc=0` would mean the write succeeded — that's what
    // we mainly want to disprove.
    assert.doesNotMatch(r2.result.stdout, /^rc=0$/m, "write to ro mount should fail");

    // (3) Confirm the host file is unchanged.
    const onHost = fs2.readFileSync(path.join(tmpHost, "hello.txt"), "utf8");
    assert.equal(onHost, "from-host-readonly\n", "host file must be unchanged");

    await h.call("shutdown", {}, 30_000);
  } finally {
    await h.stop();
    rmSync(tmpHost, { recursive: true, force: true });
  }
});

test("daemon: multiple extra_mounts coexist with workspace_mount", { skip: skipReason }, async () => {
  // The mount infrastructure must support layering many mounts at
  // different guest paths (skills/ plus the writable workspace).  This
  // test boots with two mounts active.  Individual-file mounts are
  // covered in a separate test — they may need different handling
  // depending on what Gondolin's VFS supports.

  const fs2 = await import("node:fs");
  const tmpWs = mkdtempSync(path.join(os.tmpdir(), "gondolin-mm-ws-"));
  const tmpSkills = mkdtempSync(path.join(os.tmpdir(), "gondolin-mm-skills-"));
  fs2.writeFileSync(path.join(tmpSkills, "marker.md"), "skills-marker\n", "utf8");

  const h = new DaemonHarness();
  await h.start();
  try {
    const init = await h.call("init", {
      config: {
        workspace_mount: { guest_path: "/workspace", host_path: tmpWs },
        extra_mounts: [
          { guest_path: "/root/.hermes/skills", host_path: tmpSkills, readonly: true },
        ],
      },
    }, 120_000);
    assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);

    // Skills dir reads.
    const r1 = await h.call("exec", { cmd: "cat /root/.hermes/skills/marker.md" }, 60_000);
    assert.equal(r1.result.exit_code, 0, `stderr: ${r1.result.stderr}`);
    assert.match(r1.result.stdout, /skills-marker/);

    // Workspace stays writable.
    const r3 = await h.call("exec", { cmd: "echo workspace-write > /workspace/test.txt && cat /workspace/test.txt" }, 60_000);
    assert.equal(r3.result.exit_code, 0);
    assert.match(r3.result.stdout, /workspace-write/);
    // And the file landed on the host.
    const onHost = fs2.readFileSync(path.join(tmpWs, "test.txt"), "utf8");
    assert.equal(onHost, "workspace-write\n");

    await h.call("shutdown", {}, 30_000);
  } finally {
    await h.stop();
    rmSync(tmpWs, { recursive: true, force: true });
    rmSync(tmpSkills, { recursive: true, force: true });
  }
});

test("daemon: allowed_files exposes only the listed credential and shadows siblings", { skip: skipReason }, async () => {
  // SECURITY: credential parent-dir mounting (~/.config/gh/) must not
  // expose sibling files (~/.config/gh/state.json,
  // ~/.config/gh/migration_state). Python emits an `allowed_files`
  // allowlist per credential-grouped mount; the daemon wraps the
  // RealFSProvider in a ShadowProvider so sibling reads ENOENT inside
  // the guest. This is the end-to-end VM-level check; the daemon-side
  // construction unit is in mounts.test.mjs.

  const fs2 = await import("node:fs");
  const tmpHost = mkdtempSync(path.join(os.tmpdir(), "gondolin-allowlist-"));
  // The "credential" file (allowed) and a sibling that must stay hidden.
  fs2.writeFileSync(path.join(tmpHost, "hosts.yml"), "github.com: token\n", "utf8");
  fs2.writeFileSync(
    path.join(tmpHost, "state.json"),
    "SENSITIVE-SIBLING-CONTENT\n",
    "utf8",
  );
  fs2.writeFileSync(
    path.join(tmpHost, "migration_state"),
    "internal-data\n",
    "utf8",
  );

  const h = new DaemonHarness();
  await h.start();
  try {
    const init = await h.call("init", {
      config: {
        extra_mounts: [
          {
            guest_path: "/root/.config/gh",
            host_path: tmpHost,
            readonly: true,
            allowed_files: ["/hosts.yml"],
          },
        ],
      },
    }, 120_000);
    assert.equal(init.error, undefined, `init failed: ${JSON.stringify(init.error)}`);

    // The allowed file is readable.
    const r1 = await h.call("exec", { cmd: "cat /root/.config/gh/hosts.yml" }, 60_000);
    assert.equal(r1.result.exit_code, 0, `stderr: ${r1.result.stderr}`);
    assert.match(r1.result.stdout, /github\.com: token/);

    // Sibling reads fail. The sibling content must NOT appear in stdout
    // regardless of exit code (a leak past the shadow would let the
    // sensitive payload appear).
    const r2 = await h.call("exec", {
      cmd: "cat /root/.config/gh/state.json 2>&1; echo rc=$?",
    }, 60_000);
    assert.equal(r2.result.exit_code, 0); // shell command itself succeeds
    assert.doesNotMatch(
      r2.result.stdout,
      /SENSITIVE-SIBLING-CONTENT/,
      "shadow-shadowed file leaked its content to the guest",
    );
    assert.doesNotMatch(r2.result.stdout, /^rc=0$/m, "cat on shadowed file should fail");

    // ls of the parent should also not list the sibling.
    const r3 = await h.call("exec", { cmd: "ls /root/.config/gh" }, 60_000);
    assert.equal(r3.result.exit_code, 0);
    assert.match(r3.result.stdout, /hosts\.yml/);
    assert.doesNotMatch(r3.result.stdout, /state\.json/);
    assert.doesNotMatch(r3.result.stdout, /migration_state/);

    // Symlink-bypass attempt: create a symlink from /workspace to the
    // shadowed sibling. ShadowProvider's denySymlinkBypass should consult
    // realpath and still block the read. (The workspace isn't mounted in
    // this test, so use /tmp.)
    const r4 = await h.call("exec", {
      cmd: "ln -sf /root/.config/gh/state.json /tmp/bypass.txt 2>&1; cat /tmp/bypass.txt 2>&1; echo rc=$?",
    }, 60_000);
    assert.doesNotMatch(
      r4.result.stdout,
      /SENSITIVE-SIBLING-CONTENT/,
      "symlink bypass leaked shadowed content",
    );

    await h.call("shutdown", {}, 30_000);
  } finally {
    await h.stop();
    rmSync(tmpHost, { recursive: true, force: true });
  }
});
