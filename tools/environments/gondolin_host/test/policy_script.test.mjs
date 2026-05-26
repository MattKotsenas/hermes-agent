// Test: policy_script escape hatch.
//
// When the daemon is given a policy_script path, it imports that file
// and calls its exported getHooks(yaml) function. The return value
// replaces the default buildHooksInput output entirely.
//
// The function we test (`loadPolicy`) returns a callable that, given the
// yaml config, returns the createHttpHooks() input. Default path: use
// buildHooksInput. policy_script path: use the user module.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { loadPolicy } from "../src/hooks.mjs";

async function tmpFile(content, ext = ".mjs") {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "gondolin-policy-"));
  const p = path.join(dir, `policy${ext}`);
  await fs.writeFile(p, content);
  return p;
}

test("loadPolicy: no path → uses default builder", async () => {
  const policy = await loadPolicy(null);
  const out = await policy({});
  assert.deepEqual(out.allowedHosts, ["*"]);
  assert.deepEqual(out.secrets, {});
});

test("loadPolicy: with path → imports module and uses its getHooks", async () => {
  const p = await tmpFile(`
    export function getHooks(yaml) {
      return {
        allowedHosts: ["custom.example.com"],
        secrets: { CUSTOM: { hosts: ["x"], value: yaml.marker ?? "default" } },
      };
    }
  `);
  const policy = await loadPolicy(p);
  const out = await policy({ marker: "from-yaml" });
  assert.deepEqual(out.allowedHosts, ["custom.example.com"]);
  assert.equal(out.secrets.CUSTOM.value, "from-yaml");
});

test("loadPolicy: policy_script's getHooks may be async", async () => {
  const p = await tmpFile(`
    export async function getHooks(yaml) {
      await new Promise((r) => setTimeout(r, 5));
      return { allowedHosts: ["a"], secrets: {} };
    }
  `);
  const policy = await loadPolicy(p);
  const out = await policy({});
  assert.deepEqual(out.allowedHosts, ["a"]);
});

test("loadPolicy: missing module file throws a clear error", async () => {
  await assert.rejects(
    () => loadPolicy("/nonexistent/policy-file.mjs"),
    /policy_script/,
  );
});

test("loadPolicy: module without getHooks export throws a clear error", async () => {
  const p = await tmpFile(`export const notHooks = 1;`);
  await assert.rejects(
    () => loadPolicy(p),
    /getHooks/,
  );
});
