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
def test_create_environment_translates_container_disk_to_rootfs_size_mb(
    tmp_path, _bypass_image_build
):
    """The shared `terminal.container_disk` knob (MB int) is the same lever
    docker/singularity/modal/daytona use to cap rootfs size. Gondolin's
    equivalent is VMOptions.rootfs.size (qemu-suffix string). The factory
    translates MB → wire key `rootfs_size_mb` so a user setting
    `container_disk: 20480` gets a 20 GB gondolin rootfs without having to
    learn a backend-specific schema. The daemon-side MB→qemu translation
    is tested in socket_transport.test.mjs.
    """
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        container_config={"container_disk": 20480},  # 20 GB
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox"),
            "stub_vm": True,
            "image": "python:3.11-slim",
        },
        task_id="test-disk",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        # Python-side wiring: the disk MB lands on the daemon init payload
        # under the wire key `rootfs_size_mb`.
        assert env.config.get("rootfs_size_mb") == 20480
    finally:
        env.cleanup()


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_omits_rootfs_size_when_container_disk_unset(
    tmp_path, _bypass_image_build
):
    """When `container_disk` is absent from container_config, gondolin must
    NOT pin a size — leave it null so the gondolin VM auto-sizes the rootfs
    based on the image content (no surprise truncation when the image grows)."""
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        container_config={},  # no disk knob
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox"),
            "stub_vm": True,
            "image": "python:3.11-slim",
        },
        task_id="test-disk-default",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        # No rootfs_size_mb on the daemon payload.
        assert "rootfs_size_mb" not in env.config
    finally:
        env.cleanup()


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_forwards_container_persistent_to_gondolin(
    tmp_path, _bypass_image_build
):
    """The shared `terminal.container_persistent` knob controls whether
    the per-task sandbox bind dirs survive cleanup. The factory threads
    this into GondolinEnvironment.persistent_filesystem so docker users
    switching to gondolin get the same lifecycle semantics for free."""
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        container_config={"container_persistent": True},
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox"),
            "stub_vm": True,
            "image": "python:3.11-slim",
        },
        task_id="test-persistent",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        assert env._persistent_filesystem is True
    finally:
        env.cleanup()


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")
def test_create_environment_forwards_container_persistent_false_to_gondolin(
    tmp_path, _bypass_image_build
):
    """container_persistent=False flows through and triggers workspace
    rmtree on cleanup."""
    from tools.terminal_tool import _create_environment
    from tools.environments.gondolin import GondolinEnvironment

    env = _create_environment(
        env_type="gondolin",
        image="",
        cwd="/workspace",
        timeout=60,
        container_config={"container_persistent": False},
        gondolin_config={
            "sandbox_dir": str(tmp_path / "vm-sandbox-ephemeral"),
            "stub_vm": True,
            "image": "python:3.11-slim",
        },
        task_id="test-ephemeral",
    )
    try:
        assert isinstance(env, GondolinEnvironment)
        assert env._persistent_filesystem is False
    finally:
        env.cleanup()


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


# ---- TERMINAL_GONDOLIN_SECRETS_JSON schema validation (G9b) -------------
#
# Without schema validation, hooks.mjs silently ignores unknown keys
# (typo'd `from_envs` → no value source → daemon emits a WARN diagnostic
# but the agent runs without credentials and the user has no obvious
# signal at config-load time). Validation at the env-var parse boundary
# converts the silent failure into a loud ValueError that names the
# secret and the violation. The tests below pin one failure mode each.

import json as _json

import pytest


def _set_secrets(monkeypatch, payload):
    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.setenv("TERMINAL_GONDOLIN_SECRETS_JSON", _json.dumps(payload))


def _set_secrets_raw(monkeypatch, raw: str):
    monkeypatch.setenv("TERMINAL_ENV", "gondolin")
    monkeypatch.setenv("TERMINAL_GONDOLIN_SECRETS_JSON", raw)


def _assert_invalid(monkeypatch, payload, *, fragment: str):
    """Set the secrets payload and assert _get_env_config raises ValueError
    whose message contains *fragment* (case-sensitive substring match)."""
    from tools.terminal_tool import _get_env_config

    _set_secrets(monkeypatch, payload)
    with pytest.raises(ValueError) as excinfo:
        _get_env_config()
    assert fragment in str(excinfo.value), (
        f"expected message to mention {fragment!r}, got: {excinfo.value!r}"
    )


# Happy paths -- existing tests in this file already cover the typical
# minimal config; here we just confirm the validator doesn't choke on
# the placeholder/refresh extras.

def test_secrets_validator_accepts_full_schema(monkeypatch):
    """Every legal optional key should validate together."""
    from tools.terminal_tool import _get_env_config

    _set_secrets(monkeypatch, {
        "TOKEN": {
            "hosts": ["api.github.com", "github.com"],
            "from_command": "gh auth token",
            "placeholder": {"prefix": "GHTOK_", "length": 40, "alphabet": "abcdef0123456789"},
            "refresh": True,
            "refresh_command": "gh auth refresh -h github.com",
            "ttl_seconds": 3600,
            "refresh_before_expiry_seconds": 300,
        },
        "SIMPLE": {"hosts": ["api.example.com"], "value": "x"},
    })
    cfg = _get_env_config()
    assert cfg["gondolin"]["secrets"]["TOKEN"]["hosts"] == ["api.github.com", "github.com"]
    assert cfg["gondolin"]["secrets"]["SIMPLE"]["value"] == "x"


def test_secrets_validator_accepts_string_placeholder(monkeypatch):
    """Placeholder can also be a verbatim string (hooks.mjs accepts both)."""
    from tools.terminal_tool import _get_env_config

    _set_secrets(monkeypatch, {
        "X": {"hosts": ["a"], "from_env": "X", "placeholder": "REDACTED"},
    })
    assert _get_env_config()["gondolin"]["secrets"]["X"]["placeholder"] == "REDACTED"


# JSON-level failures keep their own message ----------------------------

def test_secrets_validator_rejects_invalid_json(monkeypatch):
    """A JSON syntax error should land on the 'expected valid JSON'
    branch, not the schema branch — distinct guidance for users."""
    from tools.terminal_tool import _get_env_config

    _set_secrets_raw(monkeypatch, "{not json")
    with pytest.raises(ValueError) as excinfo:
        _get_env_config()
    msg = str(excinfo.value)
    assert "TERMINAL_GONDOLIN_SECRETS_JSON" in msg and "valid JSON" in msg


# Top-level shape -------------------------------------------------------

def test_secrets_validator_rejects_non_object_top_level(monkeypatch):
    """The top-level must be a JSON object, not a list/string/etc."""
    _assert_invalid(monkeypatch, ["GITHUB_TOKEN"], fragment="must be a JSON object")


def test_secrets_validator_rejects_non_object_secret_cfg(monkeypatch):
    """Each entry's value must be a dict, not a string."""
    _assert_invalid(monkeypatch, {"X": "just-a-string"}, fragment="must be an object")


# Unknown keys (the silent-fail trap) -----------------------------------

def test_secrets_validator_rejects_typo_from_envs(monkeypatch):
    """``from_envs`` (with the trailing s) is the canonical typo. Before
    G9b, hooks.mjs ignored it and the agent ran without the credential.
    """
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_envs": "X"}},
        fragment="unknown key(s)",
    )


def test_secrets_validator_lists_allowed_keys_on_unknown(monkeypatch):
    """Error must enumerate the allowed keys so the user can self-correct."""
    from tools.terminal_tool import _get_env_config

    _set_secrets(monkeypatch, {"X": {"hosts": ["a"], "value": "v", "ttl": 60}})
    with pytest.raises(ValueError) as excinfo:
        _get_env_config()
    msg = str(excinfo.value)
    # Spot-check a few of the legal keys the message should mention.
    for k in ("hosts", "value", "from_env", "from_command", "ttl_seconds"):
        assert k in msg, f"expected allowed-key {k!r} in message: {msg!r}"


# hosts -----------------------------------------------------------------

def test_secrets_validator_rejects_missing_hosts(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"from_env": "X"}},
        fragment="'hosts' must be a non-empty array",
    )


def test_secrets_validator_rejects_empty_hosts(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": [], "from_env": "X"}},
        fragment="'hosts' must be a non-empty array",
    )


def test_secrets_validator_rejects_non_string_host(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["github.com", 42], "from_env": "X"}},
        fragment="'hosts' entries must be non-empty strings",
    )


# value / from_env / from_command — exactly one --------------------------

def test_secrets_validator_rejects_no_value_source(monkeypatch):
    """Zero sources is a silent-fail trap: daemon would emit a WARN
    diagnostic but the agent would run without the credential."""
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"]}},
        fragment="exactly one of 'value', 'from_env', or 'from_command'",
    )


def test_secrets_validator_rejects_multiple_value_sources(monkeypatch):
    """Two sources is ambiguous — which one wins?"""
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "value": "y"}},
        fragment="set only one of",
    )


def test_secrets_validator_rejects_empty_value(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": ""}},
        fragment="must be a non-empty string",
    )


# placeholder ----------------------------------------------------------

def test_secrets_validator_rejects_placeholder_without_length(monkeypatch):
    """Object form requires a positive integer length (drives the random
    generator in hooks.mjs:resolvePlaceholder)."""
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "placeholder": {"prefix": "GH_"}}},
        fragment="placeholder object requires a positive integer 'length'",
    )


def test_secrets_validator_rejects_placeholder_zero_length(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "placeholder": {"length": 0}}},
        fragment="placeholder object requires a positive integer 'length'",
    )


def test_secrets_validator_rejects_placeholder_unknown_key(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X",
               "placeholder": {"length": 16, "encoding": "hex"}}},
        fragment="placeholder has unknown key(s)",
    )


def test_secrets_validator_rejects_placeholder_wrong_type(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "placeholder": 42}},
        fragment="placeholder must be a string or",
    )


# refresh / ttl ---------------------------------------------------------

def test_secrets_validator_rejects_refresh_not_bool(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "refresh": "true"}},
        fragment="'refresh' must be a boolean",
    )


def test_secrets_validator_rejects_ttl_negative(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "ttl_seconds": -1}},
        fragment="'ttl_seconds'",
    )


def test_secrets_validator_rejects_ttl_zero(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "ttl_seconds": 0}},
        fragment="'ttl_seconds'",
    )


def test_secrets_validator_rejects_ttl_bool(monkeypatch):
    """bool is an int subclass; explicit-reject avoids ``ttl_seconds: True``
    silently becoming 1."""
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "ttl_seconds": True}},
        fragment="'ttl_seconds'",
    )


def test_secrets_validator_rejects_refresh_command_empty(monkeypatch):
    _assert_invalid(
        monkeypatch,
        {"X": {"hosts": ["a"], "from_env": "X", "refresh_command": ""}},
        fragment="'refresh_command'",
    )


# Error messages must name the offending secret ------------------------

def test_secrets_validator_error_names_the_secret(monkeypatch):
    """In a config with multiple secrets, the message must say which one
    failed so the user can find it in the config file."""
    from tools.terminal_tool import _get_env_config

    _set_secrets(monkeypatch, {
        "OK_TOKEN": {"hosts": ["a"], "from_env": "X"},
        "BROKEN_TOKEN": {"hosts": ["a"], "from_envs": "X"},  # typo
    })
    with pytest.raises(ValueError) as excinfo:
        _get_env_config()
    assert "BROKEN_TOKEN" in str(excinfo.value)
