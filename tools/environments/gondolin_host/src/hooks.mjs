// Convert YAML-shaped policy config into Gondolin's createHttpHooks() input.
//
// Two pieces:
//   resolveSecret(cfg) → string | null   — resolves one secret entry
//   buildHooksInput(yaml) → { allowedHosts, secrets }   — full config
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
    secrets[name] = { hosts: [...cfg.hosts], value };
  }

  return { allowedHosts, secrets };
}
