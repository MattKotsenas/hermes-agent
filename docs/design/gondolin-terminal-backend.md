# Hermes Gondolin Terminal Backend — Design

**Status:** draft for review (2026-05-25)
**Predecessor:** phase 1 spike at `../phase-1/` proved Gondolin runs in WSL2,
credential injection works, exfil is denied.

## Goals

1. A new `terminal.backend: gondolin` option for Hermes Agent.
2. The agent's terminal and file tools execute inside a Gondolin micro-VM.
3. The host controls network policy: deny by default, allowlist by config.
4. Real credentials live host-side and are injected on the wire only for
   allowed destinations. The guest never sees real tokens.
5. PR-shape diff that can land in `NousResearch/hermes-agent` upstream.

## Non-goals (this phase)

- Windows support. Linux/WSL2 only. Matt's Windows Gondolin fork stays
  out of scope.
- Streaming stdout/stderr from in-VM commands. Gondolin's `vm.exec` is
  request/response. Address in a follow-up if upstream adds streaming or
  we drop to lower-level APIs.
- VM-state snapshots across session resume. Resumed sessions get a fresh
  VM and an empty (or reused-by-sandbox-dir) workspace.
- Routing MCP servers through the VM. MCP servers run host-side, which
  is correct.
- Routing every Hermes tool through the VM. Only `terminal` + file tools
  go through; `memory`, `delegate_task`, `web_search`, `browser_*`,
  `vision_analyze`, `messaging`, `kanban_*` etc. stay host-side.

## Architecture

### The gap

Hermes's `BaseEnvironment._run_bash` returns a `subprocess.Popen` for
synchronous output capture and streaming. Gondolin's `vm.exec` is an
async TypeScript function returning `{exitCode, stdout, stderr}` after
the command completes. The shapes don't compose directly.

### The bridge: per-session `gondolin-host` daemon

Each Hermes session that uses `terminal.backend: gondolin` spawns a
small Node.js daemon (`gondolin-host`) at backend init. The daemon owns
exactly one Gondolin VM and exposes a stdio JSON-RPC line protocol:

```
host  -> { "id": 1, "method": "exec", "params": { "cmd": "ls -la", "timeout_ms": 60000 } }
daemon -> { "id": 1, "result": { "exit_code": 0, "stdout": "...", "stderr": "" } }
```

Methods (minimum viable):
- `init(config)` — boots the VM with provided httpHooks/env/workspace.
- `exec(cmd, timeout_ms)` — runs a shell command, returns result.
- `set_secret(name, value)` — updates a secret value without VM restart
  (for credential refresh, e.g. a re-issued AAD token).
- `shutdown()` — closes the VM.

The Python `GondolinEnvironment._run_bash` spawns a thin Python wrapper
subprocess (`gondolin-rpc-call <session_dir>`) per command. The wrapper:
1. Connects to the daemon's socket.
2. Sends the JSON-RPC `exec` request.
3. Receives the response.
4. Writes stdout/stderr to its own stdout/stderr and exits with the
   in-VM command's exit code.

From `BaseEnvironment`'s perspective, this is a normal subprocess — its
Popen handle, timeout machinery, and activity callbacks all work
unchanged. The wrapper is the bridge from "agent expects subprocess" to
"daemon owns VM."

```
Hermes (Python)
  ├─ AIAgent loop
  ├─ tools/environments/gondolin.py
  │   ├─ __init__ → spawns: node gondolin-host.mjs <session-dir>
  │   └─ _run_bash(cmd) → spawns: python -m gondolin_rpc_call <socket> <cmd>
  │                                  │
  │                                  ├─ JSON-RPC over Unix socket
  │                                  ↓
  └─ ~/.hermes/sandboxes/<session>/  daemon process
                                     ├─ @earendil-works/gondolin VM (QEMU)
                                     └─ httpHooks (allowlist + secrets)
```

Socket path: `${HERMES_HOME}/sandboxes/<session_id>/gondolin.sock`.

### Why not...

- **A Node-side CLI per call** (`npx gondolin exec ...`) — VM boots per
  command. ~16s per terminal call. Catastrophic.
- **Fork `BaseEnvironment` to async** — too invasive. Touches every
  existing backend and the agent's tool dispatch. Not warranted by one
  new backend.
- **Embed Node-in-Python via PyMiniRacer / nodejs-bin** — Gondolin is
  much more than just JS; it spawns QEMU, manages VFS, runs HTTP hooks
  in its own event loop. The daemon process is the right boundary.

## Lifecycle

### VM-per-what?

One VM per Hermes session. Same model as docker/ssh/modal.

| Spawn point | VM behavior |
|---|---|
| Fresh `hermes` interactive session | New VM, new sandbox dir, new daemon |
| `hermes --continue` / `--resume` | New VM. Sandbox dir reused (so files written previously are visible). Agent context describes what was done previously, but VM-internal state (installed packages, running processes) does NOT persist. |
| Cron-fired session | New VM per tick. Sandbox dir derived from session id. |
| `delegate_task` subagent | New VM. Isolation contract: subagents never share a VM with their parent. |
| Multiple concurrent gateway chats | One VM each. Memory cost: ~256-512 MB per VM. Cap unverified; treat as load-test item. |
| Worktree mode (`hermes -w`) | Each worktree gets its own VM, sandbox-dir keyed by worktree id. |

Config knob `terminal.gondolin.lifecycle` reserved for future
(`per-task`, `persistent`) but **not implemented in phase 2**. Ship
per-session only.

### Init flow

1. `AIAgent.__init__` resolves config; if `terminal.backend == "gondolin"`,
   constructs `GondolinEnvironment(...)`.
2. `GondolinEnvironment.__init__`:
   a. Validates qemu/qemu-utils available (fail-fast with clear error).
   b. Validates `/dev/kvm` accessible (warn but don't error; TCG would be
      too slow, but fail-fast on missing KVM is the right default).
   c. Resolves `sandbox_dir = ${HERMES_HOME}/sandboxes/<session_id>/`.
      Creates if missing.
   d. Spawns the daemon subprocess.
   e. Sends `init` with httpHooks config (allowlist + secrets, with
      values resolved from env via `from_env: GITHUB_TOKEN` indirection).
   f. Daemon boots VM (~1s), warms helper (~15s on first ever boot per
      machine; subsequent boots are faster because the helper bundle is
      cached). The daemon returns `init_complete` only after warmup.
3. `BaseEnvironment.init_session()` runs to capture env/cwd snapshots,
   same as every other backend. This is the first `_run_bash` call.

### Pre-warm option

Hermes already supports "start the backend before the user's first
message" patterns (some backends do this implicitly). For Gondolin's ~16s
warmup, surface this explicitly: if the gateway or CLI is starting fresh
and `terminal.backend: gondolin`, kick off the VM boot during session
construction so the user's first `terminal` call doesn't pay the cost.

Implementation: `GondolinEnvironment.__init__` returns after `init`
completes (synchronous). If the agent never makes a terminal call, the
VM still booted — a small waste, but predictable and correct.

### Cleanup

1. `cleanup()` sends `shutdown` to the daemon (graceful) with 5s timeout.
2. On timeout or daemon-already-dead: `kill()` the daemon subprocess.
3. Daemon's own signal handler tears down the QEMU process via
   `vm.close()`.
4. As a final safety net: a SIGTERM handler at the GondolinEnvironment
   level kills any stray qemu-system-x86_64 child PIDs tagged with our
   session id (label via QEMU's `-name guest=<session-id>`).

Orphaned-VM watchdog: `hermes doctor` should detect stale Gondolin VMs
older than N hours and offer to kill them.

## Filesystem

### Workspace mapping

The agent's "writable working directory" inside the VM is the host
directory `${HERMES_HOME}/sandboxes/<session_id>/`, exposed through
Gondolin's VFS layer.

- Inside VM: `/workspace` (or wherever `terminal.cwd` points).
- On host: `${HERMES_HOME}/sandboxes/<session_id>/`.
- `cwd` defaults to `/workspace`. Hermes's existing cwd-tracking
  machinery just works.

Implementation: at `init` time the daemon configures
`VMOptions.vfs.mounts = { [cwd]: new RealFSProvider(sandbox_dir) }`.
Gondolin's guest-side init script mounts the VFS provider tree over
sandboxfs (FUSE-on-virtio-serial) at `/data` and then `mount --bind`s
the configured guest path into the rest of the filesystem. The
result is a normal read-write directory inside the VM whose contents
live on the host — proven end-to-end by
`tests/integration/test_gondolin_terminal.py` (host→guest and
guest→host file visibility).

User opt-out: set `terminal.gondolin.workspace_mount: false`. The
daemon skips VFS wiring entirely — useful for power users whose
`policy_script` defines its own `vfs` block.

### Hermes runtime files

Hermes's own runtime (`~/.hermes/skills/`, `~/.hermes/state.db`,
`~/.hermes/config.yaml`, etc.) is NOT inside the sandbox. The agent
cannot reach it via file tools when `terminal.backend: gondolin`,
because file tools route through the environment.

This is correct: skills, memory, and session state are Hermes-managed
surfaces with their own tools (`skill_view`, `memory`, `session_search`)
that run host-side. The agent gets to them via dedicated tools, not via
filesystem.

Open question: does this break any existing skill that does
`read_file('~/.hermes/skills/foo/SKILL.md')`? Audit needed. Most skills
use `skill_view` which is host-side. If any skills directly access
`~/.hermes/`, they'll need updating or the user accepts that those
skills require `terminal.backend: local`.

### Persistence semantics

| Scenario | Workspace state |
|---|---|
| Within a session | Persists across terminal calls (it's a real bind mount) |
| Across `--resume` of same session id | Persists (same sandbox dir) |
| Different session id | New sandbox dir, fresh empty workspace |
| `hermes -w` worktree mode | Sandbox dir keyed by worktree id |
| `delegate_task` subagent | Own sandbox dir, isolated from parent |

Cleanup of sandbox dirs is its own concern. Hermes has no auto-cleanup
for them today. Add to `hermes doctor`: "N sandbox dirs older than X
days, total Y MB, run `hermes sandboxes prune` to clean."

## Network policy

### Default: open with credential isolation

The default is **`allowed_hosts: ["*"]`** — wide-open network egress.
The safety floor is credential isolation, not the allowlist:

- Without `secrets:` configured, no real tokens enter the VM. The agent
  can reach anywhere but has no privileged credentials to exfiltrate.
- With `secrets:` configured, the guest sees a placeholder; the real
  token is injected at the wire only for hosts that match the secret's
  `hosts:` list. If the agent tries to POST `$GITHUB_TOKEN` to
  `evil.com`, it sends the placeholder (`GONDOL...`), not the real
  token. **Credential isolation works even when the allowlist is wide-open.**

The allowlist becomes a second layer on top: configurable lockdown for
paranoid scenarios, not the default.

This optimizes for the "high productivity, still mostly safe" case
Matt called out. Default config supports `pip install`, `npm install`,
`git clone`, `gh api`, ADO REST calls, arbitrary npm/pypi registries,
docker registries, and the dozen ad-hoc hosts an agent actually needs,
without any user setup.

### Tightening the allowlist

```yaml
terminal:
  gondolin:
    allowed_hosts:
      - pypi.org
      - files.pythonhosted.org
      - registry.npmjs.org
      - github.com
      - api.github.com
      - dev.azure.com
```

Once the user sets `allowed_hosts` to a non-`["*"]` value, everything
not on the list is denied with a 403.

### Credential injection

```yaml
terminal:
  gondolin:
    secrets:
      GITHUB_TOKEN:
        hosts: [api.github.com, github.com]
        from_env: GITHUB_TOKEN
      AZURE_DEVOPS_TOKEN:
        hosts: [dev.azure.com, vssps.dev.azure.com]
        from_command: "az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv"
```

The `from_command` form lets the host refresh the credential as needed
(AAD tokens are ~1h). The daemon's `set_secret` RPC also lets external
code (a refresh loop) push a new value mid-session without restarting
the VM.

#### Multi-identity per host

A single user can have multiple credentials for the same host —
e.g. personal and work GitHub accounts both on `github.com`,
disambiguated on the host by `gh-cred-as` or similar. The in-VM
equivalent works naturally because Gondolin's `secretManager` keys
on the secret *name* (which becomes the guest env var name) and
swaps based on the *placeholder value* it sees in the outbound
request, not the host.

```yaml
terminal:
  gondolin:
    secrets:
      GITHUB_TOKEN_PERSONAL:
        hosts: [api.github.com, github.com]
        from_env: GITHUB_TOKEN_PERSONAL
        placeholder: { prefix: "GONDOL_GHP_", length: 24 }
      GITHUB_TOKEN_WORK:
        hosts: [api.github.com, github.com]
        from_env: GITHUB_TOKEN_WORK
        placeholder: { prefix: "GONDOL_GHW_", length: 24 }
```

Inside the guest, `$GITHUB_TOKEN_PERSONAL` and `$GITHUB_TOKEN_WORK`
are distinct placeholder strings. Whichever one a tool reads (and
bakes into its `Authorization` header) is what the wire hook
swaps. Tools that read `$GITHUB_TOKEN` directly (e.g. `gh`) need
a small shim — set `GITHUB_TOKEN=$GITHUB_TOKEN_PERSONAL` (or
`_WORK`) before invoking the tool, per the same per-identity
selection logic that lives in `gh-cred-as` host-side today.

#### Placeholder configuration

The `placeholder:` field on each secret accepts:

- **string** — used verbatim. Useful when you want a recognizable
  fingerprint in logs (e.g. `"PLACEHOLDER_FOR_GITHUB_PERSONAL"`).
- **object** `{ prefix?, suffix?, length, alphabet? }` — converted
  to a random generator via Gondolin's `makePlaceholderFunc`. The
  declarative shape is easier to read than a function and travels
  through YAML cleanly.
- **omitted** — Gondolin auto-generates a random placeholder. Fine
  for single-identity cases.

Explicit placeholders are only *required* for multi-identity, where
identical placeholders would collapse the routing — but they're a
useful safety hint in any case, since a known prefix makes
mis-injection obvious in logs (the agent sees `GONDOL_GHP_xxx`
where it expected a real token, vs. some random hex string).

### Escape hatch: `policy_script` (no BCF)

The YAML schema above covers the 80% case (list of allowed hosts + list
of secrets) but it's a strict subset of what Gondolin's `createHttpHooks`
TS API supports. Wrapping a programmable TS API in YAML is a Bespoke
Company Framework smell — every policy more sophisticated than
"host + secret" forces a Hermes patch.

The escape hatch: an optional `policy_script` config key pointing to a
JS/TS module that exports the hooks. When set, it takes precedence over
the YAML `allowed_hosts` / `secrets` config.

```yaml
terminal:
  gondolin:
    policy_script: ~/.hermes/gondolin-policy.mjs
```

```js
// ~/.hermes/gondolin-policy.mjs
//
// Power-user escape: write directly against the Gondolin TS API.
// Called once per VM init. The yaml param contains the simple YAML
// config (allowed_hosts, secrets) if any; ignore it if you don't want
// the simple inputs.
//
// Must export a `getHooks(yaml) => { httpHooks, env }` function.

import { createHttpHooks } from "@earendil-works/gondolin";

export function getHooks(yaml) {
  // Any Gondolin-supported policy goes here:
  //   - request-rewriting
  //   - per-path filters on allowed hosts
  //   - conditional secret injection
  //   - logging hooks
  //   - chained policies
  return createHttpHooks({
    allowedHosts: yaml.allowed_hosts ?? ["*"],
    secrets: yaml.secrets ?? {},
    // ... whatever the TS API supports, full surface available
  });
}
```

If `policy_script` is unset, the daemon uses a default implementation
that consumes the YAML directly:

```js
// gondolin-host.mjs internal default
function defaultGetHooks(yaml) {
  return createHttpHooks({
    allowedHosts: yaml.allowed_hosts ?? ["*"],
    secrets: Object.fromEntries(
      Object.entries(yaml.secrets ?? {}).map(([name, cfg]) => [
        name,
        { hosts: cfg.hosts, value: resolveSecret(cfg) },
      ])
    ),
  });
}
```

Effect:
- Simple users: never touch JS. YAML covers them.
- Power users: drop a `.mjs` file, write against Gondolin's TS API
  directly. Hermes is not in the way.
- The YAML schema is the high-level convenience layer; the TS API is
  the truth. No BCF.

### Denial UX

When the agent runs `curl https://denied.example.com` against a denied
host, Gondolin denies the request and returns 403. The tool-result
returned to the agent should make clear this was a **policy denial**,
not a network error. Path:

1. The daemon detects the denial (via Gondolin's response header or
   metadata) and includes a `policy_denied: true` flag in the JSON-RPC
   `exec` response.
2. The Python-side wrapper annotates the tool result with the flag.
3. Hermes adds a hint to the system prompt when `terminal.backend:
   gondolin` AND `allowed_hosts != ["*"]`: "If you see HTTP 403 from a
   host, verify whether the host is on the allowlist."

When `allowed_hosts: ["*"]` (the default), there are no policy denials
to worry about; this UX only matters in tightened mode.

## Tool routing

Hermes's `agent/prompt_builder.py::build_environment_hints` checks
`_REMOTE_TERMINAL_BACKENDS`. **Add `"gondolin"` to that set.** That makes
file tools route through the environment and suppresses host-info hints
in the system prompt.

Per the existing convention:

| Tool category | Routes through env? |
|---|---|
| `terminal` | Yes |
| `read_file`, `write_file`, `patch`, `search_files` | Yes |
| `memory`, `session_search` | No (host-side) |
| `delegate_task` | No (spawns new agent; new agent gets own env) |
| `web_search`, `browser_*`, `vision_analyze` | No (host-side, has own controls) |
| `image_gen`, `video_gen`, `tts` | No |
| `cronjob`, `clarify`, `send_message`, `kanban_*` | No |
| `skill_view`, `skills_list`, `skill_manage` | No |
| MCP tools (`mcp_*`) | No (MCP servers are host-side Hermes subprocesses) |

This is consistent with docker/ssh/modal backends already.

## Config schema

Full shape:

```yaml
terminal:
  backend: gondolin
  cwd: /workspace        # in-VM path; defaults to /workspace
  timeout: 180           # per-command, seconds
  gondolin:
    # VM resources
    memory_mb: 512       # per VM
    cpu_count: 2

    # Sandbox dir override (default: ${HERMES_HOME}/sandboxes/<session>/)
    sandbox_dir: null

    # Network policy — default is open egress with credential isolation
    allowed_hosts: ["*"]    # tighten by listing specific hosts

    # Secrets (host-side values, swapped in at the wire)
    secrets:
      # GITHUB_TOKEN:
      #   hosts: [api.github.com, github.com]
      #   from_env: GITHUB_TOKEN
      # AZURE_DEVOPS_TOKEN:
      #   hosts: [dev.azure.com]
      #   from_command: "az account get-access-token --resource ... --query accessToken -o tsv"

    # Escape hatch: point to a JS/TS module that returns httpHooks directly,
    # bypassing the YAML schema. Lets power users write against the full
    # Gondolin TS API without a Hermes patch.
    policy_script: null   # e.g. ~/.hermes/gondolin-policy.mjs

    # Lifecycle (reserved; only per-session in phase 2)
    lifecycle: per-session

    # Daemon
    daemon_path: null    # override path to gondolin-host.mjs
    boot_timeout_ms: 30000   # fail VM init if it takes longer

    # VM image
    #
    # null  →  let Gondolin use its own default (GONDOLIN_DEFAULT_IMAGE,
    #          currently 'alpine-base:latest'). Boots fast, BusyBox tools.
    # str   →  forwarded to SandboxServerOptions.imagePath. Accepts:
    #            - registry selector ('name:tag' or build id; resolved via
    #              builtin-image-registry.json and cached under
    #              ~/.cache/gondolin/images/)
    #            - directory path containing kernel/initrd/rootfs assets
    #              built with `gondolin build`
    # Examples: 'alpine-base:latest', 'ubuntu-noble:latest',
    #           '/home/me/.gondolin-builds/my-stack/'
    #
    # The daemon always invokes the user's command via 'bash -c' so any
    # image with bash on PATH works regardless of /bin/sh choice.
    image: null
```

## Acceptance criteria (gated phase 2 close)

**Functional (blocking):**
1. `terminal.backend: gondolin` + fresh session → first `terminal` call
   succeeds, returns output.
2. `read_file`/`write_file`/`patch`/`search_files` operate on the
   workspace inside the VM.
3. Files written in session N visible in session N if `--resume`-d with
   same id.
4. Files in session N invisible from session M (parallel isolation).
5. `gh api user` returns real user JSON (credential injected).
6. `curl https://example.com` (not on allowlist) is denied.
7. Adding a host to `allowed_hosts` makes it reachable without code
   changes.
8. `cwd` persists across terminal calls.
9. `env` snapshot persists across calls.
10. `delegate_task` subagent gets its own VM, isolated from parent.

**Hermes integration (blocking):**
11. `agent/prompt_builder.py` includes `"gondolin"` in
    `_REMOTE_TERMINAL_BACKENDS` and emits a per-backend probe.
12. System prompt tells the agent it's in a sandbox + names the allowlist
    + explains 403-means-policy.
13. `hermes doctor` checks qemu/qemu-utils + /dev/kvm + Gondolin npm
    package install when `backend: gondolin`.
14. Config validation rejects invalid `allowed_hosts` / `secrets` shapes
    at startup, not at first tool call.
15. `cleanup()` reliably stops the VM and daemon. No orphaned
    qemu-system-x86_64 after `hermes` exits cleanly.

**Failure modes (blocking):**
16. QEMU missing → clear error at backend init, names the apt package
    to install.
17. KVM unavailable → clear error; recommend `terminal.backend: docker`
    as fallback.
18. VM crashes mid-session → next tool call returns tool error,
    conversation continues; Hermes does NOT abort.
19. Allowlist denial → tool result annotated as policy denial so the
    agent can distinguish from network error.

**Performance (characterize, don't block):**
20. Cold session-start overhead < 20s on a developer laptop.
21. Subsequent terminal call overhead < 200ms vs local.
22. File I/O for typical workloads within 2x of local.

**Operational (characterize, don't block):**
23. MCP servers (agency, custom) still work — no regression.
24. Worktree mode (`hermes -w`) gets isolated VMs.
25. Profile isolation works (each profile → own sandbox tree).

## File layout (the phase 2 PR shape)

```
hermes-agent/
├── tools/environments/
│   ├── gondolin.py                       # new: GondolinEnvironment
│   ├── gondolin_host.mjs                 # new: TS daemon, runs alongside hermes
│   └── gondolin_rpc_call.py              # new: the Popen-shaped bridge
├── agent/prompt_builder.py               # patch: add "gondolin" to _REMOTE_TERMINAL_BACKENDS
├── hermes_cli/config.py                  # patch: config schema for terminal.gondolin
├── hermes_cli/commands.py                # patch: `hermes doctor` checks for gondolin
└── tests/tools/environments/
    └── test_gondolin.py                  # new: smoke + acceptance suite
```

Plus, in `~/.hermes/.env` documentation: note that
`GITHUB_TOKEN` / `AZURE_DEVOPS_TOKEN` will be read host-side and
injected into the sandbox if configured.

## Open questions tracked into phase 2

1. **Streaming.** Gondolin `vm.exec` is request-response. For long
   commands, output arrives all at once. Three paths:
   - Accept it. Most agent terminal calls finish in <2s.
   - Investigate Gondolin's lower-level TS APIs (`host/` package) for a
     streaming interface.
   - Petition upstream Gondolin for streaming.
   Plan: ship with no streaming, file an upstream issue, revisit when
   it bites.
2. ~~**VFS bind-mount.**~~ **Closed (2026-05-26)** — Gondolin exposes
   host-dir mounts as a first-class API (`VMOptions.vfs.mounts` +
   `RealFSProvider`). Wired up; round-trip file visibility verified
   in `tests/integration/test_gondolin_terminal.py`. No file-sync
   fallback needed.
3. ~~**Credential refresh + env-var binding + multi-identity.**~~
   **Closed (2026-05-26).** Gondolin's `createHttpHooks` already
   exposes everything we need: `SecretDefinition.placeholder`
   (string or generator), per-secret env-var binding via the secrets
   map (name == guest env var), `SecretManager.updateSecret` for
   mid-session refresh, and value-keyed routing that makes
   multi-identity-per-host work without any custom hook code.
   - `from_command` already supported by `buildHooksInput`.
   - Daemon's `set_secret` RPC now routes through `secretManager`.
   - YAML `placeholder:` field accepted as string or
     `{ prefix?, suffix?, length, alphabet? }` (mapped to
     `makePlaceholderFunc`).
   - Multi-identity is just two `secrets:` entries with the same
     hosts and distinct names+placeholders; the guest sees them as
     distinct env vars (e.g. `GITHUB_TOKEN_PERSONAL` vs
     `GITHUB_TOKEN_WORK`).
4. **Skills that touch `~/.hermes/` from inside the VM.** Audit the
   bundled skills. Any that do `read_file('~/.hermes/skills/...')`
   directly will break under gondolin backend. Either patch those
   skills to use `skill_view`, or document the incompatibility.

   **Deferred to backend-switch time (2026-05-26).** Two broad
   categories surface today: (a) skill-bundled scripts referenced
   as `~/.hermes/skills/<name>/scripts/...` which won't be present
   in the guest filesystem, and (b) the github-skill family's
   `~/.hermes/.env` bootstrap which silently falls through to
   `AUTH_METHOD=none` instead of relying on wire-injection. Both
   are blocked on (3) landing: (a) needs a story for how skill
   scripts get into the guest (auto-mounted under
   `/etc/hermes/skills/`? rewritten in the system prompt? individual
   per-skill copy?), and (b) needs env-var binding + multi-identity
   so the github bootstrap can produce `AUTH_METHOD=gh` with a
   wire-injected placeholder. Re-open this item alongside (3).
5. **Memory cost at scale.** Each VM is ~256-512 MB. A gateway hosting
   10 concurrent chats would use 2.5-5 GB. Not crippling on a
   developer laptop, but worth a config cap (`max_concurrent_vms`) and
   documented memory budget.

## Where this goes after phase 2

- **Upstream PR** to `NousResearch/hermes-agent` with the diff above,
  citing this design doc as the design rationale.
- **Phase 2.5** if PR feedback wants it: `from_command` secrets,
  streaming via low-level Gondolin APIs, memory caps.
- **Phase 3** (separate project): bring sbx into the same shape as a
  sibling backend, since the abstraction is now proven to work.

## Activity log

- **2026-05-25** — Design doc drafted. Predecessor phase 1 spike at
  `../phase-1/` proved the technical basis.
- **2026-05-26** — Phase 2 landed. Daemon (RPC framing, hooks builder,
  policy_script, VM lifecycle), AF_UNIX socket transport, Python RPC
  bridge, GondolinEnvironment, prompt_builder registration,
  terminal_tool factory wiring, doctor check, KVM integration tests,
  configurable VM image via `terminal.gondolin.image`. 15 commits on
  `feat/gondolin-terminal-backend`.
- **2026-05-26** — Workspace bind-mount wired up via
  `VMOptions.vfs.mounts` + `RealFSProvider`. Host
  `${HERMES_HOME}/sandboxes/<session>/` is now visible inside the VM
  at the configured cwd (default `/workspace`), read-write, with
  round-trip file visibility verified by KVM integration tests.
  Closes open question (2) from phase 2.
- **2026-05-26** — Skill audit (open question 4) deferred to land
  alongside open question 3. Two real breakages identified
  (skill-bundled script paths, github `.env` bootstrap), both
  blocked on env-var binding + multi-identity wire injection.
- **2026-05-26** — Open question (3) closed. Gondolin's
  `createHttpHooks` API already supports per-secret placeholders
  (string or generator), env-var binding via secrets map keys,
  `secretManager.updateSecret` for mid-session refresh, and
  value-keyed wire-routing for multi-identity-per-host. Wired up:
  `hooks.mjs` threads `placeholder:` through (string or
  `{prefix?, suffix?, length, alphabet?}`); daemon `set_secret`
  RPC routes to `secretManager`; Python env exposes
  `set_secret(name, value=..., hosts=...)`. Multi-identity works
  with two `secrets:` entries having the same hosts and distinct
  names+placeholders. Skill audit (4) can now proceed against this
  shape: the github `.env` bootstrap will see distinct env vars
  per identity (`GITHUB_TOKEN_PERSONAL`, `GITHUB_TOKEN_WORK`) and
  pick one explicitly.
