"""Tests for gondolin backend wiring in tools.terminal_tool.

Two things to validate:
1. ``_create_environment("gondolin", ...)`` returns a GondolinEnvironment
   with the config dict forwarded.
2. ``_get_env_config()`` reads gondolin-specific env vars
   (TERMINAL_GONDOLIN_*) into a structured config block.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NODE_DAEMON = REPO_ROOT / "tools" / "environments" / "gondolin_host" / "src" / "daemon.mjs"
NODE_AVAILABLE = shutil.which("node") is not None and NODE_DAEMON.exists()


def test_get_env_config_reads_gondolin_keys(monkeypatch):
    """All gondolin env vars surface as a structured config block under
    a 'gondolin' key, with sane defaults when nothing is set."""
    from tools.terminal_tool import _get_env_config

    # Set the gondolin-specific env vars.
    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.setenv(
        "TERMINAL_GONDOLIN_ALLOWED_HOSTS",
        '["pypi.org","registry.npmjs.org"]',
    )
    monkeypatch.setenv(
        "TERMINAL_GONDOLIN_SECRETS_JSON",
        '{"GITHUB_TOKEN":{"hosts":["github.com"],"from_env":"GITHUB_TOKEN"}}',
    )
    monkeypatch.setenv(
        "TERMINAL_GONDOLIN_POLICY_SCRIPT",
        "/tmp/my-policy.mjs",
    )
    monkeypatch.setenv("TERMINAL_GONDOLIN_SANDBOX_DIR", "/tmp/my-sandbox")
    monkeypatch.setenv("TERMINAL_GONDOLIN_IMAGE", "ubuntu-noble:latest")

    cfg = _get_env_config()
    g = cfg["gondolin"]
    assert g["allowed_hosts"] == ["pypi.org", "registry.npmjs.org"]
    assert g["secrets"]["GITHUB_TOKEN"]["from_env"] == "GITHUB_TOKEN"
    assert g["policy_script"] == "/tmp/my-policy.mjs"
    assert g["sandbox_dir"] == "/tmp/my-sandbox"
    assert g["image"] == "ubuntu-noble:latest"


def test_get_env_config_defaults_for_gondolin(monkeypatch):
    """When no gondolin env vars are set, the block is present but empty
    so downstream code can use ``.get()`` uniformly. image=None means
    'let Gondolin use its own default (alpine-base:latest)'."""
    from tools.terminal_tool import _get_env_config

    # Make sure no gondolin vars leak in from the surrounding shell.
    for k in (
        "TERMINAL_GONDOLIN_ALLOWED_HOSTS",
        "TERMINAL_GONDOLIN_SECRETS_JSON",
        "TERMINAL_GONDOLIN_POLICY_SCRIPT",
        "TERMINAL_GONDOLIN_SANDBOX_DIR",
        "TERMINAL_GONDOLIN_IMAGE",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TERMINAL_ENV", "gondolin")

    cfg = _get_env_config()
    g = cfg["gondolin"]
    assert g["allowed_hosts"] == ["*"]
    assert g["secrets"] == {}
    assert g["policy_script"] is None
    assert g["sandbox_dir"] is None
    assert g["image"] is None


def test_get_env_config_default_cwd_for_gondolin(monkeypatch):
    """Default cwd for gondolin backend is /workspace (the in-VM mount)."""
    from tools.terminal_tool import _get_env_config

    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    cfg = _get_env_config()
    assert cfg["cwd"] == "/workspace"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_returns_gondolin_environment(tmp_path):
    """_create_environment('gondolin', ...) constructs a GondolinEnvironment
    using the gondolin_config block. Uses stub_vm=True via a config flag
    so we don't boot QEMU."""
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    sandbox = str(tmp_path / "vm-sandbox")
    gondolin_config = {
        "allowed_hosts": ["*"],
        "secrets": {},
        "policy_script": None,
        "sandbox_dir": sandbox,
        "image": "ubuntu-noble:latest",
        "stub_vm": True,  # test-only flag honored by factory
    }
    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        gondolin_config=gondolin_config,
        task_id="test-gondolin",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        assert env.sandbox_dir.as_posix() == sandbox
        # Image must have been forwarded into the daemon's init payload.
        assert env.config.get("image") == "ubuntu-noble:latest"
    finally:
        env.cleanup()


def test_create_environment_rejects_unknown_backend():
    """Sanity: the existing error path still rejects gibberish so the
    new gondolin branch doesn't accidentally swallow it."""
    from tools.terminal_tool import _create_environment

    with pytest.raises(ValueError, match="Unknown environment type"):
        _create_environment(
            env_type="not_a_backend",
            image="",
            cwd="/workspace",
            timeout=60,
        )
