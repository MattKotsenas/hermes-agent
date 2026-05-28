// Convert YAML-shaped policy config into Gondolin's createHttpHooks() input.
//
// Three exports:
//   resolveSecret(cfg) → string | null   — resolves one secret entry
//   buildHooksInput(yaml) → { allowedHosts, secrets }   — default builder
//   loadPolicy(path) → async (yaml) → { allowedHosts, secrets }
//                                        — returns a callable that uses
//                                          either buildHooksInput (path null)
//                                          or the user module's getHooks
//
// Resolution precedence (highest first):
//   value          - literal string in the config
//   from_env       - process.env[NAME]
//   from_command   - shell command, trimmed stdout
//
// A secret that can't be resolved (env unset, command failed) is OMITTED
// from the output rather than passed as null/undefined. That way the agent
// can still run; the credential just isn't injected. Misconfiguration is
// surfaced in daemon logs, not by crashing the VM.

import { execSync } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { makePlaceholderFunc } from "@earendil-works/gondolin";

// Resolve a single secret config to either a value (string) or a structured
// diagnostic describing why it couldn't be resolved. The diagnostic includes
// the secret name, the resolver type that was attempted, an error message,
// and (for from_command) the captured stderr. Callers that don't care about
// diagnostics can use the simpler resolveSecret() wrapper.
export function resolveSecretWithDiagnostics(name, cfg) {
  if (!cfg || typeof cfg !== "object") {
    return { value: null, diagnostic: { name, type: "none", error: "no secret config" } };
  }

  if (cfg.value != null) return { value: String(cfg.value), diagnostic: null };

  if (cfg.from_env) {
    const v = process.env[cfg.from_env];
    if (v == null || v === "") {
      return {
        value: null,
        diagnostic: {
          name,
          type: "from_env",
          error: `env var ${cfg.from_env} is unset or empty`,
        },
      };
    }
    return { value: v, diagnostic: null };
  }

  if (cfg.from_command) {
    const timeoutMs = typeof cfg.timeout_ms === "number" && cfg.timeout_ms > 0
      ? cfg.timeout_ms
      : 30_000;
    // SECURITY: captured stderr/stdout from a failing resolver may contain
    // partial secrets — a token half-written before exit, a JWT echoed in
    // a verbose error, the raw response body from a misconfigured auth
    // endpoint. They DO NOT enter the diagnostic by default. Set
    // HERMES_GONDOLIN_DEBUG_SECRETS=1 to opt in when debugging a broken
    // resolver. The flag is host-side only and never propagated to the
    // guest.
    const debugCapture = process.env.HERMES_GONDOLIN_DEBUG_SECRETS === "1";
    let stdout;
    try {
      stdout = execSync(cfg.from_command, {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
        timeout: timeoutMs,
      });
    } catch (err) {
      // execSync attaches stderr/stdout/status/signal on the error object.
      const stderr = err.stderr != null ? String(err.stderr).trim() : "";
      const out = err.stdout != null ? String(err.stdout).trim() : "";
      let msg;
      if (err.code === "ETIMEDOUT" || err.signal === "SIGTERM") {
        msg = `command timed out after ${timeoutMs}ms`;
      } else if (typeof err.status === "number") {
        msg = `command exited with code ${err.status}`;
      } else if (err.signal) {
        msg = `command killed by signal ${err.signal}`;
      } else {
        msg = `command failed: ${err.message}`;
      }
      const diagnostic = {
        name,
        type: "from_command",
        error: msg,
      };
      if (debugCapture) {
        diagnostic.stderr = stderr;
        diagnostic.stdout = out;
      }
      return { value: null, diagnostic };
    }
    const trimmed = stdout.trim();
    if (trimmed === "") {
      const diagnostic = {
        name,
        type: "from_command",
        error: "command exited 0 but produced empty output",
      };
      if (debugCapture) {
        diagnostic.stderr = "";
        diagnostic.stdout = "";
      }
      return { value: null, diagnostic };
    }
    return { value: trimmed, diagnostic: null };
  }

  return {
    value: null,
    diagnostic: { name, type: "none", error: "secret has no value/from_env/from_command" },
  };
}

export function resolveSecret(cfg) {
  // Backward-compat wrapper: just the value, no diagnostic.
  return resolveSecretWithDiagnostics(null, cfg).value;
}

// Resolve a YAML-shaped placeholder value into the form Gondolin expects.
//
// Accepts:
//   undefined → undefined (Gondolin auto-generates a random placeholder)
//   string    → string (verbatim)
//   object    → generator function via makePlaceholderFunc({prefix, length, alphabet})
//
// Throws on malformed object form (missing `length`) so misconfig surfaces
// at init time rather than from inside Gondolin's hook machinery.
export function resolvePlaceholder(secretName, ph) {
  if (ph == null) return undefined;
  if (typeof ph === "string") return ph;
  if (typeof ph === "object") {
    if (typeof ph.length !== "number" || ph.length <= 0) {
      throw new Error(
        `secret ${secretName}: placeholder object requires a positive 'length' (got ${ph.length})`,
      );
    }
    return makePlaceholderFunc({
      prefix: ph.prefix,
      suffix: ph.suffix,
      length: ph.length,
      alphabet: ph.alphabet,
    });
  }
  throw new Error(
    `secret ${secretName}: placeholder must be a string or {prefix?, length, alphabet?} object`,
  );
}

export function buildHooksInput(yaml = {}) {
  const allowedHosts = Array.isArray(yaml.allowed_hosts) && yaml.allowed_hosts.length
    ? [...yaml.allowed_hosts]
    : ["*"];

  const secrets = {};
  const secretDiagnostics = [];
  for (const [name, cfg] of Object.entries(yaml.secrets ?? {})) {
    if (!cfg || !Array.isArray(cfg.hosts) || cfg.hosts.length === 0) {
      throw new Error(`secret ${name}: missing required 'hosts' array`);
    }
    const { value, diagnostic } = resolveSecretWithDiagnostics(name, cfg);
    if (diagnostic) secretDiagnostics.push(diagnostic);
    if (value == null) {
      // Skip unresolved secrets — agent runs without that credential.
      continue;
    }
    const entry = { hosts: [...cfg.hosts], value };
    const placeholder = resolvePlaceholder(name, cfg.placeholder);
    if (placeholder !== undefined) {
      entry.placeholder = placeholder;
    }
    secrets[name] = entry;
  }

  return { allowedHosts, secrets, secretDiagnostics };
}

export async function loadPolicy(scriptPath) {
  if (!scriptPath) {
    return async (yaml) => buildHooksInput(yaml);
  }

  let mod;
  try {
    const url = pathToFileURL(path.resolve(scriptPath)).href;
    mod = await import(url);
  } catch (e) {
    throw new Error(`failed to load policy_script ${scriptPath}: ${e.message}`);
  }

  if (typeof mod.getHooks !== "function") {
    throw new Error(`policy_script ${scriptPath} must export a getHooks(yaml) function`);
  }

  return async (yaml) => mod.getHooks(yaml);
}
