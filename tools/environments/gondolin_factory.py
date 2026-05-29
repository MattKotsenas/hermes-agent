"""Registry of ``terminal.gondolin.*`` keys consumed by the factory.

Companion to ``config_bridge`` (which owns ``terminal.gondolin.* → env
var`` mapping). This module owns the *second* bridge:
``gondolin_config (gc) dict → daemon_config dict`` inside
``terminal_tool._create_environment``.

Why this exists
---------------

Before this refactor ``_create_environment``'s gondolin branch
hand-enumerated the keys it forwarded into ``daemon_config``::

    daemon_config = {
        "allowed_hosts": gc.get("allowed_hosts", ["*"]),
        "secrets":       gc.get("secrets", {}),
        "policy_script": gc.get("policy_script"),
    }
    # extra_mounts, project_skills, project_credentials, image,
    # memory, cpus, rootfs_size_mb, max_concurrent_vms, lock_dir
    # each handled as a separate ad-hoc ``if "foo" in gc:`` block.

The regression that motivated commit 34d25f7fb was exactly this shape:
``extra_mounts`` was a documented key, valid in ``config.yaml`` and
already forwarded through the env-var bridge, but the factory forgot
the ``daemon_config["extra_mounts"] = …`` line. Every user-supplied
vault mount silently dropped on the floor; agents reported "wrote
file ✓" while the host path never saw the write.

The wiring tests passed because they enumerated keys the factory *did*
forward — which is exactly the set the factory always had. The bug
fell into the seam between two layers that were each individually
"tested."

The fix is to make the registry the source of truth at this layer too.
``test_gondolin_factory_registry.py`` asserts that **every registered
key materially affects ``daemon_config`` when set in ``gc``**, so a
new key added in one place but not the other fails a test immediately
instead of degrading to silent data loss in production.

Two entry shapes
----------------

``PASSTHROUGH`` (most keys): take ``gc[key]`` verbatim and write to
``daemon_config[key]`` (or a renamed key). Optional ``default`` is
materialized when ``gc.get(key) is None``; a key with no default and
no user value is omitted from ``daemon_config`` entirely.

``TRANSFORMED`` (image / memory / cpus / rootfs / lock_dir): the value
goes through a named callable that takes ``(gc, container_config,
ctx)`` and returns a mapping to merge into ``daemon_config``. Returning
``{}`` means "nothing to forward for this key with this input" — the
default for "user didn't set anything and there's no shared-knob
fallback either."

The transform callable is responsible for the full decision tree
(fallback to ``container_config``, format coercion, default
materialization). The registry still owns the *fact* that the key
exists; the test still passes through every transformed key and
asserts that setting ``gc[key]`` to a representative value produces
a non-empty change in ``daemon_config``.

Drift protection
----------------

``test_gondolin_factory_registry.py`` runs ``build_daemon_config``
once with an empty ``gc`` to capture the baseline, then once per
registered key with that key set to a probe value, and asserts the
output differs. Any future key added to the registry without a
corresponding entry in the factory dispatch will fail this test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class GondolinFactoryKey:
    """One ``gondolin_config`` key and how it bridges into ``daemon_config``.

    Attributes:
        gc_key: Name of the key on the input ``gondolin_config`` dict
            (matches the YAML leaf under ``terminal.gondolin.*``).
        daemon_key: Name of the key on the output ``daemon_config`` dict.
            Usually the same as ``gc_key``; differs for renames.
        shape: ``"passthrough"`` (verbatim with optional default) or
            ``"transformed"`` (call ``transform`` for full control).
        default: Materialized into ``daemon_config[daemon_key]`` when
            ``gc.get(gc_key) is None``. ``_OMIT`` (the default sentinel)
            means "omit from daemon_config when user didn't set it."
            Passthrough-only; transformed keys handle defaults inside
            their callable.
        transform: ``(gc, container_config, ctx) → Mapping`` returning
            a dict to merge into ``daemon_config``. Transformed-only.
            Return ``{}`` to forward nothing for this input.
    """

    gc_key: str
    daemon_key: str
    shape: str  # "passthrough" | "transformed"
    default: Any = None  # _OMIT or a concrete value; ignored when shape=transformed
    transform: Callable[[Mapping, Mapping, Mapping], Mapping] | None = None


# Sentinel — distinguishes "no default, omit the key" from "default is
# None/0/[]/etc., set it explicitly." Cannot use None because some
# daemon keys legitimately accept None as a meaningful value.
class _OmitSentinel:
    def __repr__(self) -> str:
        return "<OMIT>"


_OMIT = _OmitSentinel()


# ─── Transform callables ──────────────────────────────────────────────────
#
# Each takes ``(gc, container_config, ctx)``:
#   gc: gondolin_config dict (terminal.gondolin.* values)
#   container_config: shared container_config dict (terminal.container_* values)
#   ctx: dict with factory-injected helpers — currently:
#     - "ensure_image_built": Callable[[str|None], str] for image materialization
#     - "hermes_home": Callable[[], Path] for default sandbox/lock dirs
#     - "task_id": str for per-task sandbox path defaults
#
# Returns a mapping to merge into daemon_config. Empty mapping means
# "nothing to forward for this input" (e.g. user didn't override and
# no shared-knob fallback applied).


def _xform_image(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.image`` → ``daemon_config['image']``.

    Always required end-to-end. ``ensure_image_built`` materializes
    the OCI image on first use and returns the gondolin-store tag.
    """
    return {"image": ctx["ensure_image_built"](gc.get("image"))}


def _xform_memory(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.memory`` or fallback to ``container_memory``.

    Gondolin's native shape is a string with qemu suffix (``"5120M"``,
    ``"4G"``). When user sets only the shared ``container_memory`` MB
    int, we translate it. Omitted entirely when neither is set.
    """
    gondolin_mem = gc.get("memory")
    if not gondolin_mem:
        container_mem_mb = cc.get("container_memory")
        if container_mem_mb is not None:
            try:
                gondolin_mem = f"{int(container_mem_mb)}M"
            except (TypeError, ValueError):
                gondolin_mem = None
    return {"memory": gondolin_mem} if gondolin_mem else {}


def _xform_cpus(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.cpus`` or fallback to ``container_cpu``.

    Gondolin wants an integer count. The shared ``container_cpu`` knob
    is a float; round it (1.5 → 2) so users who set 1.5 for docker
    don't silently get nothing here. Omitted when neither is set.
    """
    gondolin_cpus = gc.get("cpus")
    if gondolin_cpus is None:
        container_cpu = cc.get("container_cpu")
        if container_cpu is not None:
            try:
                gondolin_cpus = max(1, round(float(container_cpu)))
            except (TypeError, ValueError):
                gondolin_cpus = None
    return {"cpus": int(gondolin_cpus)} if gondolin_cpus is not None else {}


def _xform_rootfs_size_mb(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.rootfs_size_mb`` — opt-in disk cap.

    No fallback to ``container_disk`` — the semantics differ (see the
    explanatory comment in terminal_tool.py).
    """
    v = gc.get("rootfs_size_mb")
    if v is None:
        return {}
    return {"rootfs_size_mb": int(v)}


def _xform_max_concurrent_vms(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.max_concurrent_vms`` — host-wide VM cap."""
    v = gc.get("max_concurrent_vms")
    if v is None:
        return {}
    return {"max_concurrent_vms": int(v)}


def _xform_lock_dir(gc: Mapping, cc: Mapping, ctx: Mapping) -> Mapping:
    """``terminal.gondolin.lock_dir`` — VM cap lock directory.

    Always materialized: defaults to ``$HERMES_HOME/sandboxes/gondolin/.locks``
    so the cap applies across CLI, gateway, cron, subagents by default.
    """
    lock_dir = gc.get("lock_dir")
    if not lock_dir:
        lock_dir = str(ctx["hermes_home"]() / "sandboxes" / "gondolin" / ".locks")
    return {"lock_dir": lock_dir}


# ─── The registry ─────────────────────────────────────────────────────────
#
# Order mirrors the docs / config.yaml example block ordering.

GONDOLIN_FACTORY_KEYS: tuple[GondolinFactoryKey, ...] = (
    # Allow-list of network hosts. Default ["*"] = open (sandbox-trust model).
    GondolinFactoryKey("allowed_hosts", "allowed_hosts", "passthrough", default=["*"]),
    # Per-secret declarations. Default {} = no secrets injected.
    GondolinFactoryKey("secrets", "secrets", "passthrough", default={}),
    # Policy script path (None = no policy).
    GondolinFactoryKey("policy_script", "policy_script", "passthrough", default=None),
    # User-supplied bind mounts. Omitted from daemon_config when not set
    # (the env appends auto-derived skill/credential mounts on top).
    GondolinFactoryKey("extra_mounts", "extra_mounts", "passthrough", default=_OMIT),
    # Opt-out gates for auto-derived mounts; omitted when not set so
    # GondolinEnvironment uses its own default-on behaviour.
    GondolinFactoryKey("project_skills", "project_skills", "passthrough", default=_OMIT),
    GondolinFactoryKey("project_credentials", "project_credentials", "passthrough", default=_OMIT),
    # Image — required; transform materializes the build.
    GondolinFactoryKey("image", "image", "transformed", transform=_xform_image),
    # Resource caps — shared-knob fallbacks live in the transforms.
    GondolinFactoryKey("memory", "memory", "transformed", transform=_xform_memory),
    GondolinFactoryKey("cpus", "cpus", "transformed", transform=_xform_cpus),
    GondolinFactoryKey("rootfs_size_mb", "rootfs_size_mb", "transformed", transform=_xform_rootfs_size_mb),
    GondolinFactoryKey("max_concurrent_vms", "max_concurrent_vms", "transformed", transform=_xform_max_concurrent_vms),
    GondolinFactoryKey("lock_dir", "lock_dir", "transformed", transform=_xform_lock_dir),
)


def build_daemon_config(
    gc: Mapping | None,
    container_config: Mapping | None,
    *,
    ensure_image_built: Callable[[Any], str],
    hermes_home: Callable[[], Any],
    task_id: str = "default",
) -> dict:
    """Apply the registry to build a ``daemon_config`` from inputs.

    Args:
        gc: ``terminal.gondolin.*`` dict (the ``gondolin_config`` the
            terminal-tool factory receives). ``None`` is treated as
            ``{}``.
        container_config: shared ``terminal.container_*`` dict. ``None``
            is treated as ``{}``. Consulted by transformed keys for
            fallback values (memory / cpus).
        ensure_image_built: Callable that takes a user-supplied image
            name (or ``None`` for the default) and returns the gondolin
            image tag the daemon should use. Injected so this module
            doesn't take a hard dependency on ``terminal_tool``.
        hermes_home: Zero-arg callable returning the active Hermes home
            ``Path``. Injected for the same reason.
        task_id: Per-task identifier used by transforms that derive
            default paths. Not currently used by any built-in transform
            but kept in the context dict for future keys (e.g. per-task
            sandbox_dir).

    Returns:
        A new ``dict`` ready to hand to ``GondolinEnvironment(config=...)``
        as the daemon's init config.
    """
    gc = dict(gc or {})
    cc = dict(container_config or {})
    ctx = {
        "ensure_image_built": ensure_image_built,
        "hermes_home": hermes_home,
        "task_id": task_id,
    }
    daemon_config: dict[str, Any] = {}
    for key in GONDOLIN_FACTORY_KEYS:
        if key.shape == "passthrough":
            value = gc.get(key.gc_key)
            if value is None:
                if isinstance(key.default, _OmitSentinel):
                    continue  # user didn't set, no default → omit
                value = key.default
            # ``extra_mounts`` is the one key we copy defensively
            # (downstream code mutates it). Everything else is forwarded
            # by reference; the daemon-config dict is consumed once and
            # discarded by GondolinEnvironment.__init__.
            if key.gc_key == "extra_mounts":
                value = list(value)
            daemon_config[key.daemon_key] = value
        elif key.shape == "transformed":
            assert key.transform is not None, f"transformed key {key.gc_key!r} missing transform"
            daemon_config.update(key.transform(gc, cc, ctx))
        else:
            raise AssertionError(f"unknown shape {key.shape!r} for {key.gc_key!r}")
    return daemon_config


def lookup_by_gc_key(gc_key: str) -> GondolinFactoryKey | None:
    """Find a registered entry by its ``gc_key``."""
    for k in GONDOLIN_FACTORY_KEYS:
        if k.gc_key == gc_key:
            return k
    return None
