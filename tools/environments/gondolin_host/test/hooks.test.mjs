// Test: hooks builder. Converts the YAML-shaped policy config into Gondolin's
// createHttpHooks() input. Pure functions, no VM.

import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveSecret, buildHooksInput } from "../src/hooks.mjs";

// ---- resolveSecret -----------------------------------------------------

test("resolveSecret: from_env reads process.env at call time", () => {
  process.env.TEST_TOKEN_X = "secret-from-env-123";
  try {
    const v = resolveSecret({ from_env: "TEST_TOKEN_X" });
    assert.equal(v, "secret-from-env-123");
  } finally {
    delete process.env.TEST_TOKEN_X;
  }
});

test("resolveSecret: from_env returns null when env var missing", () => {
  delete process.env.NONEXISTENT_TOKEN_QQQ;
  const v = resolveSecret({ from_env: "NONEXISTENT_TOKEN_QQQ" });
  assert.equal(v, null);
});

test("resolveSecret: from_command runs a command and returns trimmed stdout", () => {
  const v = resolveSecret({ from_command: "echo  secret-from-cmd-456  " });
  assert.equal(v, "secret-from-cmd-456");
});

test("resolveSecret: from_command returns null when the command exits non-zero", () => {
  const v = resolveSecret({ from_command: "false" });
  assert.equal(v, null);
});

test("resolveSecret: value (literal) returned as-is, ignoring other fields", () => {
  const v = resolveSecret({ value: "literal-789", from_env: "ANYTHING" });
  assert.equal(v, "literal-789");
});

test("resolveSecret: prefers value > from_env > from_command", () => {
  process.env.PREFER_TEST = "from-env-val";
  try {
    assert.equal(
      resolveSecret({ value: "lit", from_env: "PREFER_TEST", from_command: "echo cmd" }),
      "lit",
    );
    assert.equal(
      resolveSecret({ from_env: "PREFER_TEST", from_command: "echo cmd" }),
      "from-env-val",
    );
    assert.equal(
      resolveSecret({ from_command: "echo cmd-val" }),
      "cmd-val",
    );
  } finally {
    delete process.env.PREFER_TEST;
  }
});

test("resolveSecret: empty config returns null", () => {
  assert.equal(resolveSecret({}), null);
});

// ---- buildHooksInput ---------------------------------------------------
//
// buildHooksInput takes the raw YAML config and returns the argument that
// Gondolin's createHttpHooks() expects. We test the *shape* it produces
// (which matches Gondolin's documented input), not Gondolin's behavior.

test("buildHooksInput: default empty config = wide-open, no secrets", () => {
  const input = buildHooksInput({});
  assert.deepEqual(input.allowedHosts, ["*"]);
  assert.deepEqual(input.secrets, {});
});

test("buildHooksInput: allowed_hosts passes through", () => {
  const input = buildHooksInput({ allowed_hosts: ["a.example.com", "b.example.com"] });
  assert.deepEqual(input.allowedHosts, ["a.example.com", "b.example.com"]);
});

test("buildHooksInput: secrets are resolved to {hosts, value} shape", () => {
  process.env.HB_TOKEN = "real-token-abc";
  try {
    const input = buildHooksInput({
      secrets: {
        HB_TOKEN: { hosts: ["api.github.com"], from_env: "HB_TOKEN" },
      },
    });
    assert.deepEqual(input.secrets.HB_TOKEN, {
      hosts: ["api.github.com"],
      value: "real-token-abc",
    });
  } finally {
    delete process.env.HB_TOKEN;
  }
});

test("buildHooksInput: secrets with unresolved value are omitted (not undefined)", () => {
  delete process.env.NEVER_SET_QQQ;
  const input = buildHooksInput({
    secrets: {
      MISSING_TOKEN: { hosts: ["api.github.com"], from_env: "NEVER_SET_QQQ" },
      OK_TOKEN: { hosts: ["api.example.com"], value: "literal" },
    },
  });
  assert.equal(input.secrets.MISSING_TOKEN, undefined);
  assert.deepEqual(input.secrets.OK_TOKEN, { hosts: ["api.example.com"], value: "literal" });
});

test("buildHooksInput: multiple secrets all resolved independently", () => {
  process.env.HB_A = "aaa";
  process.env.HB_B = "bbb";
  try {
    const input = buildHooksInput({
      secrets: {
        A: { hosts: ["a"], from_env: "HB_A" },
        B: { hosts: ["b"], from_env: "HB_B" },
      },
    });
    assert.equal(input.secrets.A.value, "aaa");
    assert.equal(input.secrets.B.value, "bbb");
  } finally {
    delete process.env.HB_A;
    delete process.env.HB_B;
  }
});

test("buildHooksInput: malformed secret entry (missing hosts) is rejected", () => {
  assert.throws(
    () => buildHooksInput({ secrets: { BAD: { from_env: "X" } } }),
    /hosts/,
  );
});
