"""Drift guards for the terminal config → env bridge.

These tests prove that the bridge registry in
``tools/environments/config_bridge.py`` is the single source of truth.
If anyone adds a new key to one of the three historical hand-maintained
lists in cli.py / gateway/run.py / hermes_cli/config.py without adding
it to the registry, these tests fail.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Drop every TERMINAL_* env var so tests start from a clean slate."""
    import os
    for key in list(os.environ):
        if key.startswith("TERMINAL_") or key in {"SUDO_PASSWORD"}:
            monkeypatch.delenv(key, raising=False)
    yield


def test_registry_has_no_duplicate_yaml_paths():
    """Drift guard: every yaml_path is unique. Two entries for the same
    key would mean the bridge double-sets and one of them silently wins."""
    from tools.environments.config_bridge import TERMINAL_CONFIG_KEYS
    paths = [k.yaml_path for k in TERMINAL_CONFIG_KEYS]
    assert len(paths) == len(set(paths)), (
        f"Duplicate yaml_path in TERMINAL_CONFIG_KEYS: "
        f"{[p for p in paths if paths.count(p) > 1]}"
    )


def test_registry_has_no_duplicate_env_vars():
    """Drift guard: every env_var is unique."""
    from tools.environments.config_bridge import TERMINAL_CONFIG_KEYS
    envs = [k.env_var for k in TERMINAL_CONFIG_KEYS]
    assert len(envs) == len(set(envs)), (
        f"Duplicate env_var in TERMINAL_CONFIG_KEYS: "
        f"{[e for e in envs if envs.count(e) > 1]}"
    )


def test_registry_includes_all_documented_gondolin_keys():
    """The gondolin regression that motivated this refactor: extra_mounts,
    secrets, project_skills, project_credentials were missing from the
    bridge. Lock them in here as a permanent regression guard."""
    from tools.environments.config_bridge import lookup_by_yaml_path
    for required in (
        "gondolin.image",
        "gondolin.allowed_hosts",
        "gondolin.secrets",
        "gondolin.extra_mounts",
        "gondolin.project_skills",
        "gondolin.project_credentials",
        "gondolin.rootfs_size_mb",
    ):
        assert lookup_by_yaml_path(required) is not None, (
            f"Required gondolin key {required!r} missing from bridge registry"
        )


def test_apply_bridge_walks_nested_paths():
    """A nested key like ``gondolin.secrets`` lives under
    ``terminal_cfg["gondolin"]["secrets"]``. The bridge must walk into
    the nested dict — flat-only lookup would silently drop everything
    under ``terminal.gondolin.*``."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    env: dict = {}
    terminal_cfg = {
        "backend": "gondolin",
        "gondolin": {
            "image": "mcr.example.com/img:1",
            "secrets": {"GH_TOKEN": {"hosts": ["api.github.com"], "from_env": "X"}},
            "extra_mounts": [{"host_path": "/h", "guest_path": "/g", "readonly": False}],
            "project_skills": False,
        },
    }
    set_vars = apply_terminal_config_to_env(terminal_cfg, os_environ=env)

    assert "TERMINAL_ENV" in set_vars and env["TERMINAL_ENV"] == "gondolin"
    assert env["TERMINAL_GONDOLIN_IMAGE"] == "mcr.example.com/img:1"
    assert json.loads(env["TERMINAL_GONDOLIN_SECRETS_JSON"]) == terminal_cfg["gondolin"]["secrets"]
    assert json.loads(env["TERMINAL_GONDOLIN_EXTRA_MOUNTS_JSON"]) == terminal_cfg["gondolin"]["extra_mounts"]
    assert env["TERMINAL_GONDOLIN_PROJECT_SKILLS"] == "False"


def test_apply_bridge_skips_missing_keys():
    """Don't set an env var for a key that isn't in the YAML."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    env: dict = {}
    apply_terminal_config_to_env({"backend": "local"}, os_environ=env)
    # Only TERMINAL_ENV should be set; nothing else.
    assert env == {"TERMINAL_ENV": "local"}


def test_apply_bridge_respects_overwrite_false():
    """When overwrite=False, pre-existing env vars win. This is what
    cli.py does so a user-set .env entry isn't clobbered by an absent
    config.yaml block."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    env = {"TERMINAL_ENV": "docker"}  # pre-existing
    apply_terminal_config_to_env(
        {"backend": "gondolin"}, os_environ=env, overwrite=False
    )
    assert env["TERMINAL_ENV"] == "docker"  # unchanged


def test_apply_bridge_skip_predicate():
    """Call sites that need to special-case a key (e.g. cwd placeholders)
    can pass a skip predicate without forking the bridge."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    env: dict = {}
    apply_terminal_config_to_env(
        {"backend": "local", "cwd": "."},
        os_environ=env,
        skip=lambda key, val: key.yaml_path == "cwd" and val == ".",
    )
    assert env == {"TERMINAL_ENV": "local"}


# ── Round-trip: bridge → _get_env_config() reads the same shape ──────────

def test_round_trip_gondolin_secrets_through_get_env_config(monkeypatch):
    """Set TERMINAL_GONDOLIN_SECRETS_JSON via the bridge, then read it
    back via terminal_tool._get_env_config(). They must agree on shape.
    This is the core SSOT property: write side and read side share the
    same registry, so adding a key in one place can't drift from the
    other."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    secrets = {
        "GH_TOKEN_WORK": {
            "hosts": ["api.github.com"],
            "from_command": "gh auth token --user mattkot_microsoft",
        },
    }
    terminal_cfg = {
        "backend": "gondolin",
        "gondolin": {
            "image": "python:3.11-slim",
            "secrets": secrets,
        },
    }
    apply_terminal_config_to_env(terminal_cfg, os_environ=dict(__import__("os").environ))
    # Use real os.environ so _get_env_config sees the writes
    apply_terminal_config_to_env(terminal_cfg)

    from tools.terminal_tool import _get_env_config
    cfg = _get_env_config()
    assert cfg["env_type"] == "gondolin"
    assert cfg["gondolin"]["image"] == "python:3.11-slim"
    assert cfg["gondolin"]["secrets"] == secrets


def test_round_trip_gondolin_extra_mounts_through_get_env_config(monkeypatch):
    """Same SSOT property for extra_mounts — the key whose absence broke
    the rollout. If this fails after a future refactor, the
    `_get_env_config` read side has fallen out of sync with the bridge."""
    from tools.environments.config_bridge import apply_terminal_config_to_env

    mounts = [
        {"host_path": "/h/a", "guest_path": "/g/a", "readonly": False},
        {"host_path": "/h/b", "guest_path": "/g/b", "readonly": True},
    ]
    terminal_cfg = {
        "backend": "gondolin",
        "gondolin": {
            "image": "python:3.11-slim",
            "extra_mounts": mounts,
        },
    }
    apply_terminal_config_to_env(terminal_cfg)

    from tools.terminal_tool import _get_env_config
    cfg = _get_env_config()
    assert cfg["gondolin"]["extra_mounts"] == mounts
