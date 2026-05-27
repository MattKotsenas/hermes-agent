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

`~/.hermes/skills/` and individual credential files (OAuth tokens,
session DBs, CLI config) ARE projected into the guest, read-only, at the
same paths docker/singularity use (`/root/.hermes/skills/`, etc.). This
matches the cross-sandbox convention defined by
`tools/credential_files.py`:

- **`get_skills_directory_mount()`** returns the host paths for
  `~/.hermes/skills/` and any external skill directories the user has
  configured. Gondolin mounts each as a `ReadonlyProvider(RealFSProvider)`
  at the matching guest path. Symlinks are sanitized into a temp copy by
  `tools/credential_files.py` before mounting, so a malicious symlink in
  the skills tree can't exfil arbitrary host files.
- **`get_credential_file_mounts()`** returns individual credential files
  registered by skills (Google OAuth, 1Password CLI session, `gh` auth
  config, etc.) or declared by the user in `terminal.credential_files:`.

  Gondolin's `RealFSProvider` takes a directory `rootPath`, not a single
  file — so unlike docker's `-v $f:$g:ro`, we can't mount a credential
  file directly. The Python wrapper groups credential files by their
  guest parent directory and mounts that parent read-only. Most
  credential files already live in dedicated config dirs
  (`~/.config/gcloud/`, `~/.config/gh/`, `~/.op/`) so this works out
  cleanly. If two credentials share a guest parent path but resolve to
  different host parents, the second is skipped with a WARN — the user
  can route that credential via wire-injected `secrets:` instead.

The writable workspace at `cwd` is unchanged: a `RealFSProvider` bind
mount (no readonly wrap) on the per-session sandbox subdir.

State that intentionally stays host-side (and is reachable only via the
respective Hermes-managed tools, not the guest filesystem):
`~/.hermes/state.db` (`session_search`), `~/.hermes/config.yaml`
(host-side config), `~/.hermes/memory.json` and friends (`memory` tool),
`~/.hermes/.env` (host environment / wire-injection source).

Config knobs:

- `terminal.gondolin.project_skills` (default `true`) — set false to
  suppress the skills-directory mount. Useful for paranoid setups where
  the sandbox should be as bare as possible.
- `terminal.gondolin.project_credentials` (default `true`) — set false
  to suppress credential-file projection.
- `terminal.gondolin.extra_mounts: [{guest_path, host_path, readonly}]`
  — explicit additional mounts for arbitrary host directories. Each
  entry is validated at init (path must exist, guest_path must be
  absolute); `readonly: true` is default, wrap in
  `ReadonlyProvider(RealFSProvider)` on the daemon side.

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

   **Closed (2026-05-27).** Originally framed as "audit and patch
   each skill"; on review, the actual gap was infrastructural — the
   gondolin backend was missing a piece every other sandbox backend
   already has. `tools/credential_files.py` exposes
   `get_skills_directory_mount()` and `get_credential_file_mounts()`,
   both already consumed by `docker.py`, `singularity.py`,
   `modal.py`, and `managed_modal.py` to bind-mount `~/.hermes/skills/`
   and individual credential files read-only into the sandbox.
   Gondolin was wired for the workspace mount only.

   Fix: validated that Gondolin's
   `ReadonlyProvider(RealFSProvider(hostPath))` layered into
   `vfs.mounts` gives the same read-only semantics as docker's
   `:ro` (KVM integration tests prove read works, write rejected
   with EROFS, host file unchanged). Added a daemon-side
   `extra_mounts` config key and a Python-side wiring that
   populates it from the two existing helpers. See §Hermes runtime
   files above for the user-facing config surface and §Skill
   compatibility note below for what this means for the categories
   originally flagged in the deferral.

   The `~/.hermes/.env` credential fallthrough (the original (b)
   case) is a cross-sandbox bug, not gondolin-specific — `.env`
   isn't in `get_credential_file_mounts()` under docker either. The
   correct fix is wire-injection via `terminal.gondolin.secrets:`
   (or equivalent per-backend mechanism). Tracked as a follow-up to
   the github-skill family specifically; doesn't gate the gondolin
   PR.
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

## Skill compatibility note (2026-05-27)

Two patterns were flagged during the pre-merge audit as potentially
broken under `terminal.backend: gondolin`. Both are mitigated by the
skill + credential-file projection landed in this phase, with one
follow-up for the github-skill family. None gate the upstream PR.

- **Skill-bundled scripts referenced as
  `~/.hermes/skills/<name>/scripts/...`**: covered by the
  `get_skills_directory_mount()` projection. Same fix as
  docker/singularity. Works under gondolin without per-skill edits as
  long as the user keeps `terminal.gondolin.project_skills: true`
  (the default).

- **`~/.hermes/.env` credential fallthrough** (`github/github-auth`
  and its sibling skills): this is a cross-sandbox bug — `.env` isn't
  in `get_credential_file_mounts()` under any current backend, so the
  in-VM `[ -f ~/.hermes/.env ] && export GITHUB_TOKEN=...` branch
  silently falls through to `AUTH_METHOD=none` whether the user is on
  gondolin, docker, modal, or any other remote backend. The right fix
  is wire-injected `GITHUB_TOKEN` via `terminal.gondolin.secrets:` (or
  the analogous per-backend mechanism). Tracked as a follow-up to the
  github-skill family; not gondolin-specific and not a regression
  introduced by this PR.

A few skills (`devops/docker-management`, `mlops/inference/vllm`,
`research/blogwatcher`) issue `docker run` from inside the sandbox.
Running them under a microVM-bearing backend is a category mismatch
("don't use those under gondolin") — not a skill change.

Network-allowlist surprises (a skill hits a host not in
`allowed_hosts`) are caught at runtime by the existing `policy_denied`
sentinel and surfaced through the tool result — no pre-audit needed.

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
- **2026-05-27** — Skill + credential-file projection landed. Closes
  open question (4). Daemon-side `extra_mounts` config key takes
  `{guest_path, host_path, readonly}` entries and layers them into
  `vfs.mounts` as `ReadonlyProvider(RealFSProvider)` when `readonly:
  true` (default). Python-side `GondolinEnvironment` populates
  `extra_mounts` from `tools/credential_files.py`'s
  `get_skills_directory_mount()` and `get_credential_file_mounts()`
  helpers — the same helpers `docker.py`, `singularity.py`,
  `modal.py`, and `managed_modal.py` already use. Credential files
  group by guest parent directory (Gondolin's `RealFSProvider` takes
  a directory rootPath, not a single file); collisions skip with a
  WARN and the user can route those via wire-injected `secrets:`
  instead. Config knobs: `project_skills` (default true),
  `project_credentials` (default true), or explicit `extra_mounts:`
  for arbitrary host directories. 4 KVM integration tests + 3 Python
  unit tests + 76/76 existing gondolin tests green, 8/8 KVM
  end-to-end still green.

## Note for upstream: unify the credential surface

This phase landed two credential delivery paths that, from a user's
perspective, answer the same question — "how does an in-sandbox process
authenticate to an external service?" — at different layers:

1. **File projection** (`terminal.credential_files:`, the existing
   cross-backend mechanism) — host file → read-only mount inside the
   sandbox.
2. **Wire injection** (`terminal.gondolin.secrets:`, gondolin-only) —
   placeholder in the guest's env, real value spliced into outbound
   HTTPS for allow-listed hosts.

Configuring both surfaces is confusing because:
- they live at different config sites (`terminal.credential_files:` at
  the terminal level, `terminal.gondolin.secrets:` nested under one
  backend);
- there's no per-credential guidance baked into the docs about which
  mode to pick (e.g. `GITHUB_TOKEN` is naturally wire-inject; an
  `~/.ssh/id_ed25519` is naturally file projection);
- switching backends silently changes whether `terminal.gondolin.secrets:`
  has any effect.

**Recommended upstream direction**: unify under a single top-level
`terminal.credentials:` block. Each entry declares a source (`from_env`
/ `from_command` / `from_file` / `value:`) and a delivery mode
(`wire_inject` with `hosts:`, or `file` with `guest_path:`). Hermes
validates the (backend × delivery mode) pair at startup against a
shipped support matrix and warns on unsupported combinations.

Crucially, the design should explicitly accept that **not every backend
supports every delivery mode** — wire injection requires an HTTP hooks
layer that only gondolin ships today (other backends would need a
proxy). The user-facing config stays single-shape; the
backend-capability matrix is the validation surface.

This is intentionally **not** part of the phase-2 PR — that PR
introduces `secrets:` as it stands today to keep the diff scoped to
gondolin. Unification is a follow-up RFC against
`NousResearch/hermes-agent` once gondolin is upstream and the wire
injection mechanism has a second consumer to justify the abstraction.

## Default image: first-run local build (`hermes-runtime`)

### The gap

`alpine-base:latest` — gondolin's own default — ships BusyBox plus
networking. It does **not** ship `python3`, `node`, `npm`, `uv`, or
`bash`. That makes the built-in `execute_code` tool fail on the
default gondolin install, and a handful of skills that shell out to
those interpreters degrade. The previous mitigation was an actionable
error message + a `hermes doctor` info line + `test_execute_code_round_trip`
marked `xfail(strict=True)`. Honest, but a built-in tool failing on a
built-in backend violates Hermes's "select a backend, things work"
posture.

### What was considered, what was picked

Four options, narrowed by experiment:

1. **Default to a gondolin-registry image with python preinstalled**
   (e.g. `ubuntu-noble:latest`, assuming it ships `python3`). Cheap to
   change; hitches our default contents to gondolin's image cadence;
   loses pinning control.
2. **Hermes publishes its own image** (`hermes-runtime:latest` to a
   registry). Full control over contents and version cadence; brings
   image-publishing infrastructure into Hermes for the first time
   (registry, signing keys, release pipeline). Significant new
   surface.
3. **Stay on `alpine-base:latest`.** Smallest install, fastest boot,
   keep the strict-xfail and the doctor warning. Today's behavior.
4. **First-run local build using gondolin's existing pipeline.**
   `gondolin build --config <hermes-bundled-config> --tag
   hermes-runtime:<hermes-version>` runs on the user's host, pulls
   Alpine packages from the upstream mirror, produces a versioned
   local image. Zero Hermes-published artifacts; we own the package
   list via a JSON file in the repo.

(4) was validated with real numbers on a clean WSL2 host (Ubuntu
24.04.4, x86_64, gondolin 0.12.0):

| Metric                | Value                                                                              |
|-----------------------|------------------------------------------------------------------------------------|
| Build wall-clock      | **9.19 s** (cold; Alpine mirror was warm)                                          |
| Image size on disk    | **326 MB** (rootfs 289 MB + kernel 19 MB + krun 12 MB + initramfs 6 MB)            |
| Total gondolin cache  | 789 MB (alpine-base + hermes-runtime)                                              |
| Host packages needed  | `cpio`, `lz4` (both stock apt; absent by default on Ubuntu 24.04)                  |
| In-VM toolchain proven| python 3.12.13, node 24, npm 11, uv 0.10, bash 5.3, curl 8.19, openssh             |

Build pulls from `dl-cdn.alpinelinux.org` (Alpine packages) and
`github.com/containers/libkrunfw/releases` (kernel). No host root
required at build time (the build step that needs root — mkfs.ext4
inside the chroot — runs via gondolin's own machinery, which on this
host worked unprivileged). Image artifacts land under
`~/.cache/gondolin/images/objects/<build-id>/` and are tagged in
gondolin's own image store via `gondolin image tag` — Hermes does not
own a parallel store.

(4) won. It gives us option (2)'s contents-control without any of
(2)'s infrastructure cost, and bypasses (1)'s upstream-cadence
coupling.

### What Hermes ships

- **`tools/environments/gondolin_host/hermes-runtime.json`** — pinned
  build config. Alpine version, kernel package, package list, krunfw
  version all hard-pinned. This file is the supply-chain spec; bumping
  it is a deliberate Hermes-side change with its own commit and
  changelog entry. Lives next to the daemon source for proximity.
- **`hermes doctor` checks**:
  1. host has `cpio` and `lz4` (the two non-standard apt deps the
     build needs)
  2. `gondolin image ls` includes a current
     `hermes-runtime:<hermes-version>` tag
  3. if either check fails, surface a one-line actionable fix (the
     exact `sudo apt-get install …` command, or the exact `hermes
     gondolin build` command)
- **Default image resolution** in `tools/environments/gondolin.py`:
  the daemon-side `config.image` defaults to
  `hermes-runtime:<hermes-version>` (where `<hermes-version>` is read
  from `hermes_cli.__version__`). If the user has explicitly set
  `terminal.gondolin.image`, their value wins. If the
  `hermes-runtime:<hermes-version>` tag isn't present at session
  start, the environment falls back to `alpine-base:latest` with a
  WARN-level log entry and `execute_code` keeps its existing
  actionable error — no regression versus today.

### Why version the tag with Hermes's version

`hermes-runtime:0.14.0` rather than `hermes-runtime:latest`. Three
reasons:

1. **Reproducibility.** Two users on Hermes 0.14.0 get identical
   images. A user on 0.14.0 and a user on 0.15.0 may not — that's
   intended, because the spec moved.
2. **Upgrade detection.** When a user upgrades to a Hermes version
   that changed the build spec, the existing `hermes-runtime:0.14.0`
   tag is still present and still works for the old install. The new
   install starts with no `hermes-runtime:0.15.0` tag and the doctor
   prompts for a fresh build. No mid-flight image swap.
3. **Garbage collection later.** `gondolin image ls` makes it trivial
   for a future `hermes gondolin gc` command to delete old
   `hermes-runtime:<old-version>` tags.

### First-run UX

The build is a side-effect-bearing host operation (writes to
`~/.cache/gondolin/`, ~330 MB on disk, ~10 s wall, network-dependent).
Hermes does **not** run the build automatically — gondolin's contract
is that the user opts in to running VMs on their host. Instead:

- `hermes doctor` and the first gondolin VM boot detect the missing
  tag and surface the one-line command:
  ```
  hermes-runtime:0.14.0 not built. Run:
    hermes gondolin build
  to build it (~10 s, ~330 MB local cache). Requires cpio + lz4.
  ```
- `hermes gondolin build` is a thin wrapper around
  `gondolin build --config tools/environments/gondolin_host/hermes-runtime.json --tag hermes-runtime:<hermes-version>`.
  Idempotent: rebuilding while the tag exists rebuilds. Failure modes
  surface the upstream gondolin error verbatim — no swallowing.

### Network and security posture

Both the Alpine mirror and libkrunfw GitHub release are HTTPS, both
have integrity checks built into Alpine's package format and
gondolin's manifest verification respectively. The build doesn't
require Hermes-side signing — the supply chain is "Alpine package
signatures + libkrunfw release signatures + a pinned spec in our
repo." That is, deliberately, the same trust surface a Hermes user
would have if they ran `gondolin build` themselves. We're not adding
trust assumptions.

### What this closes

- `test_execute_code_round_trip` flips from `xfail(strict=True)` to a
  passing test (gated on a built `hermes-runtime` tag; falls back to
  `xfail` if the tag isn't present, since CI may not have the host
  packages).
- The doctor's existing "default image lacks python3" info line is
  replaced by the build-prompt line above.
- `code_execution_tool`'s actionable error stays as a defensive
  fallback for the case where someone explicitly sets
  `terminal.gondolin.image` to a python-less image.

### Phase 5+ ideas (not in this commit)

- Optional pre-warm during `hermes setup` when the user picks gondolin
  as their backend.
- `hermes gondolin gc` to prune old `hermes-runtime:<old-version>`
  tags.
- Layer skill-specific tooling on demand: a skill that requires `gh`
  declares it, and `hermes gondolin build` includes `github-cli` in
  the package list. Out of scope for the upstream PR; tracked
  separately.
