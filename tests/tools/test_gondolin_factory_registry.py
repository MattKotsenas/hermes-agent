"""Drift guard for tools.environments.gondolin_factory.

The previous regression that motivated the refactor: a
``terminal.gondolin.*`` key was documented, valid in YAML, and
forwarded by the env-var bridge — but the factory branch in
``terminal_tool._create_environment`` enumerated the keys it forwarded
by hand and just *didn't list it*. The user-supplied value silently
dropped at the factory bridge. Agents reported "wrote file ✓" while
the host path never saw the write.

The fix moved the per-key dispatch into ``GONDOLIN_FACTORY_KEYS``.
This test asserts the registry is honest: setting any registered key
in ``gondolin_config`` must materially affect the resulting
``daemon_config``. If somebody adds a key to the registry but forgets
to wire it (or breaks the wiring), one of these parameterized cases
goes red.
"""

from __future__ import annotations

import pytest

from tools.environments.gondolin_factory import (
    GONDOLIN_FACTORY_KEYS,
    GondolinFactoryKey,
    build_daemon_config,
    lookup_by_gc_key,
)


# Probe values exercise the "user set this to a non-default value"
# branch for every registered key. The exact value doesn't matter —
# only that it's distinguishable from the baseline (empty gc) output.
# Add a new row when you add a new GondolinFactoryKey; the
# test_registry_probe_table_is_exhaustive test below will tell you if
# you forgot.
_PROBE_VALUES: dict[str, object] = {
    "allowed_hosts": ["api.github.com", "github.com"],
    "secrets": {"GH_TOKEN": {"hosts": ["github.com"], "from_env": "GH_TOKEN"}},
    "policy_script": "/tmp/policy.sh",
    "extra_mounts": [
        {"host_path": "/h", "guest_path": "/g", "readonly": False},
    ],
    "project_skills": False,
    "project_credentials": False,
    "image": "mcr.example.com/img:1",
    "memory": "8G",
    "cpus": 4,
    "rootfs_size_mb": 20480,
    "max_concurrent_vms": 3,
    "lock_dir": "/tmp/custom-locks",
}


def _fake_ensure_image_built(image):
    # Identity for probe — the real implementation materializes an OCI
    # image and returns the gondolin-store tag. The registry test only
    # cares that the value flows through; format coercion is a separate
    # concern verified by the wiring test.
    return image or "default-image-tag"


def _fake_hermes_home():
    from pathlib import Path
    return Path("/tmp/fake-hermes-home")


def _build(gc, container_config=None):
    return build_daemon_config(
        gc,
        container_config or {},
        ensure_image_built=_fake_ensure_image_built,
        hermes_home=_fake_hermes_home,
        task_id="probe",
    )


def test_registry_is_non_empty():
    """Sanity: the registry must list at least the keys we currently
    forward. If somebody empties it, every backend-flip-to-gondolin
    flow degrades to defaults silently — better to fail loud here.
    """
    assert len(GONDOLIN_FACTORY_KEYS) >= 10, (
        f"GONDOLIN_FACTORY_KEYS shrunk unexpectedly to "
        f"{len(GONDOLIN_FACTORY_KEYS)} entries — did you mean to "
        f"delete keys instead of refactoring?"
    )


def test_registry_gc_keys_unique():
    """No two entries may share a ``gc_key`` — would cause the second
    to silently shadow the first inside ``build_daemon_config``.
    """
    gc_keys = [k.gc_key for k in GONDOLIN_FACTORY_KEYS]
    dupes = [k for k in gc_keys if gc_keys.count(k) > 1]
    assert not dupes, f"Duplicate gc_key in GONDOLIN_FACTORY_KEYS: {dupes}"


def test_probe_table_covers_every_registered_key():
    """Whenever a new ``GondolinFactoryKey`` is added, ``_PROBE_VALUES``
    must grow a matching entry — otherwise the parametrized drift
    test below silently skips the new key.
    """
    registered = {k.gc_key for k in GONDOLIN_FACTORY_KEYS}
    probed = set(_PROBE_VALUES.keys())
    missing_from_probes = registered - probed
    assert not missing_from_probes, (
        f"Add entries to _PROBE_VALUES for: {sorted(missing_from_probes)}. "
        f"Without them the drift-guard test silently skips these keys."
    )
    # Symmetric: stale probe entries (key removed from registry but
    # value left in the probe table) are confusing dead code.
    stale_probes = probed - registered
    assert not stale_probes, (
        f"Remove stale _PROBE_VALUES entries: {sorted(stale_probes)}"
    )


@pytest.mark.parametrize(
    "key",
    GONDOLIN_FACTORY_KEYS,
    ids=[k.gc_key for k in GONDOLIN_FACTORY_KEYS],
)
def test_every_registered_key_affects_daemon_config(key: GondolinFactoryKey):
    """The drift guard. For each registered key, setting it in ``gc``
    must produce an observably different ``daemon_config`` than the
    baseline (empty ``gc``).

    This is the test that would have caught the original ``extra_mounts``
    regression: with ``extra_mounts`` in the registry but absent from
    the factory dispatch, baseline and probe outputs would be byte-
    identical and the assertion would fail.
    """
    baseline = _build({})
    probe_value = _PROBE_VALUES[key.gc_key]
    probed = _build({key.gc_key: probe_value})
    assert probed != baseline, (
        f"Setting gondolin_config[{key.gc_key!r}] = {probe_value!r} "
        f"produced an identical daemon_config to the baseline. "
        f"This is the silent-drop regression class: the registry "
        f"claims the key is forwarded, but ``build_daemon_config`` "
        f"doesn't actually wire it. Fix the dispatch in "
        f"tools/environments/gondolin_factory.py."
    )


def test_lookup_by_gc_key_finds_every_registered_entry():
    """Lookup helper must agree with the registry — drift between the
    two would let other code (e.g. config validators) read a stale
    view.
    """
    for k in GONDOLIN_FACTORY_KEYS:
        assert lookup_by_gc_key(k.gc_key) is k


def test_lookup_by_gc_key_returns_none_for_unknown():
    assert lookup_by_gc_key("definitely-not-a-real-key") is None


# ─── Behavioural assertions for transforms (catch silent default drift) ──


def test_image_key_always_materializes_default():
    """The image key has no ``passthrough`` default — the transform
    must always populate ``daemon_config['image']``, even on empty
    ``gc``. Otherwise a session with no image config falls through
    to daemon-side failure instead of our build pipeline.
    """
    cfg = _build({})
    assert "image" in cfg
    assert cfg["image"] == "default-image-tag"


def test_lock_dir_always_materializes_default():
    """``lock_dir`` must always be set so the per-host VM cap works
    even when the user didn't configure anything. The transform
    falls back to a path under HERMES_HOME.
    """
    cfg = _build({})
    assert "lock_dir" in cfg
    assert "/tmp/fake-hermes-home" in cfg["lock_dir"]


def test_memory_falls_back_to_container_memory():
    """``terminal.container_memory`` (shared knob) drives gondolin's
    memory cap when ``terminal.gondolin.memory`` isn't set — same
    config shape as docker/singularity/modal/daytona.
    """
    cfg = _build({}, container_config={"container_memory": 4096})
    assert cfg.get("memory") == "4096M"


def test_cpus_falls_back_to_container_cpu_and_rounds():
    """``container_cpu`` is a float; gondolin needs an int. Round up
    so docker/gondolin parity isn't silently broken at 1.5 cores.
    """
    cfg = _build({}, container_config={"container_cpu": 1.5})
    assert cfg.get("cpus") == 2


def test_extra_mounts_omitted_when_unset():
    """``extra_mounts`` is the regression key — verify the omit-on-None
    behaviour explicitly so a default ever creeping in (e.g. ``[]``)
    doesn't shadow GondolinEnvironment's auto-derived mount logic.
    """
    cfg = _build({})
    assert "extra_mounts" not in cfg


def test_extra_mounts_defensively_copied():
    """Downstream code (``GondolinEnvironment.__init__``) mutates the
    list. The factory must copy so the caller's input isn't aliased.
    """
    user_mounts = [
        {"host_path": "/h", "guest_path": "/g", "readonly": False},
    ]
    cfg = _build({"extra_mounts": user_mounts})
    assert cfg["extra_mounts"] == user_mounts
    assert cfg["extra_mounts"] is not user_mounts


def test_project_skills_omitted_when_unset():
    """The gate is meaningful only when the user explicitly opted out;
    forwarding ``None`` would override GondolinEnvironment's default.
    """
    cfg = _build({})
    assert "project_skills" not in cfg
    assert "project_credentials" not in cfg


def test_project_skills_forwarded_when_explicitly_false():
    """``False`` is the meaningful opt-out value — verify it actually
    rides through to ``daemon_config`` (rather than being mistaken
    for "unset" because both look falsy).
    """
    cfg = _build({"project_skills": False, "project_credentials": False})
    assert cfg["project_skills"] is False
    assert cfg["project_credentials"] is False
