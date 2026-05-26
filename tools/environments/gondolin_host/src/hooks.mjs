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

export function resolveSecret(cfg) {
  if (!cfg || typeof cfg !== "object") return null;

  if (cfg.value != null) return String(cfg.value);

  if (cfg.from_env) {
    const v = process.env[cfg.from_env];
    return v == null || v === "" ? null : v;
  }

  if (cfg.from_command) {
    try {
      const out = execSync(cfg.from_command, {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
        timeout: 30_000,
      });
      const trimmed = out.trim();
      return trimmed === "" ? null : trimmed;
    } catch {
      return null;
    }
  }

  return null;
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
  for (const [name, cfg] of Object.entries(yaml.secrets ?? {})) {
    if (!cfg || !Array.isArray(cfg.hosts) || cfg.hosts.length === 0) {
      throw new Error(`secret ${name}: missing required 'hosts' array`);
    }
    const value = resolveSecret(cfg);
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

  return { allowedHosts, secrets };
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
