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

// ---- placeholder pass-through ------------------------------------------
//
// Gondolin's createHttpHooks() accepts a `placeholder` field on each secret
// (string or generator function). When the guest reads a secret env var it
// sees the placeholder; the wire hook swaps the placeholder for the real
// value on outbound requests whose host matches. Without an explicit
// placeholder Gondolin auto-generates one — that's still fine for the
// single-identity case but useless when two secrets target the same host
// (multi-identity) because the guest needs distinct env-var values that
// the wire hook can route on.

test("buildHooksInput: explicit placeholder string is passed through verbatim", () => {
  const input = buildHooksInput({
    secrets: {
      GH_PERSONAL: {
        hosts: ["github.com"],
        value: "real-personal-pat",
        placeholder: "GONDOL_GH_PERSONAL_xxxxxxxx",
      },
    },
  });
  assert.deepEqual(input.secrets.GH_PERSONAL, {
    hosts: ["github.com"],
    value: "real-personal-pat",
    placeholder: "GONDOL_GH_PERSONAL_xxxxxxxx",
  });
});

test("buildHooksInput: placeholder object becomes a generator function", () => {
  // Gondolin accepts either a string or a () => string. The config-shape we
  // expose is the declarative { prefix, length, alphabet } object — we
  // convert it to a function here using makePlaceholderFunc.
  const input = buildHooksInput({
    secrets: {
      GH_WORK: {
        hosts: ["github.com"],
        value: "real-work-token",
        placeholder: { prefix: "GHW_", length: 12, alphabet: "abcdef0123456789" },
      },
    },
  });
  const ph = input.secrets.GH_WORK.placeholder;
  assert.equal(typeof ph, "function", "placeholder object should produce a generator function");
  const generated = ph();
  assert.match(generated, /^GHW_[a-f0-9]{12}$/);
});

test("buildHooksInput: multi-identity — two secrets, same host, distinct placeholders", () => {
  // The actual disambiguation happens inside Gondolin (it looks at which
  // placeholder appears in the outbound request and swaps for the matching
  // real value). Here we just confirm the builder lets you define two
  // entries pointing at the same host without one clobbering the other.
  const input = buildHooksInput({
    secrets: {
      GITHUB_TOKEN_PERSONAL: {
        hosts: ["github.com", "api.github.com"],
        value: "personal-pat",
        placeholder: "GONDOL_GHP_AAAA",
      },
      GITHUB_TOKEN_WORK: {
        hosts: ["github.com", "api.github.com"],
        value: "work-pat",
        placeholder: "GONDOL_GHW_BBBB",
      },
    },
  });
  assert.equal(input.secrets.GITHUB_TOKEN_PERSONAL.value, "personal-pat");
  assert.equal(input.secrets.GITHUB_TOKEN_PERSONAL.placeholder, "GONDOL_GHP_AAAA");
  assert.equal(input.secrets.GITHUB_TOKEN_WORK.value, "work-pat");
  assert.equal(input.secrets.GITHUB_TOKEN_WORK.placeholder, "GONDOL_GHW_BBBB");
  // Both must keep their full host list; no implicit dedup-or-merge across
  // entries.
  assert.deepEqual(
    input.secrets.GITHUB_TOKEN_PERSONAL.hosts,
    ["github.com", "api.github.com"],
  );
});

test("buildHooksInput: secret without explicit placeholder omits the field (Gondolin auto-generates)", () => {
  // Single-identity case: don't force users to spell out a placeholder for
  // every secret. Gondolin's createHttpHooks generates a random one when
  // `placeholder` is undefined.
  process.env.HB_PHDEF = "real";
  try {
    const input = buildHooksInput({
      secrets: { HB_PHDEF: { hosts: ["x.example.com"], from_env: "HB_PHDEF" } },
    });
    assert.equal(input.secrets.HB_PHDEF.placeholder, undefined);
  } finally {
    delete process.env.HB_PHDEF;
  }
});

test("buildHooksInput: malformed placeholder object (no length) is rejected", () => {
  // Object form requires `length`. Catch the typo early instead of letting
  // a confusing makePlaceholderFunc error surface from inside Gondolin.
  assert.throws(
    () => buildHooksInput({
      secrets: {
        BAD: {
          hosts: ["x"],
          value: "v",
          placeholder: { prefix: "P_" },  // missing length
        },
      },
    }),
    /placeholder|length/,
  );
});
