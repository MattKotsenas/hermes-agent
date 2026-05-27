# Hermes Gondolin Terminal Backend — Design

**Status:** phase 2 implementation complete; pre-PR cleanup
(2026-05-26)
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
- ~~Streaming stdout/stderr from in-VM commands.~~ **Closed
  (2026-05-26)** — Gondolin's `vm.exec()` is async-iterable in
  addition to awaitable. Wired into the daemon as a separate
  `exec_stream` RPC method (rpc.mjs grew a `streamWriter` arg threaded
  through handlers, daemon returns chunk frames before the final
  response), the Python wrapper supports `--stream`, and
  `GondolinEnvironment._run_bash` opts in by default (config:
  `stream: false` to disable). Open question (1) closed below.
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
exactly one Gondolin VM and exposes a length-prefixed msgpack JSON-RPC
protocol over an AF_UNIX socket:

```
host  -> [u32 BE: N][N bytes: msgpack{"id":1,"method":"exec","params":{...}}]
daemon -> [u32 BE: M][M bytes: msgpack{"id":1,"result":{"exit_code":0,...}}]
```

The wire is length-prefixed msgpack in both directions. msgpack carries
binary chunks natively (bin8/bin32), so stream output from
`vm.exec({stdout: "pipe"})` round-trips byte-for-byte — no base64 tax,
no UTF-8 corruption of non-text bytes. The 4-byte BE length prefix
eliminates the delimiter-scan that newline-delimited JSON requires and
keeps reassembly trivially correct across TCP segmentation. Both sides
enforce a 64 MiB cap on a single frame's payload as defence-in-depth
against a hostile or buggy peer.

Methods (minimum viable):
- `init(config)` — boots the VM with provided httpHooks/env/workspace.
- `exec(cmd, timeout_ms)` — runs a shell command, returns result.
- `exec_stream(cmd, timeout_ms)` — same shape but emits intermediate
  `{id, stream: {kind, data}}` frames as stdout/stderr chunks arrive
  from the VM, ending with a final `{id, result: {exit_code, ...}}`.
- `set_secret(name, value)` — updates a secret value without VM restart
  (for credential refresh, e.g. a re-issued AAD token).
- `shutdown()` — closes the VM.

The Python `GondolinEnvironment._run_bash` spawns a thin Python wrapper
subprocess (`gondolin-rpc-call <session_dir>`) per command. The wrapper:
1. Connects to the daemon's socket.
2. Sends the JSON-RPC `exec` (or `exec_stream`) request as one framed
   msgpack object.
3. Reads framed response(s); for `exec_stream`, decodes and writes
   each `{kind, data}` chunk to its respective pipe as it arrives.
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
  │                                  ├─ length-prefixed msgpack
  │                                  │  JSON-RPC over Unix socket
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
    # VM resources (forwarded to Gondolin's VMOptions.memory / .cpus).
    # Omit to use Gondolin's defaults (1G / 2 cpus). Dial down for many
    # concurrent sessions on a memory-constrained host: 256M + 1 cpu
    # fits ~30 VMs in 8 GB at the cost of slower in-guest builds.
    memory: null         # qemu syntax, e.g. "256M", "1G"
    cpus: null           # integer

    # Cap on the number of live Gondolin VMs in THIS process. Subagents,
    # the gateway, and a separate `hermes` CLI live in different processes
    # and don't share this counter — so this is not a host-wide guarantee
    # (cross-process locking is a planned follow-up). 0 = disabled.
    max_concurrent_vms: 0

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

1. ~~**Streaming.**~~ **Closed (2026-05-26)** — Gondolin's `vm.exec()`
   returns an `ExecProcess` that's both awaitable AND async-iterable.
   Wired up end-to-end:
   - `rpc.mjs` framing extended to support multi-frame responses:
     handlers now receive a second `ctx` arg with a `streamWriter` that
     writes `{stream: {kind, data}}` frames before the terminal
     `{result}` frame. Back-compat preserved — single-arg handlers
     ignore ctx.
   - Daemon `exec_stream(cmd, timeout_ms)` RPC iterates the
     `ExecProcess` and forwards each chunk. Stub-mode equivalent
     (`vm.execStreaming`) recognizes a `STREAM:` test marker for
     pure-JS tests.
   - Python `gondolin_rpc_call --stream` consumes stream frames and
     writes each chunk to its own stdout/stderr immediately, then
     exits with the final exit_code. `BaseEnvironment`'s `select()`
     drain loop sees real-time output without modification.
   - `GondolinEnvironment._run_bash` passes `--stream` by default;
     opt out with `terminal.gondolin.stream: false`.
   - Safety: CWD-marker prelude survives streaming because Gondolin
     chunks at coarse granularity (whole-write boundaries), not
     mid-byte. Watch for split-marker bugs if they surface.
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

   **Audited (2026-05-27).** See §Skill compatibility audit below.
   Net: 9 Category A (blocking) skills + 19 Category B (degraded
   credential lookup) skills + the long tail (Category C) which is
   unaffected. Neither category gates landing the gondolin backend
   on `main`. Both are tracked as follow-up work with the fix shape
   spelled out per-category in the audit section.
5. ~~**Memory cost at scale.**~~ **Closed (2026-05-26).** Two knobs
   landed:
   - Per-VM `terminal.gondolin.memory` (qemu syntax, e.g. `"256M"`)
     and `terminal.gondolin.cpus` (int). Defaults to Gondolin's own
     defaults (1G, 2 cpus); dialing memory down to 256M and cpus to
     1 brings the per-VM cost into a range where 30+ VMs fit in 8 GB.
   - `terminal.gondolin.max_concurrent_vms` — in-process cap that
     refuses to spawn another VM when the limit is reached. Set to
     0 (default) to disable entirely. Slot is released on cleanup and
     on init failure (including KeyboardInterrupt).

   **Sub-item cross-process locking:** ~~The cap above is in-process
   only.~~ **Closed (2026-05-26)** — `terminal.gondolin.lock_dir`
   (default `${HERMES_HOME}/sandboxes/gondolin/.locks/`) holds one
   file per slot; each VM acquires `fcntl.flock(LOCK_EX|LOCK_NB)` on
   the lowest free slot file. Kernel auto-releases on process exit,
   so PID-liveness checks aren't needed for stale-lock recovery. Cap
   now applies across subagents, gateway, and standalone CLI on the
   same host, all sharing the lock dir.

6. **`execute_code` (in-VM Python REPL).** The default
   `alpine-base:latest` image ships BusyBox without `python3`, so the
   built-in `execute_code` tool can't run inside the Gondolin VM. The
   error message in `tools/code_execution_tool.py::_execute_remote`
   now names this case explicitly and points at switching to a
   python-enabled image (e.g. `ubuntu-noble:latest` with python3
   layered in, or a custom-built image), and `hermes doctor` warns
   when `terminal.backend: gondolin` is configured. Test
   `test_execute_code_round_trip` is marked strict-xfail until a
   python-enabled image is selected. Bundling a python-bearing image
   as the default is a phase 3 question — adds ~50 MB to the
   download and a sharper "what's our default" debate.

## Where this goes after phase 2

- **Upstream PR** to `NousResearch/hermes-agent` with the diff above,
  citing this design doc as the design rationale.
- **Phase 3** (separate project): bring sbx into the same shape as a
  sibling backend, since the abstraction is now proven to work.
  Also: bundling a python-enabled default image so `execute_code`
  works out of the box.

## Skill compatibility audit (2026-05-27)

Snapshot of bundled skills (`skills/` + `optional-skills/`, 174 SKILL.md)
classified against the gondolin terminal backend. The audit method was a
pattern scan over every `SKILL.md`, then a manual triage of the hits.

### Category A — BLOCKING (9 skills)

Skill ships its own `scripts/` directory AND its `SKILL.md` instructs the
agent to invoke a script at a path that doesn't exist inside the guest
filesystem. Under `terminal.backend: gondolin` the cwd is `/workspace`
(workspace bind mount) and `~/.hermes/skills/` is host-only — the
referenced script is not present.

| Skill | Pattern |
|---|---|
| `productivity/maps` | `~/.hermes/skills/maps/scripts/maps_client.py` |
| `productivity/linear` | bare `python scripts/...` (assumes cwd = skill dir) |
| `productivity/powerpoint` | bare `python scripts/...` |
| `productivity/ocr-and-documents` | bare `python scripts/...` |
| `creative/comfyui` | bare `python scripts/...` |
| `creative/excalidraw` | `python skills/<name>/scripts/...` (also broken on host — separate bug) |
| `red-teaming/godmode` | bare `python scripts/...` |
| `research/arxiv` | bare `python scripts/...` |
| `OPT/health/fitness-nutrition` | bare `python scripts/...` |

**Fix shape (any of these works):**
1. Adopt the `SKILL_DIR/scripts/...` placeholder convention. Hermes
   substitutes the real path at prompt-injection time, and the agent
   can fetch the file via `skill_view(name=..., file_path=...)` and
   write it into `/workspace` before invoking. Several skills already
   use this pattern (e.g. `media/youtube-content`) and are clean.
2. Auto-mount `~/.hermes/skills/` into the guest at a stable path
   (e.g. `/etc/hermes/skills/`) at VM init. Requires extending the
   workspace-mount machinery to support multiple read-only mounts;
   not yet wired up.
3. Rewrite the script content inline into SKILL.md and have the agent
   `write_file` it on the fly. Workable for small scripts; doesn't
   scale.

(1) is the cleanest — it converges all backends on the same convention
and works equally on local, docker, ssh, modal, daytona, vercel, and
gondolin without per-backend special-casing.

### Category B — DEGRADES (19 skills)

Skill reads `~/.hermes/.env` directly to source a credential
(`GITHUB_TOKEN`, `OPENAI_API_KEY`, etc.). The host file isn't present
inside the guest, so the credential lookup silently falls through to
"no auth" or empty-string and the skill proceeds without telling the
agent what failed.

| Skill | Notes |
|---|---|
| `github/github-auth` | `AUTH_METHOD=*** fallthrough — the (b) case from the original audit deferral |
| `github/github-code-review`, `github/github-issues`, `github/github-pr-workflow`, `github/github-repo-management` | each sources `GITHUB_TOKEN` via the same pattern |
| `productivity/airtable`, `productivity/notion`, `productivity/teams-meeting-pipeline` | API tokens from `.env` |
| `media/gif-search` | Tenor API key |
| `devops/webhook-subscriptions` | webhook secrets |
| `autonomous-ai-agents/hermes-agent` | self-describes the `.env` location (low-impact, documentation context) |
| `OPT/creative/kanban-video-orchestrator`, `OPT/devops/watchers`, `OPT/productivity/canvas`, `OPT/productivity/shopify`, `OPT/productivity/siyuan`, `OPT/productivity/telephony`, `OPT/security/1password`, `OPT/software-development/rest-graphql-debug` | optional-skill counterparts |

**Fix shape:**
- Use the gondolin secret-injection plumbing
  (`terminal.gondolin.secrets:`) to wire-inject the credential at the
  HTTP layer for the relevant hosts (e.g. `api.github.com`).
  Credential never enters the guest; the skill's pre-flight
  `[ -f ~/.hermes/.env ] && export TOKEN=...` line becomes a no-op
  but the outbound request still bears the real token.
- For the github-skill family this is straightforward: a single
  `secrets.GITHUB_TOKEN` entry in `config.yaml` covers all five
  skills.
- The `AUTH_METHOD=*** branch in `github-auth/SKILL.md` should be
  taught about a third state — `AUTH_METHOD=*** for users on
  gondolin who've delegated auth to the wire-injection layer.

### Category C — IRRELEVANT (no terminal access required)

Skills that work via Hermes' own tools (MCP, browser, vision, mail,
calendar, Teams, web search, etc.) and never spawn a guest-side
process. They are unaffected by the terminal backend choice. Examples:
`dogfood`, `mail`, `calendar`, `teams`, `m365-copilot`, all browser-
driven skills, all MCP-based skills. No action needed.

### What the audit explicitly did NOT cover

- **Network policy hits.** Skills that hit external hosts not in the
  default allowlist (`pypi.org`, `registry.npmjs.org`, `github.com`,
  `dev.azure.com`, etc.) will get HTTP 403 under tightened policy
  modes. With the phase 2 default of `allowed_hosts: ["*"]` this is a
  non-issue out of the box. When a user tightens the allowlist, the
  doctor-time `policy_denied: true` sentinel surfaces the breakage
  on first invocation — no audit needed in advance.
- **Package-install instructions.** `apt install`, `brew install`,
  `pip install` etc. all work inside a gondolin VM (provided network
  is open). The default alpine image lacks `python` (open question
  6), which is bundled-image work tracked separately.
- **`docker run`-bearing skills.** `devops/docker-management`,
  `mlops/inference/vllm`, `research/blogwatcher`, etc. are
  inherently host-targeted; running them under gondolin is a category
  mismatch. The fix is "don't use those skills under gondolin," not a
  skill change.

### Disposition

- Track the **Category A** fixes as 9 follow-up commits, one per
  skill, applying fix shape (1) above. None of them block landing
  the gondolin backend on `main` — they're per-skill cleanups that
  improve cross-backend portability beyond gondolin too (the same
  fix makes them work under docker/modal/daytona/vercel which all
  also lack `~/.hermes/skills/` inside the sandbox).
- Track the **Category B** fixes as a single follow-up that wires
  up the recommended `secrets:` entries in a default config snippet
  documented under `terminal.gondolin.secrets:`. Land the
  github-skill SKILL.md update alongside it.
- Re-open the audit when gondolin lands on `main` and the actual
  user surface starts touching these skills in anger.

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
- **2026-05-26** — Open question (5) closed for the in-process
  case: per-VM `memory`/`cpus` knobs forwarded to Gondolin's
  `VMOptions`, plus in-process `max_concurrent_vms` cap in
  `GondolinEnvironment` that rejects over-cap construction and
  releases slots on cleanup or init failure. Cross-process locking
  (gateway + ad-hoc CLI in different PIDs) is filed as a follow-up;
  needs `${HERMES_HOME}/sandboxes/.gondolin.lock` + PID-liveness
  checks.
- **2026-05-26** — Operability batch:
  - `hermes sandboxes` CLI (list/prune subcommands) for the
    sandbox-dir cleanup story called out in §Filesystem.
  - Workspace bind-mount regression fixed: socket no longer lives
    inside the bind-mounted workspace dir (would have leaked the
    daemon socket into guest userspace). Moved to a sibling subdir.
  - `terminal.gondolin.image` knob added (forwarded to
    `SandboxServerOptions.imagePath`) so users can swap images
    without touching daemon code.
- **2026-05-26** — `execute_code` in-VM gap (open question 6) wired
  up: explicit error in `code_execution_tool._execute_remote`,
  `hermes doctor` info-line warning when backend is gondolin,
  strict-xfail on `test_execute_code_round_trip` until a
  python-enabled image is supplied.
- **2026-05-26** — Cross-process VM cap closed (5 sub-item):
  `fcntl.flock(LOCK_EX|LOCK_NB)` on per-slot files in
  `terminal.gondolin.lock_dir` (default
  `${HERMES_HOME}/sandboxes/gondolin/.locks/`). Kernel-released on
  process exit, so stale-lock recovery is free. `_GondolinSlot`
  abstraction wraps fd/lock_path. 5 new tests including
  cross-process subprocess scenarios.
- **2026-05-26** — Secret-resolution diagnostics surfaced. The
  daemon's `init` response now includes a `secretDiagnostics` array
  (`[{name, type, error, stderr}]`) populated from
  `resolveSecretWithDiagnostics()` in `hooks.mjs`; per-secret
  `timeout_ms` (default 30s) prevents hung `from_command` calls
  blocking VM init. Python `GondolinEnvironment.secret_diagnostics`
  exposes the list and WARN-logs each entry. 8 new tests across the
  Node and Python sides.
- **2026-05-26** — `SecretRefresher` shipped
  (`tools/environments/gondolin_secret_refresh.py`). Background
  thread per env; JWT exp parsing → ttl_seconds fallback → skip;
  10/30/60s exponential backoff on refresh failures. Opt-in via
  `refresh: true` per-secret (reuses `from_command`). Wired in
  post-init via `_start_secret_refresher_if_needed`, torn down in
  cleanup. 16 unit + 3 env wiring tests.
- **2026-05-26** — `hermes doctor` log-grep probe added
  (`hermes_cli/gondolin_log_scan.py`). Parses `errors.log` for
  recent "gondolin secret X (kind) unresolved/refresh failed"
  warnings, dedupes by (name, kind), shows age, trims stderr.
  Read-only — no VM spawn. 12 unit tests.
- **2026-05-26** — Streaming landed end-to-end (open question 1
  closed; see updated §Open questions). Three sub-cycles:
  rpc.mjs `streamWriter` framing (3 new tests, 8 total in
  `rpc.test.mjs`); daemon `exec_stream` RPC with stub-mode
  `vm.execStreaming` (2 new tests, 14 total in
  `socket_transport`); Python wrapper `--stream` flag (4 new
  tests, 13 total in `test_gondolin_rpc_call.py`). `_run_bash`
  passes `--stream` by default; `terminal.gondolin.stream: false`
  opts out. 33 commits on `feat/gondolin-terminal-backend`, all
  pushed to `fork`. Totals: 73 Python + 52 Node tests green.
- **2026-05-27** — Wire flipped from newline-delimited JSON to
  length-prefixed msgpack (closes the "Protocol revisit" follow-up
  in §Where this goes after phase 2). Both sides change in lockstep;
  no back-compat negotiation per Matt. Two motivations, measured:
  (a) binary safety — the streaming path used to `String(chunk)`
  Buffer output from `vm.exec({stdout: "pipe"})`, silently mangling
  non-UTF-8 bytes; msgpack carries `bin8/bin32` natively, so chunks
  now round-trip byte-for-byte; (b) throughput — `bench/protocol_compare.{py,mjs}`
  measured ~5-7x faster encode+decode on multi-MB binary payloads,
  ~3x faster on 1 MB text, ~1.34x smaller wire on every workload
  (no base64 expansion). One outlier: small line-oriented chunks
  under apt's older msgpack 1.0.3 are ~2 ms slower (invisible
  against a 300+ ms command). msgpack is declared as the
  `terminal.gondolin` extra in `pyproject.toml` + `LAZY_DEPS`, so
  non-gondolin users never pay for it (matches the modal/daytona/vercel
  pattern). 12 RPC tests + 14 socket tests + 13 wrapper tests +
  2 KVM integration tests + 8 sandbox/doctor/inventory ripple tests
  all green on the new wire.
- **2026-05-27** — Skill compatibility audit. Closes open question
  (4). Scanned all 174 bundled `SKILL.md` files for patterns that
  break or degrade under `terminal.backend: gondolin`: 9 Category A
  (blocking — bare `python scripts/...` or `~/.hermes/skills/...`
  references that don't resolve in the guest), 19 Category B
  (silent credential-lookup fallthrough on `~/.hermes/.env`), rest
  unaffected (Category C — pure MCP / browser / Hermes-tool
  skills). Fix shapes documented per-category; tracked as follow-up
  work, none gating the upstream PR. See §Skill compatibility
  audit for the full table.
