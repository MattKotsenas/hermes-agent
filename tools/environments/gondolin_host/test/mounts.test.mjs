// Unit tests for the extra_mounts → VirtualProvider construction in
// daemon.mjs. We hit the providers directly (no VM boot) so the security
// property — sibling credentials in a parent dir get ENOENT, not read
// access — can be asserted on every commit, not only when KVM is
// available.
//
// The real-VM integration test in daemon.integration.test.mjs covers the
// end-to-end VFS path (file-system semantics the guest actually sees).

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { buildExtraMountProvider } from "../src/daemon.mjs";

function mkTmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// ---- backward compat: no allowed_files → unrestricted read-only mount ----

test("buildExtraMountProvider: no allowed_files exposes the whole directory read-only", async () => {
  const dir = mkTmp("gondolin-mount-plain-");
  try {
    fs.writeFileSync(path.join(dir, "a.txt"), "hello-a");
    fs.writeFileSync(path.join(dir, "b.txt"), "hello-b");
    const provider = await buildExtraMountProvider({
      guestPath: "/mnt/cfg",
      hostPath: dir,
      readonly: true,
    });
    // Both files visible via readdir.
    const names = await provider.readdir("/");
    assert.deepEqual(names.sort(), ["a.txt", "b.txt"]);
    // Read-only — write rejected.
    assert.equal(provider.readonly, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---- allowed_files: only listed files are visible ----

test("buildExtraMountProvider: allowed_files hides siblings (ENOENT on read, omitted from readdir)", async () => {
  // SECURITY: a credential at ~/.config/gh/hosts.yml requires mounting
  // ~/.config/gh/, but the rest of that directory (state.json,
  // migration_state) must not be visible to the agent. allowed_files
  // makes the visible set explicit; everything else gets ShadowProvider's
  // ENOENT.
  const dir = mkTmp("gondolin-mount-allow-");
  try {
    fs.writeFileSync(path.join(dir, "hosts.yml"), "github.com: token");
    fs.writeFileSync(path.join(dir, "state.json"), "{secret: data}");
    fs.writeFileSync(path.join(dir, "migration_state"), "internal-data");
    const provider = await buildExtraMountProvider({
      guestPath: "/root/.config/gh",
      hostPath: dir,
      readonly: true,
      allowedFiles: ["/hosts.yml"],
    });

    // Allowed file is readable.
    const stat = await provider.stat("/hosts.yml");
    assert.equal(stat.isFile(), true);

    // Siblings are ENOENT (errno 2) on stat. ShadowProvider rejects with
    // an errno-tagged error rather than a string code, so match on
    // err.errno or err.code.
    const isENOENT = (err) => err.errno === 2 || err.code === "ENOENT" || /ENOENT|ERRNO_2/.test(err.code || "");
    await assert.rejects(
      () => provider.stat("/state.json"),
      isENOENT,
      "sibling credential file must surface as ENOENT",
    );
    await assert.rejects(
      () => provider.stat("/migration_state"),
      isENOENT,
    );

    // Directory listing only shows allowed files.
    const names = await provider.readdir("/");
    const plain = names.map((n) => (typeof n === "string" ? n : n.name));
    assert.deepEqual(plain.sort(), ["hosts.yml"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("buildExtraMountProvider: allowed_files with multiple entries exposes all of them", async () => {
  // Two credentials sharing one parent dir (e.g. ~/.config/gcloud/
  // credentials.json + access_tokens.db). Both end up in allowed_files,
  // both visible, every other sibling shadowed.
  const dir = mkTmp("gondolin-mount-multi-");
  try {
    fs.writeFileSync(path.join(dir, "credentials.json"), "{}");
    fs.writeFileSync(path.join(dir, "access_tokens.db"), "");
    fs.writeFileSync(path.join(dir, "leftover-debug.log"), "secrets-here");
    const provider = await buildExtraMountProvider({
      guestPath: "/root/.config/gcloud",
      hostPath: dir,
      readonly: true,
      allowedFiles: ["/credentials.json", "/access_tokens.db"],
    });

    assert.equal((await provider.stat("/credentials.json")).isFile(), true);
    assert.equal((await provider.stat("/access_tokens.db")).isFile(), true);
    await assert.rejects(() => provider.stat("/leftover-debug.log"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("buildExtraMountProvider: allowed_files blocks symlink-based bypass", async () => {
  // A guest process trying `ln -s state.json bypass.txt; cat bypass.txt`
  // must not see through to a shadowed sibling. ShadowProvider's
  // denySymlinkBypass default consults realpath().
  //
  // We can't actually `ln -s` inside this test (we don't have a guest
  // shell — we're hitting the provider directly), but we CAN drop a
  // symlink on the host filesystem and exercise the same realpath
  // resolution.
  const dir = mkTmp("gondolin-mount-symlink-");
  try {
    fs.writeFileSync(path.join(dir, "hosts.yml"), "ok");
    fs.writeFileSync(path.join(dir, "state.json"), "secret");
    fs.symlinkSync("state.json", path.join(dir, "bypass.txt"));
    const provider = await buildExtraMountProvider({
      guestPath: "/root/.config/gh",
      hostPath: dir,
      readonly: true,
      allowedFiles: ["/hosts.yml"],
    });

    // Direct access to the symlink target is blocked (sibling shadow).
    await assert.rejects(() => provider.stat("/state.json"));
    // And the symlink itself resolves through realpath into the
    // shadowed target, so reading via the link is also blocked.
    await assert.rejects(
      () => provider.open("/bypass.txt", "r"),
      "symlink to shadowed target must also be blocked",
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
