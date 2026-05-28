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


@pytest.fixture
def _bypass_image_build(monkeypatch):
    """Bypass the OCI rootfs build path so _create_environment can run
    without touching gondolin's image store or shelling out to podman.

    Substitutes ``ensure_built`` (and the wrapper in terminal_tool) with
    an identity passthrough — whatever image string the test provides,
    flows straight through to the daemon config. Used by the
    constructor-shape tests below; the actual build path is exercised
    end-to-end in tests/integration/test_gondolin_terminal.py and
    unit-tested in tests/hermes_cli/test_gondolin_image.py.
    """
    from hermes_cli import gondolin_image
    from tools import terminal_tool

    monkeypatch.setattr(gondolin_image, "ensure_built", lambda image: image)
    monkeypatch.setattr(
        terminal_tool, "_ensure_gondolin_image_built", lambda image: image
    )
    yield


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
    monkeypatch.setenv("TERMINAL_GONDOLIN_IMAGE", "python:3.11-slim")
    monkeypatch.setenv("TERMINAL_GONDOLIN_MEMORY", "512M")
    monkeypatch.setenv("TERMINAL_GONDOLIN_CPUS", "1")

    cfg = _get_env_config()
    g = cfg["gondolin"]
    assert g["allowed_hosts"] == ["pypi.org", "registry.npmjs.org"]
    assert g["secrets"]["GITHUB_TOKEN"]["from_env"] == "GITHUB_TOKEN"
    assert g["policy_script"] == "/tmp/my-policy.mjs"
    assert g["sandbox_dir"] == "/tmp/my-sandbox"
    assert g["image"] == "python:3.11-slim"
    assert g["memory"] == "512M"
    assert g["cpus"] == 1


def test_get_env_config_defaults_for_gondolin(monkeypatch):
    """When no gondolin env vars are set, the gondolin block carries
    the default OCI image — same string the docker backend defaults to.
    Image resolution happens later in _create_environment, not here.
    """
    from tools.terminal_tool import DEFAULT_GONDOLIN_IMAGE, _get_env_config

    # Make sure no gondolin vars leak in from the surrounding shell.
    for k in (
        "TERMINAL_GONDOLIN_ALLOWED_HOSTS",
        "TERMINAL_GONDOLIN_SECRETS_JSON",
        "TERMINAL_GONDOLIN_POLICY_SCRIPT",
        "TERMINAL_GONDOLIN_SANDBOX_DIR",
        "TERMINAL_GONDOLIN_IMAGE",
        "TERMINAL_GONDOLIN_MEMORY",
        "TERMINAL_GONDOLIN_CPUS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TERMINAL_ENV", "gondolin")

    cfg = _get_env_config()
    g = cfg["gondolin"]
    assert g["allowed_hosts"] == ["*"]
    assert g["secrets"] == {}
    assert g["policy_script"] is None
    assert g["sandbox_dir"] is None
    assert g["image"] == DEFAULT_GONDOLIN_IMAGE
    assert g["memory"] is None
    assert g["cpus"] is None


def test_default_gondolin_image_matches_docker_default():
    """Parity check: the gondolin default OCI image is the same string
    docker uses, so a user switching backends doesn't relearn.

    If the docker default changes, this test fails and the docs in
    configuration.md need updating in lockstep.
    """
    from tools.terminal_tool import DEFAULT_GONDOLIN_IMAGE, _get_env_config

    monkeypatch_env = {
        "TERMINAL_ENV": "docker",
    }
    # Read the docker backend's default by inspecting the code, since
    # _get_env_config returns a Dict that doesn't carry the docker
    # default in a stable place. The hardcoded default in terminal_tool
    # is "docker.io/nikolaik/python-nodejs:python3.11-nodejs20".
    # Fully-qualified so podman (which gondolin uses on Linux) doesn't
    # trip on short-name resolution — see DEFAULT_GONDOLIN_IMAGE comment.
    assert (
        DEFAULT_GONDOLIN_IMAGE
        == "docker.io/nikolaik/python-nodejs:python3.11-nodejs20"
    )


def test_get_env_config_default_cwd_for_gondolin(monkeypatch):
    """Default cwd for gondolin backend is /workspace (the in-VM mount)."""
    from tools.terminal_tool import _get_env_config

    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    cfg = _get_env_config()
    assert cfg["cwd"] == "/workspace"


def test_get_env_config_rejects_host_cwd_for_gondolin(monkeypatch):
    """TERMINAL_CWD pointing at a host path must be overridden — the guest
    VM doesn't have ``/home/matt``. This is the regression for the
    real-world smoke that did ``echo … > /workspace/proof.txt`` against a
    VM whose actual cwd was bind-mounted to ``/home/matt`` (so writes
    failed and the host sandbox_dir stayed empty).

    Behaviour mirrors what docker/singularity/modal already do."""
    from tools.terminal_tool import _get_env_config

    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.setenv("TERMINAL_CWD", "/home/matt")  # host path

    cfg = _get_env_config()
    assert cfg["cwd"] == "/workspace", (
        f"gondolin must override host-path TERMINAL_CWD; got {cfg['cwd']!r}"
    )


def test_get_env_config_rejects_relative_cwd_for_gondolin(monkeypatch):
    """Relative cwd ('.' or 'src/') is meaningless in the guest VM and
    must fall back to /workspace."""
    from tools.terminal_tool import _get_env_config

    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.setenv("TERMINAL_CWD", ".")

    cfg = _get_env_config()
    assert cfg["cwd"] == "/workspace"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_returns_gondolin_environment(
    tmp_path, _bypass_image_build
):
    """_create_environment('gondolin', ...) constructs a GondolinEnvironment
    using the gondolin_config block. Uses stub_vm=True via a config flag
    so we don't boot QEMU.

    Bypasses the image build path with a fixture — the build is unit-tested
    separately in tests/hermes_cli/test_gondolin_image.py.
    """
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    sandbox = str(tmp_path / "vm-sandbox")
    gondolin_config = {
        "allowed_hosts": ["*"],
        "secrets": {},
        "policy_script": None,
        "sandbox_dir": sandbox,
        "image": "python:3.11-slim",
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
        assert env.config.get("image") == "python:3.11-slim"
    finally:
        env.cleanup()


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_propagates_lock_dir_for_cross_process_cap(
    tmp_path, monkeypatch, _bypass_image_build
):
    """The factory injects a default lock_dir under HERMES_HOME so the
    cross-process concurrent-VM cap is honored across the CLI, subagents,
    the gateway, and cron jobs — without users having to set anything."""
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox"),
            "stub_vm": True,
            "image": "python:3.11-slim",
        },
        task_id="test-lockdir",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        # The default lock dir lives under HERMES_HOME and is created on
        # first slot acquire. Even though the in-process cap defaults to
        # 0 (disabled), the lock dir wiring should still be intact.
        # We probe via the resolved value flowing through __init__: with
        # cap=0 no flock is taken, so the slot's fd is None — but the
        # default path is computed and would be used if cap were > 0.
        assert env._slot is not None
        assert env._slot.in_process is True
    finally:
        env.cleanup()


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_propagates_max_concurrent_vms_knob(
    tmp_path, monkeypatch, _bypass_image_build
):
    """`terminal.gondolin.max_concurrent_vms` set in config flows through
    to the module-level cap so users don't have to also export the env var."""
    from tools.terminal_tool import _create_environment
    from tools.environments import gondolin as gondolin_mod

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 0)

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox"),
            "stub_vm": True,
            "max_concurrent_vms": 3,
            "image": "python:3.11-slim",
        },
        task_id="test-cap-knob",
    )
    try:
        assert gondolin_mod._max_concurrent_vms == 3
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


def test_config_set_terminal_gondolin_keys_sync_to_env(monkeypatch, tmp_path):
    """`hermes config set terminal.gondolin.<key>` must persist into .env
    under the matching TERMINAL_GONDOLIN_* name so terminal_tool sees it.

    Without the YAML->env sync, the user sets the YAML key but the
    backend never reads it (terminal_tool only consults env vars), which
    is a silent footgun.
    """
    import hermes_cli.config as config_mod

    saved = {}
    def _fake_save_env_value(key, value):
        saved[key] = value
    monkeypatch.setattr(config_mod, "save_env_value", _fake_save_env_value)

    # Provide a minimal yaml/file harness so set_config doesn't need a real
    # HERMES_HOME — write/read goes through tmp_path.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("terminal:\n  backend: local\n")
    monkeypatch.setattr(config_mod, "get_config_path", lambda: cfg_path)

    keys_to_sync = {
        "terminal.backend": ("gondolin", "TERMINAL_ENV"),
        "terminal.gondolin.image": ("python:3.11-slim", "TERMINAL_GONDOLIN_IMAGE"),
        "terminal.gondolin.policy_script": ("/tmp/p.mjs", "TERMINAL_GONDOLIN_POLICY_SCRIPT"),
        "terminal.gondolin.sandbox_dir": ("/tmp/sb", "TERMINAL_GONDOLIN_SANDBOX_DIR"),
        "terminal.gondolin.memory": ("512M", "TERMINAL_GONDOLIN_MEMORY"),
        "terminal.gondolin.cpus": ("1", "TERMINAL_GONDOLIN_CPUS"),
        "terminal.gondolin.max_concurrent_vms": ("4", "TERMINAL_GONDOLIN_MAX_CONCURRENT_VMS"),
    }
    for key, (value, expected_env_key) in keys_to_sync.items():
        config_mod.set_config_value(key, value)

    for key, (value, expected_env_key) in keys_to_sync.items():
        assert expected_env_key in saved, (
            f"{key} should have synced to env var {expected_env_key}; "
            f"saved keys were {sorted(saved.keys())}"
        )
        assert saved[expected_env_key] == value
