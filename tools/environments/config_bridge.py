"""Single source of truth for terminal-config → env var bridging.

Three different code paths used to maintain their own hand-edited copy of
the ``terminal.* → TERMINAL_*`` mapping:

- ``cli.py``'s ``load_cli_config()`` at startup — bridges so the CLI's
  agent sees the values.
- ``gateway/run.py``'s init bridge — same, for the gateway.
- ``hermes_cli/config.py``'s ``hermes config set`` command — mirrors a
  single key change into ``~/.hermes/.env`` for the next session.

Adding a new ``terminal.*`` knob meant editing three places. Forgetting
one is a silent bug: the YAML is read but the runtime never sees it. The
gondolin ``extra_mounts`` / ``secrets`` regression that prompted this
refactor was exactly that — three places knew about ``terminal.backend``
but none of them knew about ``terminal.gondolin.extra_mounts``.

This module owns the canonical list. All three call sites import from
here. The matching read side, ``terminal_tool._get_env_config()``, also
consults this map so the round trip is closed by construction.

Drift protection: ``tests/tools/test_terminal_config_bridge.py`` asserts
that every entry round-trips (YAML → env → ``_get_env_config()``) for
each declared encoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator


@dataclass(frozen=True)
class TerminalConfigKey:
    """One terminal config key and how it bridges to an env var.

    Attributes:
        yaml_path: Dotted path under config.yaml ``terminal:`` block
            (e.g. ``"backend"``, ``"gondolin.extra_mounts"``).
        env_var: Environment variable name (e.g. ``"TERMINAL_ENV"``).
        encoding: How the value crosses the env-var boundary.
            ``"scalar"`` → ``str(value)``. ``"json"`` → ``json.dumps(value)``.
        is_nested: True iff yaml_path contains a dot (per-backend nested key).
            Bridges have historically only flattened the top level; nested
            keys need to walk one level into the dict.
    """

    yaml_path: str
    env_var: str
    encoding: str  # "scalar" | "json"

    @property
    def is_nested(self) -> bool:
        return "." in self.yaml_path

    def split(self) -> tuple[str, ...]:
        return tuple(self.yaml_path.split("."))


# Single source of truth. Adding a new terminal.* knob: add a row here,
# then read it from env via terminal_tool._get_env_config(). No other
# bridge edits needed.
#
# The order here is the order entries appear in `terminal:` blocks
# throughout the docs/examples — keep it that way so a diff against
# the docs stays readable.
TERMINAL_CONFIG_KEYS: tuple[TerminalConfigKey, ...] = (
    # ── Top-level (apply across backends) ─────────────────────────────────
    TerminalConfigKey("backend", "TERMINAL_ENV", "scalar"),
    TerminalConfigKey("cwd", "TERMINAL_CWD", "scalar"),
    TerminalConfigKey("timeout", "TERMINAL_TIMEOUT", "scalar"),
    TerminalConfigKey("lifetime_seconds", "TERMINAL_LIFETIME_SECONDS", "scalar"),
    TerminalConfigKey("modal_mode", "TERMINAL_MODAL_MODE", "scalar"),
    # ── Per-backend images / hosts (scalars) ──────────────────────────────
    TerminalConfigKey("docker_image", "TERMINAL_DOCKER_IMAGE", "scalar"),
    TerminalConfigKey("docker_forward_env", "TERMINAL_DOCKER_FORWARD_ENV", "json"),
    TerminalConfigKey("singularity_image", "TERMINAL_SINGULARITY_IMAGE", "scalar"),
    TerminalConfigKey("modal_image", "TERMINAL_MODAL_IMAGE", "scalar"),
    TerminalConfigKey("daytona_image", "TERMINAL_DAYTONA_IMAGE", "scalar"),
    TerminalConfigKey("vercel_runtime", "TERMINAL_VERCEL_RUNTIME", "scalar"),
    # ── SSH backend ───────────────────────────────────────────────────────
    TerminalConfigKey("ssh_host", "TERMINAL_SSH_HOST", "scalar"),
    TerminalConfigKey("ssh_user", "TERMINAL_SSH_USER", "scalar"),
    TerminalConfigKey("ssh_port", "TERMINAL_SSH_PORT", "scalar"),
    TerminalConfigKey("ssh_key", "TERMINAL_SSH_KEY", "scalar"),
    # ── Shared container knobs (docker, singularity, modal, daytona, vercel) ─
    TerminalConfigKey("container_cpu", "TERMINAL_CONTAINER_CPU", "scalar"),
    TerminalConfigKey("container_memory", "TERMINAL_CONTAINER_MEMORY", "scalar"),
    TerminalConfigKey("container_disk", "TERMINAL_CONTAINER_DISK", "scalar"),
    TerminalConfigKey("container_persistent", "TERMINAL_CONTAINER_PERSISTENT", "scalar"),
    TerminalConfigKey("docker_volumes", "TERMINAL_DOCKER_VOLUMES", "json"),
    TerminalConfigKey("docker_env", "TERMINAL_DOCKER_ENV", "json"),
    TerminalConfigKey("docker_mount_cwd_to_workspace", "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "scalar"),
    TerminalConfigKey("docker_run_as_host_user", "TERMINAL_DOCKER_RUN_AS_HOST_USER", "scalar"),
    TerminalConfigKey("docker_extra_args", "TERMINAL_DOCKER_EXTRA_ARGS", "json"),
    TerminalConfigKey("sandbox_dir", "TERMINAL_SANDBOX_DIR", "scalar"),
    TerminalConfigKey("persistent_shell", "TERMINAL_PERSISTENT_SHELL", "scalar"),
    TerminalConfigKey("sudo_password", "SUDO_PASSWORD", "scalar"),
    # ── Gondolin backend (nested under terminal.gondolin.*) ───────────────
    # Image, hosts, and concurrency: scalars.
    TerminalConfigKey("gondolin.image", "TERMINAL_GONDOLIN_IMAGE", "scalar"),
    TerminalConfigKey("gondolin.policy_script", "TERMINAL_GONDOLIN_POLICY_SCRIPT", "scalar"),
    TerminalConfigKey("gondolin.sandbox_dir", "TERMINAL_GONDOLIN_SANDBOX_DIR", "scalar"),
    TerminalConfigKey("gondolin.memory", "TERMINAL_GONDOLIN_MEMORY", "scalar"),
    TerminalConfigKey("gondolin.cpus", "TERMINAL_GONDOLIN_CPUS", "scalar"),
    TerminalConfigKey("gondolin.max_concurrent_vms", "TERMINAL_GONDOLIN_MAX_CONCURRENT_VMS", "scalar"),
    TerminalConfigKey("gondolin.rootfs_size_mb", "TERMINAL_GONDOLIN_ROOTFS_SIZE_MB", "scalar"),
    TerminalConfigKey("gondolin.lock_dir", "TERMINAL_GONDOLIN_LOCK_DIR", "scalar"),
    # Lists / dicts: json. extra_mounts and secrets are the keys whose
    # absence broke the rollout — see _bridge_test_round_trip below for
    # the regression guard.
    TerminalConfigKey("gondolin.allowed_hosts", "TERMINAL_GONDOLIN_ALLOWED_HOSTS", "json"),
    TerminalConfigKey("gondolin.extra_mounts", "TERMINAL_GONDOLIN_EXTRA_MOUNTS_JSON", "json"),
    TerminalConfigKey("gondolin.secrets", "TERMINAL_GONDOLIN_SECRETS_JSON", "json"),
    # Opt-out gates for auto-derived mounts. Booleans; scalar-encoded.
    TerminalConfigKey("gondolin.project_skills", "TERMINAL_GONDOLIN_PROJECT_SKILLS", "scalar"),
    TerminalConfigKey("gondolin.project_credentials", "TERMINAL_GONDOLIN_PROJECT_CREDENTIALS", "scalar"),
)


def iter_keys() -> Iterator[TerminalConfigKey]:
    """Iterate over every registered key. Stable order (declaration order)."""
    return iter(TERMINAL_CONFIG_KEYS)


def yaml_path_to_env_var() -> dict[str, str]:
    """Return ``{"terminal.<yaml_path>": "<ENV_VAR>", ...}``.

    The ``terminal.`` prefix is included so callers like
    ``hermes_cli/config.py`` can do a direct ``key in map`` lookup
    against the dotted key the user typed.
    """
    return {f"terminal.{k.yaml_path}": k.env_var for k in TERMINAL_CONFIG_KEYS}


def lookup_by_yaml_path(yaml_path: str) -> TerminalConfigKey | None:
    """Find an entry by its yaml_path (no ``terminal.`` prefix)."""
    for k in TERMINAL_CONFIG_KEYS:
        if k.yaml_path == yaml_path:
            return k
    return None


def encode_value(key: TerminalConfigKey, value: Any) -> str:
    """Encode a Python value for the env var, per the key's encoding."""
    import json
    if key.encoding == "json":
        return json.dumps(value)
    return str(value)


def apply_terminal_config_to_env(
    terminal_cfg: dict,
    *,
    os_environ: dict | None = None,
    overwrite: bool = True,
    skip: Callable[[TerminalConfigKey, Any], bool] | None = None,
) -> list[str]:
    """Bridge a ``terminal:`` config block to environment variables.

    Walks every key in :data:`TERMINAL_CONFIG_KEYS`, reads its value out
    of ``terminal_cfg`` (handling nested ``gondolin.*`` paths), and sets
    the corresponding env var. Returns the list of env vars that were
    set, in declaration order. Used by ``cli.py`` at CLI startup and by
    ``gateway/run.py`` at gateway startup; both have light wrappers
    around it for their own pre/post massaging (e.g. cwd resolution).

    Args:
        terminal_cfg: The ``terminal`` sub-dict of config.yaml.
        os_environ: The environment dict to mutate. Defaults to
            ``os.environ`` (the live process env).
        overwrite: If False, only set an env var when it isn't already
            present. Used by cli.py to honor pre-existing .env values
            unless a config.yaml ``terminal:`` block was explicitly set
            in this run (the call site decides the policy).
        skip: Optional predicate. When it returns True for a (key, value)
            pair, that pair is skipped entirely. Used by call sites that
            need to special-case a single key (e.g. cwd placeholders)
            without forking the bridge.

    Returns:
        List of env var names that were set this call.
    """
    import os
    env = os_environ if os_environ is not None else os.environ
    set_vars: list[str] = []
    if not isinstance(terminal_cfg, dict):
        return set_vars
    for key in TERMINAL_CONFIG_KEYS:
        # Walk the dotted path through terminal_cfg.
        cur: Any = terminal_cfg
        found = True
        for part in key.split():
            if not isinstance(cur, dict) or part not in cur:
                found = False
                break
            cur = cur[part]
        if not found:
            continue
        if skip is not None and skip(key, cur):
            continue
        if not overwrite and key.env_var in env:
            continue
        env[key.env_var] = encode_value(key, cur)
        set_vars.append(key.env_var)
    return set_vars
