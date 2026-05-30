"""Tests for GondolinEnvironment (the BaseEnvironment subclass).

The environment owns the per-session Gondolin daemon: it spawns the Node
daemon subprocess at construction time, sends an init RPC, and from then
on every ``_run_bash`` spawns the Python wrapper that talks to that
daemon over the AF_UNIX socket. ``cleanup()`` sends shutdown and tears
the daemon down.

These tests run the daemon in stub-VM mode so the suite stays VM-free.
A KVM-gated integration test is colocated for full-stack validation.
"""

from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path

import pytest

from tools.environments import gondolin as gondolin_mod
from tools.environments.gondolin import GondolinEnvironment


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = REPO_ROOT  # alias used by subprocess test bodies below
NODE_DAEMON = REPO_ROOT / "tools" / "environments" / "gondolin_host" / "src" / "daemon.mjs"
NODE_AVAILABLE = shutil.which("node") is not None and NODE_DAEMON.exists()

requires_node = pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")


@pytest.fixture
def stub_env_factory(tmp_path):
    """Factory that yields a GondolinEnvironment using the daemon in stub mode.

    Each invocation gets its own sandbox dir under tmp_path. Cleanup is
    automatic at fixture teardown.
    """
    created: list[GondolinEnvironment] = []

    def make(**kwargs):
        sandbox = tmp_path / f"sandbox-{len(created)}"
        env = GondolinEnvironment(
            sandbox_dir=str(sandbox),
            stub_vm=True,
            **kwargs,
        )
        created.append(env)
        return env

    yield make

    for env in created:
        try:
            env.cleanup()
        except Exception:
            pass


@requires_node
def test_construct_starts_daemon_and_opens_socket(stub_env_factory):
    """Constructing a GondolinEnvironment spawns the daemon and the
    socket file appears on disk in the sandbox dir."""
    env = stub_env_factory()
    assert env.sock_path is not None
    assert os.path.exists(env.sock_path), f"socket file missing at {env.sock_path}"
    # Can connect — proves daemon is listening.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(env.sock_path)
    s.close()


@requires_node
def test_run_bash_returns_popen_and_exec_succeeds(stub_env_factory):
    """_run_bash spawns the wrapper subprocess; draining its output returns
    the stubbed VM's echo of the command."""
    env = stub_env_factory()
    proc = env._run_bash("echo hello-from-env", timeout=10)
    stdout, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"wrapper exited {proc.returncode}: {stdout!r}"
    # Stub VM echoes the command back as stdout.
    assert "echo hello-from-env" in stdout


@requires_node
def test_cleanup_terminates_daemon(stub_env_factory):
    """After cleanup(), the socket is gone and a fresh _run_bash fails."""
    env = stub_env_factory()
    sock_path = env.sock_path
    assert os.path.exists(sock_path)
    env.cleanup()
    # Give the daemon a moment to exit + unlink the socket.
    for _ in range(20):
        if not os.path.exists(sock_path):
            break
        time.sleep(0.1)
    assert not os.path.exists(sock_path), "daemon socket still present after cleanup"


@requires_node
def test_two_environments_have_isolated_daemons(stub_env_factory):
    """Each GondolinEnvironment owns its own daemon process and socket.
    Two parallel envs do not collide."""
    a = stub_env_factory()
    b = stub_env_factory()
    assert a.sock_path != b.sock_path
    # Both work independently.
    pa = a._run_bash("echo A", timeout=10)
    pb = b._run_bash("echo B", timeout=10)
    stdout_a, _ = pa.communicate(timeout=10)
    stdout_b, _ = pb.communicate(timeout=10)
    assert "echo A" in stdout_a
    assert "echo B" in stdout_b


@requires_node
def test_daemon_init_failure_is_surfaced(tmp_path, monkeypatch):
    """If the daemon crashes during init (e.g. bad config), the env's
    constructor raises rather than handing back a broken object."""
    # Force the daemon to exit before serving by pointing it at a path
    # that doesn't exist as the policy script.
    sandbox = tmp_path / "sandbox"
    with pytest.raises(RuntimeError, match=r"gondolin|init|policy"):
        GondolinEnvironment(
            sandbox_dir=str(sandbox),
            stub_vm=False,  # force real init path so policy script gets loaded
            config={"policy_script": "/no/such/file.mjs"},
            init_timeout=5.0,
        )


@requires_node
def test_rootfs_size_error_gets_actionable_hint(tmp_path, monkeypatch):
    """When the daemon rejects a rootfs.size config because the image
    lacks e2fsprogs (or pinned rootfs.mode='memory'), the bare gondolin
    error doesn't tell the user what to do. Python prepends a hint that
    tells them to either remove `rootfs_size_mb` or use an image with
    e2fsprogs."""
    sandbox = tmp_path / "sandbox"

    def fake_rpc(sock_path, request, timeout=30.0):
        if request.get("method") == "init":
            return {
                "id": request.get("id"),
                "error": {
                    "message": "rootfs.size requires resize2fs in the guest image (install e2fsprogs)",
                },
            }
        return {"id": request.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(gondolin_mod, "_rpc_call", fake_rpc)
    with pytest.raises(RuntimeError) as excinfo:
        GondolinEnvironment(
            sandbox_dir=str(sandbox),
            stub_vm=True,
            config={"rootfs_size_mb": 10240},
            init_timeout=5.0,
        )
    msg = str(excinfo.value)
    assert "rootfs.size requires resize2fs" in msg
    assert "remove `rootfs_size_mb`" in msg
    assert "e2fsprogs" in msg


@requires_node
def test_missing_node_binary_raises_clear_error(monkeypatch, tmp_path):
    """If 'node' is not on PATH, GondolinEnvironment construction fails
    with a diagnostic that names the missing dependency."""
    monkeypatch.setattr(gondolin_mod.shutil, "which", lambda name: None if name == "node" else "/usr/bin/" + name)
    with pytest.raises(RuntimeError, match=r"[Nn]ode"):
        GondolinEnvironment(
            sandbox_dir=str(tmp_path / "sandbox"),
            stub_vm=True,
        )


@requires_node
def test_default_workspace_mount_wires_workspace_dir_to_cwd(stub_env_factory):
    """By default the env tells the daemon to bind the host ``workspace_dir``
    (a SUBDIR of ``sandbox_dir``) to the configured in-VM cwd via vfs.mounts.
    This is what makes file tools (read_file/write_file/patch) work against
    /workspace in the VM: the same bytes appear on the host under
    ``sandbox_dir/workspace/``. Per-session infra like ``gondolin.sock``
    stays at the sandbox_dir root and is NOT visible to the guest. The
    stub daemon echoes the resolved mount back in its init response; we
    assert against that."""
    env = stub_env_factory()
    # cwd defaults to /workspace.
    assert env.workspace_mount == {
        "guestPath": "/workspace",
        "hostPath": str(env.workspace_dir),
    }
    # workspace_dir is the 'workspace' subdir under sandbox_dir.
    assert env.workspace_dir == env.sandbox_dir / "workspace"
    assert env.workspace_dir.is_dir()


@requires_node
def test_custom_cwd_changes_workspace_mount_guest_path(stub_env_factory):
    """Changing cwd shifts where the workspace dir appears inside the VM,
    but the host path stays anchored at ``sandbox_dir/workspace/``."""
    env = stub_env_factory(cwd="/srv/work")
    assert env.workspace_mount == {
        "guestPath": "/srv/work",
        "hostPath": str(env.workspace_dir),
    }


@requires_node
def test_workspace_mount_can_be_disabled(stub_env_factory):
    """Setting workspace_mount=False in config skips the VFS wiring entirely.
    Power-user escape: someone hand-rolling vfs via a policy_script doesn't
    need our default mount and may want a stricter image-only filesystem.
    The host workspace_dir is still created on disk (no-op cost) — disabling
    the bind mount doesn't mean refusing to make the directory."""
    env = stub_env_factory(config={"workspace_mount": False})
    assert env.workspace_mount is None


@requires_node
def test_set_secret_routes_through_daemon_rpc(stub_env_factory):
    """The env exposes set_secret(name, value=..., hosts=...) which routes
    a set_secret RPC to the daemon's secretManager. Use case: a credential
    refresh loop (e.g. AAD token) updates the wire-injection value without
    restarting the VM."""
    import socket as _socket

    import msgpack

    env = stub_env_factory(config={
        "secrets": {
            "GITHUB_TOKEN": {"hosts": ["github.com"], "value": "initial"},
        },
    })

    # Refresh via the Python helper.
    env.set_secret("GITHUB_TOKEN", value="refreshed")

    # Confirm via the daemon's stub-mode debug RPC (raw socket to avoid
    # circular dependency on the helper we just tested). Wire is
    # length-prefixed msgpack — see tools/environments/gondolin_host/src/rpc.mjs.
    def rpc(req):
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(env.sock_path)
        payload = msgpack.packb(req, use_bin_type=True)
        assert payload is not None
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        buf = bytearray()
        while True:
            if len(buf) >= 4:
                n = int.from_bytes(buf[:4], "big")
                if len(buf) >= 4 + n:
                    break
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        s.close()
        n = int.from_bytes(buf[:4], "big")
        return msgpack.unpackb(bytes(buf[4:4 + n]), raw=False)

    peek = rpc({"id": 1, "method": "_debug_get_secret", "params": {"name": "GITHUB_TOKEN"}})
    assert peek.get("error") is None
    assert peek["result"]["value"] == "refreshed"


@requires_node
def test_set_secret_raises_on_unknown_name(stub_env_factory):
    """Unknown secret names surface as RuntimeError on the Python side."""
    env = stub_env_factory(config={"secrets": {}})
    with pytest.raises(RuntimeError, match=r"NEVER_DEFINED|unknown"):
        env.set_secret("NEVER_DEFINED", value="x")


# ---- Concurrency cap ---------------------------------------------------
#
# Each Gondolin VM costs ~256-512 MB on the host. A gateway hosting many
# parallel chats could exhaust memory; a configurable cap prevents that.
# In-process only (subagents + CLI live in different processes; a
# cross-process file lock is a deferred sub-item).

@requires_node
def test_concurrent_vm_cap_blocks_excess_envs(tmp_path, monkeypatch):
    """When the in-process VM count is already at the cap, constructing
    another GondolinEnvironment raises a RuntimeError that names the cap."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    # Force the cap down to 2 so we don't have to spawn N daemons.
    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 2)

    envs = []
    try:
        envs.append(GondolinEnvironment(
            sandbox_dir=str(tmp_path / "s0"), stub_vm=True,
        ))
        envs.append(GondolinEnvironment(
            sandbox_dir=str(tmp_path / "s1"), stub_vm=True,
        ))
        # Third should fail.
        with pytest.raises(RuntimeError, match=r"max_concurrent_vms|cap|limit|2"):
            GondolinEnvironment(
                sandbox_dir=str(tmp_path / "s2"), stub_vm=True,
            )
    finally:
        for e in envs:
            try: e.cleanup()
            except Exception: pass


@requires_node
def test_concurrent_vm_cap_releases_slot_on_cleanup(tmp_path, monkeypatch):
    """cleanup() releases the slot so the next env can be created. Without
    this, a cap of N would be a one-shot limit per process."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 1)

    e1 = GondolinEnvironment(sandbox_dir=str(tmp_path / "s0"), stub_vm=True)
    # Second under a cap of 1 must fail.
    with pytest.raises(RuntimeError):
        GondolinEnvironment(sandbox_dir=str(tmp_path / "s1"), stub_vm=True)
    e1.cleanup()
    # After cleanup the slot is free, so a fresh env constructs cleanly.
    e2 = GondolinEnvironment(sandbox_dir=str(tmp_path / "s2"), stub_vm=True)
    try:
        assert e2._daemon_proc is not None
    finally:
        e2.cleanup()


@requires_node
def test_concurrent_vm_cap_releases_slot_on_failed_init(tmp_path, monkeypatch):
    """If __init__ raises (daemon init fails), the slot must still be
    released — otherwise a flaky daemon could permanently consume slots."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 1)

    # Force a failure during init by pointing at a non-existent policy script
    # (and disabling stub_vm so the real init path runs).
    with pytest.raises(RuntimeError):
        GondolinEnvironment(
            sandbox_dir=str(tmp_path / "fail"),
            stub_vm=False,
            config={"policy_script": "/no/such/file.mjs"},
            init_timeout=5.0,
        )

    # Slot should be free; a clean stub env constructs.
    e = GondolinEnvironment(sandbox_dir=str(tmp_path / "ok"), stub_vm=True)
    e.cleanup()


@requires_node
def test_concurrent_vm_cap_disabled_when_zero_or_negative(tmp_path, monkeypatch):
    """Setting the cap to 0 (or negative) disables it entirely — the
    user explicitly opts out of any limit."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 0)

    envs = [
        GondolinEnvironment(sandbox_dir=str(tmp_path / f"s{i}"), stub_vm=True)
        for i in range(3)
    ]
    try:
        # No exception means cap is disabled. Sanity-check the count.
        assert len(envs) == 3
    finally:
        for e in envs:
            e.cleanup()


# ----------------------------------------------------------------------
# Same-sandbox-dir (same task_id) coexistence — regression for the
# multi-process daemon collision discovered 2026-05-30:
# - two processes both with task="default" → both daemons listen() on
#   the same gondolin.sock path
# - second daemon's bind() unlinks the first daemon's socket file
# - first daemon's listener still exists in the kernel but is unreachable
#   from the filesystem; the first agent reports "sandbox died" with a
#   socket error on the next call
# - the bandage today is "whoever spawned last wins, the other gets a
#   confused VM"
#
# Fix: each GondolinEnvironment instance gets its own per-instance
# subdirectory under sandbox_dir/, and its socket + overlay scratch
# live there. Multiple instances with the same sandbox_dir coexist
# without ever touching each other's runtime files.
# ----------------------------------------------------------------------


@requires_node
def test_two_envs_same_sandbox_dir_get_isolated_sockets(tmp_path, monkeypatch):
    """Two GondolinEnvironment instances sharing the same sandbox_dir
    (the on-disk equivalent of two processes both with task_id='default')
    MUST get separate socket paths so neither daemon unlinks the other's
    listener. Without this, the second instance's bind() steals the
    socket file and the first instance silently loses connectivity."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    # Disable the cap so both instances spawn without slot conflict.
    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 0)

    shared = str(tmp_path / "shared-task")
    env_a = GondolinEnvironment(sandbox_dir=shared, stub_vm=True)
    try:
        env_b = GondolinEnvironment(sandbox_dir=shared, stub_vm=True)
        try:
            # Per-instance: socket paths must differ.
            assert env_a.sock_path != env_b.sock_path, (
                f"both instances landed on the same socket path: "
                f"{env_a.sock_path!r} — second bind would unlink the first"
            )
            # Both socket files exist on disk.
            assert os.path.exists(env_a.sock_path), (
                f"env_a socket missing at {env_a.sock_path} — was it unlinked "
                f"when env_b started?"
            )
            assert os.path.exists(env_b.sock_path), (
                f"env_b socket missing at {env_b.sock_path}"
            )
            # Both daemons accept connections (proves neither was orphaned).
            for env in (env_a, env_b):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(env.sock_path)
                finally:
                    s.close()
            # Workspace dir is SHARED — that's the persistent contract
            # tied to task_id. Sanity check that we didn't accidentally
            # isolate that too.
            assert env_a.workspace_dir == env_b.workspace_dir, (
                "workspace_dir must stay shared across instances of the "
                "same sandbox_dir (it's the persistent-by-task contract)"
            )
        finally:
            env_b.cleanup()
    finally:
        env_a.cleanup()


@requires_node
def test_two_envs_same_sandbox_dir_overlay_scratch_isolated(tmp_path, monkeypatch):
    """When two instances share sandbox_dir and both configure an
    overlay extra_mount, their fuse-overlayfs upper/work scratch dirs
    must be in separate per-instance paths so they don't corrupt each
    other (two fuse-overlayfs processes writing to the same upper layer
    is a recipe for filesystem-level disaster)."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 0)

    # Skip if fuse-overlayfs isn't available — the overlay code path
    # short-circuits and there's nothing to test.
    if shutil.which("fuse-overlayfs") is None:
        pytest.skip("fuse-overlayfs not installed")

    # A real host dir for the overlay lower.
    lower = tmp_path / "lower"
    lower.mkdir()
    (lower / "preexisting.txt").write_text("hi\n")

    shared = str(tmp_path / "shared-task")
    overlay_cfg = {
        "extra_mounts": [
            {
                "host_path": str(lower),
                "guest_path": "/mnt/lower",
                "readonly": False,
                "overlay": True,
            }
        ],
        # Disable skill/credential projection so we only see the overlay we set up.
        "project_skills": False,
        "project_credentials": False,
    }

    env_a = GondolinEnvironment(sandbox_dir=shared, stub_vm=True, config=dict(overlay_cfg))
    try:
        env_b = GondolinEnvironment(sandbox_dir=shared, stub_vm=True, config=dict(overlay_cfg))
        try:
            # Overlay merged paths must differ (different upper/work dirs
            # → different merged mount points).
            assert env_a._overlay_mounts, "env_a should have at least one overlay mount"
            assert env_b._overlay_mounts, "env_b should have at least one overlay mount"
            assert set(env_a._overlay_mounts).isdisjoint(set(env_b._overlay_mounts)), (
                f"overlay scratch collided across instances: "
                f"env_a={env_a._overlay_mounts!r}, env_b={env_b._overlay_mounts!r}"
            )
        finally:
            env_b.cleanup()
    finally:
        env_a.cleanup()


def test_module_default_cap_enforces_a_nonzero_limit():
    """The default cap MUST be non-zero so the cross-process lock_dir
    machinery actually engages out of the box. A default of 0 leaves
    the cap disabled and the lock_dir decorative — that's the bug
    Phase 4 fixes."""
    from tools.environments import gondolin as gondolin_mod

    # Default is module-level; check the constant rather than the
    # runtime value (env var / monkeypatch may shift it in CI).
    assert gondolin_mod._DEFAULT_MAX_CONCURRENT_VMS > 0, (
        "default cap must be > 0 so the cross-process flock layer "
        "engages without explicit opt-in"
    )


@requires_node
def test_default_lock_dir_materializes_on_first_env(tmp_path, monkeypatch):
    """When the factory provides a lock_dir (the normal case) and the
    cap is the default, constructing a GondolinEnvironment must create
    the lock dir and write a slot file. Today this happens via
    _acquire_vm_slot's os.makedirs — this test locks it in."""
    from tools.environments import gondolin as gondolin_mod
    from tools.environments.gondolin import GondolinEnvironment

    lock_dir = tmp_path / "locks"
    assert not lock_dir.exists()
    # Force the default cap into effect (in case the env or another test
    # monkeypatched it elsewhere).
    monkeypatch.setattr(
        gondolin_mod,
        "_max_concurrent_vms",
        gondolin_mod._DEFAULT_MAX_CONCURRENT_VMS,
    )
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sb"),
        stub_vm=True,
        config={"lock_dir": str(lock_dir)},
    )
    try:
        assert lock_dir.is_dir(), (
            f"lock_dir not materialized at {lock_dir} — "
            f"_acquire_vm_slot should have os.makedirs'd it"
        )
        # Exactly one slot file should be flocked (this instance's).
        slot_files = list(lock_dir.glob("slot-*.lock"))
        assert len(slot_files) >= 1, (
            f"no slot files in {lock_dir}; flock layer didn't fire"
        )
    finally:
        env.cleanup()


# ----------------------------------------------------------------------
# Sweep ownership-awareness — protects against a second process's
# import-time sweep unmounting a live instance's fuse-overlayfs mount.
# These are pure-function tests against the helpers; the cross-process
# integration scenario is hard to exercise from a single test runner.
# ----------------------------------------------------------------------

def test_instance_is_alive_returns_true_for_own_pid(tmp_path):
    """A pidfile containing the test runner's own PID is by definition alive."""
    from tools.environments.gondolin import (
        _instance_is_alive,
        _INSTANCE_PIDFILE,
    )
    instance_dir = tmp_path / "live"
    instance_dir.mkdir()
    (instance_dir / _INSTANCE_PIDFILE).write_text(f"{os.getpid()}\n")
    assert _instance_is_alive(instance_dir) is True


def test_instance_is_alive_returns_false_for_dead_pid(tmp_path):
    """A pidfile pointing at a PID that doesn't exist must report dead.
    Picks a PID known to be free by walking up from 2**31 - 1 (max int32
    pid on Linux; never assigned)."""
    from tools.environments.gondolin import (
        _instance_is_alive,
        _INSTANCE_PIDFILE,
    )
    instance_dir = tmp_path / "dead"
    instance_dir.mkdir()
    # PIDs above /proc/sys/kernel/pid_max never exist; 2**22 is well
    # past the default 4M ceiling on most distros.
    fake_pid = 2**22
    (instance_dir / _INSTANCE_PIDFILE).write_text(f"{fake_pid}\n")
    assert _instance_is_alive(instance_dir) is False


def test_instance_is_alive_returns_false_for_missing_pidfile(tmp_path):
    """No pidfile at all → not a recognizable live instance (e.g. legacy
    pre-instance-split scratch dir, or partially-cleaned-up dir)."""
    from tools.environments.gondolin import _instance_is_alive
    instance_dir = tmp_path / "no_pidfile"
    instance_dir.mkdir()
    assert _instance_is_alive(instance_dir) is False


def test_instance_is_alive_returns_false_for_junk_pidfile(tmp_path):
    """A pidfile with non-integer content shouldn't crash the sweep; treat
    it as dead so the cleanup proceeds."""
    from tools.environments.gondolin import (
        _instance_is_alive,
        _INSTANCE_PIDFILE,
    )
    instance_dir = tmp_path / "junk"
    instance_dir.mkdir()
    (instance_dir / _INSTANCE_PIDFILE).write_text("not-a-pid\n")
    assert _instance_is_alive(instance_dir) is False


def test_find_owning_instance_dir_walks_up_to_instances_subdir(tmp_path):
    """Given a fuse-overlayfs merged path, the helper must return the
    per-instance dir (the grandparent of overlays/)."""
    from tools.environments.gondolin import (
        _find_owning_instance_dir,
        _INSTANCES_SUBDIR,
    )
    instance = tmp_path / "sb" / _INSTANCES_SUBDIR / "abc12345"
    mount = instance / "overlays" / "vault_xyz" / "merged"
    assert _find_owning_instance_dir(str(mount)) == instance


def test_find_owning_instance_dir_returns_none_for_legacy_layout(tmp_path):
    """Pre-instance-split mount paths (no `instances/` segment) return
    None — the sweep treats those as ownerless and proceeds to clean
    them up (which is the right behavior for legacy leaks)."""
    from tools.environments.gondolin import _find_owning_instance_dir
    legacy = tmp_path / "sb" / "overlays" / "vault_xyz" / "merged"
    assert _find_owning_instance_dir(str(legacy)) is None


# ---- Cross-process cap -------------------------------------------------
#
# The in-process cap above only blocks excess VMs from a single Python
# process. Real deployments have many: the CLI, subagents (each a fresh
# subprocess), the gateway, scheduled cron jobs. Without a host-wide cap,
# a fleet of subagents can blow past the configured limit and OOM the host.
#
# We use flock() on N slot files in a shared lock directory. The kernel
# releases flock on process exit, so we get crash-safe cleanup for free
# without PID liveness checks.

@requires_node
def test_concurrent_vm_cap_blocks_across_processes(tmp_path, monkeypatch):
    """A second Python process honoring the same cap cannot acquire a slot
    once the in-process count is at the limit. This is the cross-process
    case the in-process cap deliberately doesn't cover."""
    import subprocess as _sp
    import sys as _sys
    import textwrap as _tw

    from tools.environments.gondolin import GondolinEnvironment

    # Pin the in-process cap via monkeypatch so the config-knob path below
    # doesn't leak a mutation to subsequent tests in the same session.
    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 1)
    lock_dir = tmp_path / "gondolin-locks"

    # Hold one slot in *this* process via a stub env. The slot is registered
    # in the shared lock dir; the second process below must respect it.
    e1 = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "s0"),
        stub_vm=True,
        config={"lock_dir": str(lock_dir)},
    )
    try:
        script = _tw.dedent(f"""
            import sys
            sys.path.insert(0, {repr(str(_REPO_ROOT))})
            from tools.environments import gondolin as gmod
            gmod._max_concurrent_vms = 1
            from tools.environments.gondolin import GondolinEnvironment
            try:
                GondolinEnvironment(
                    sandbox_dir={repr(str(tmp_path / 's1'))},
                    stub_vm=True,
                    config={{"lock_dir": {repr(str(lock_dir))}}},
                )
            except RuntimeError as exc:
                print("BLOCKED:" + str(exc))
                sys.exit(0)
            print("ACQUIRED")
            sys.exit(1)
        """)
        result = _sp.run(
            [_sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"second process should have been blocked, got rc={result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "BLOCKED:" in result.stdout, result.stdout
    finally:
        e1.cleanup()


@requires_node
def test_concurrent_vm_cap_releases_across_processes_on_exit(tmp_path, monkeypatch):
    """When the first process exits (cleanup or hard kill), its slot frees up
    and a subsequent process can acquire it. flock() releases on process
    death — no zombie-slot tracking needed."""
    import subprocess as _sp
    import sys as _sys
    import textwrap as _tw

    from tools.environments.gondolin import GondolinEnvironment

    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 1)
    lock_dir = tmp_path / "gondolin-locks"

    # Process A: hold a slot, exit cleanly.
    holder_script = _tw.dedent(f"""
        import sys
        sys.path.insert(0, {repr(str(_REPO_ROOT))})
        from tools.environments import gondolin as gmod
        gmod._max_concurrent_vms = 1
        from tools.environments.gondolin import GondolinEnvironment
        env = GondolinEnvironment(
            sandbox_dir={repr(str(tmp_path / 'hold'))},
            stub_vm=True,
            config={{"lock_dir": {repr(str(lock_dir))}}},
        )
        env.cleanup()
    """)
    rc = _sp.run(
        [_sys.executable, "-c", holder_script], capture_output=True, text=True, timeout=30,
    )
    assert rc.returncode == 0, rc.stderr

    # Process B (us): slot should be free.
    env_b = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "after"),
        stub_vm=True,
        config={"lock_dir": str(lock_dir)},
    )
    env_b.cleanup()


@requires_node
def test_concurrent_vm_cap_config_knob_overrides_module_default(tmp_path, monkeypatch):
    """The user-facing config knob `max_concurrent_vms` overrides the
    module-level default. Restored via monkeypatch so the mutation doesn't
    leak across tests."""
    # Start clean: module default of 0 (disabled). The knob should bump it.
    monkeypatch.setattr(gondolin_mod, "_max_concurrent_vms", 0)

    e = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "knob"),
        stub_vm=True,
        config={"max_concurrent_vms": 5},
    )
    try:
        assert gondolin_mod._max_concurrent_vms == 5, (
            "config knob did not propagate to module-level cap"
        )
    finally:
        e.cleanup()


# ---- Secret diagnostics ------------------------------------------------
#
# from_command silently dropping unresolvable secrets is the right runtime
# behavior (agent runs without the credential rather than crashing) but
# terrible for unattended cron jobs where a token-fetch script breaking is
# the most common reason a credential isn't injected. The daemon surfaces
# a per-secret diagnostic; Python captures it on the env, logs at WARN,
# and `hermes doctor` shows it.

@requires_node
def test_secret_diagnostics_captured_from_init(tmp_path, caplog, monkeypatch):
    """A from_command that exits non-zero shows up on env.secret_diagnostics
    and is logged at WARNING. SECURITY: stderr/stdout captured from the
    failing helper are NOT included by default (they can contain partial
    secrets); opt in via HERMES_GONDOLIN_DEBUG_SECRETS=1 (see next test)."""
    import logging as _logging
    monkeypatch.delenv("HERMES_GONDOLIN_DEBUG_SECRETS", raising=False)

    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "diag"),
        stub_vm=True,
        config={
            "secrets": {
                "AAD_TOKEN": {
                    "hosts": ["login.microsoftonline.com"],
                    "from_command": "sh -c 'echo broken-creds-leak-marker >&2; exit 3'",
                },
            },
        },
    )
    try:
        assert len(env.secret_diagnostics) == 1
        d = env.secret_diagnostics[0]
        assert d["name"] == "AAD_TOKEN"
        assert d["type"] == "from_command"
        assert "3" in d["error"]
        # Default behavior: stderr/stdout NOT captured into the diagnostic.
        assert "stderr" not in d, (
            f"stderr leaked into diagnostic without debug opt-in: {d!r}"
        )
        assert "stdout" not in d, (
            f"stdout leaked into diagnostic without debug opt-in: {d!r}"
        )

        # And it was logged at WARNING, but the leak marker is NOT in the log.
        warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any("AAD_TOKEN" in r.getMessage() for r in warnings), (
            f"expected WARNING log mentioning AAD_TOKEN, got {[r.getMessage() for r in warnings]}"
        )
        for r in warnings:
            assert "broken-creds-leak-marker" not in r.getMessage(), (
                f"stderr leaked into errors.log via WARN: {r.getMessage()!r}"
            )
    finally:
        env.cleanup()


@requires_node
def test_secret_diagnostics_capture_stderr_with_debug_env(tmp_path, caplog, monkeypatch):
    """When HERMES_GONDOLIN_DEBUG_SECRETS=1, the failing-helper stderr lands
    in the diagnostic and the WARN log so operators can debug a broken
    refresh script. Host-side env var only."""
    import logging as _logging
    monkeypatch.setenv("HERMES_GONDOLIN_DEBUG_SECRETS", "1")

    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "diag-debug"),
        stub_vm=True,
        config={
            "secrets": {
                "AAD_TOKEN": {
                    "hosts": ["login.microsoftonline.com"],
                    "from_command": "sh -c 'echo broken-creds-debug-marker >&2; exit 3'",
                },
            },
        },
    )
    try:
        assert len(env.secret_diagnostics) == 1
        d = env.secret_diagnostics[0]
        assert "broken-creds-debug-marker" in d.get("stderr", "")

        warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any(
            "broken-creds-debug-marker" in r.getMessage() for r in warnings
        ), f"expected debug-mode stderr in WARN log, got {[r.getMessage() for r in warnings]}"
    finally:
        env.cleanup()


@requires_node
def test_secret_diagnostics_empty_when_all_resolved(tmp_path):
    """No diagnostics surface when every secret resolves cleanly."""
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "diag-ok"),
        stub_vm=True,
        config={
            "secrets": {
                "OK_LITERAL": {"hosts": ["x"], "value": "real-token"},
            },
        },
    )
    try:
        assert env.secret_diagnostics == []
    finally:
        env.cleanup()


# ---- Refresh-loop wiring -----------------------------------------------
#
# When a secret config has `refresh: true`, the env spins up a
# SecretRefresher to call set_secret() on schedule (JWT exp / configured
# ttl_seconds). Without `refresh: true` (default), no refresher is spawned.

@requires_node
def test_refresh_loop_spawns_when_secret_has_refresh_true(tmp_path):
    """`refresh: true` on a secret config spawns a SecretRefresher thread
    and stores it on the env. `cleanup()` stops it cleanly."""
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "refresh-on"),
        stub_vm=True,
        config={
            "secrets": {
                "AAD_TOKEN": {
                    "hosts": ["login.microsoftonline.com"],
                    "value": "initial-opaque-token",
                    "refresh": True,
                    "refresh_command": "echo new-aad-token",
                    "ttl_seconds": 3600,
                },
            },
        },
    )
    try:
        assert env.secret_refresher is not None
        assert env.secret_refresher.is_running()
    finally:
        env.cleanup()
        # cleanup stops the refresher.
        assert env.secret_refresher is None or not env.secret_refresher.is_running()


@requires_node
def test_refresh_loop_not_spawned_without_refresh_flag(tmp_path):
    """The default (no `refresh:` key) is no refresher — avoids the cost
    and the per-process thread for the 99% case where the user just wants
    a one-shot wire-injected credential."""
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "refresh-off"),
        stub_vm=True,
        config={
            "secrets": {
                "STATIC": {"hosts": ["x"], "value": "permanent-pat"},
            },
        },
    )
    try:
        assert env.secret_refresher is None
    finally:
        env.cleanup()


@requires_node
def test_refresh_loop_warns_when_refresh_true_but_no_command(tmp_path, caplog):
    """If `refresh: true` is set on a literal-`value` secret with no
    `refresh_command` or `from_command`, the env warns and refresh stays
    disabled — better than silently doing nothing."""
    import logging as _logging
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "refresh-misconfig"),
        stub_vm=True,
        config={
            "secrets": {
                "BAD": {
                    "hosts": ["x"],
                    "value": "literal",
                    "refresh": True,
                    # no refresh_command, no from_command
                },
            },
        },
    )
    try:
        assert env.secret_refresher is None
        warnings = [r for r in caplog.records if r.levelno >= _logging.WARNING]
        assert any("refresh_command" in r.getMessage() for r in warnings), (
            f"expected WARN mentioning refresh_command, got "
            f"{[r.getMessage() for r in warnings]}"
        )
    finally:
        env.cleanup()


# ---- Streaming exec --------------------------------------------------------
#
# _run_bash passes --stream to the wrapper by default so the agent sees
# stdout chunks live. Config knob `stream: false` opts out (one-shot exec
# for snapshot-prelude calls or environments where chunk timing matters).

@requires_node
def test_run_bash_defaults_to_streaming(tmp_path):
    """A default-config env wires --stream into the wrapper argv."""
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "stream-default"),
        stub_vm=True,
    )
    try:
        proc = env._run_bash("STREAM:hello|world", timeout=30)
        proc.wait(timeout=10)
        out = proc.stdout.read() if proc.stdout else ""
        # The stub VM streams each segment as a chunk; output concatenates them.
        assert "hello" in out
        assert "world" in out
        assert proc.returncode == 0
    finally:
        env.cleanup()


@requires_node
def test_run_bash_can_opt_out_of_streaming(tmp_path):
    """`stream: false` falls back to the one-shot exec path."""
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "stream-off"),
        stub_vm=True,
        config={"stream": False},
    )
    try:
        proc = env._run_bash("echo hi", timeout=30)
        proc.wait(timeout=10)
        out = proc.stdout.read() if proc.stdout else ""
        # Non-streaming stub echoes the wrapped cmd back as stdout.
        assert "echo hi" in out
        assert proc.returncode == 0
    finally:
        env.cleanup()


# ---- Skill + credential file projection ----------------------------------
#
# docker/singularity/modal bind-mount ~/.hermes/skills/ and individual
# credential files (OAuth tokens, etc.) read-only into the sandbox via
# tools/credential_files.py.  GondolinEnvironment must do the same via
# extra_mounts so terminal.backend=gondolin reaches parity with the other
# remote backends without per-skill changes.

@requires_node
def test_skill_directory_mounts_are_projected_by_default(stub_env_factory, monkeypatch, tmp_path):
    """get_skills_directory_mount() entries land in extra_mounts as
    read-only directory mounts."""
    skills_host = tmp_path / "skills"; skills_host.mkdir()
    ext_host = tmp_path / "external_skills" / "0"; ext_host.mkdir(parents=True)
    fake_skill_mounts = [
        {"host_path": str(skills_host), "container_path": "/root/.hermes/skills"},
        {"host_path": str(ext_host), "container_path": "/root/.hermes/external_skills/0"},
    ]
    monkeypatch.setattr(
        "tools.credential_files.get_skills_directory_mount",
        lambda **kw: fake_skill_mounts,
    )
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [],
    )
    env = stub_env_factory()
    mounts = env.config.get("extra_mounts") or []
    guest_paths = sorted(m["guest_path"] for m in mounts)
    assert guest_paths == ["/root/.hermes/external_skills/0", "/root/.hermes/skills"]
    assert all(m["readonly"] is True for m in mounts)


@requires_node
def test_credential_files_grouped_by_parent_dir(stub_env_factory, monkeypatch, tmp_path):
    """Individual credential files mount as their parent directory with an
    allow-list of the visible filenames.

    Gondolin's RealFSProvider takes a directory rootPath, so a per-file
    bind-mount á la docker -v $f:$g:ro doesn't translate. GondolinEnvironment
    groups credentials by guest parent path and mounts that directory
    read-only — but every other file in the parent dir (state.json,
    migration_state, leftover-debug.log) MUST be shadowed so the agent
    sees only the credentials it's supposed to. The daemon receives a
    per-mount `allowed_files` list and constructs a ShadowProvider that
    surfaces siblings as ENOENT (see daemon.mjs buildExtraMountProvider).
    """
    gcloud_dir = tmp_path / "gcloud"; gcloud_dir.mkdir()
    (gcloud_dir / "credentials.json").write_text("{}")
    (gcloud_dir / "access_tokens.db").write_text("")
    # A sibling that must NOT be exposed.
    (gcloud_dir / "leftover-debug.log").write_text("secrets-in-debug-log")
    op_dir = tmp_path / "op"; op_dir.mkdir()
    (op_dir / "session.json").write_text("{}")
    fake_credentials = [
        {"host_path": str(gcloud_dir / "credentials.json"), "container_path": "/root/.config/gcloud/credentials.json"},
        # A second file under the same guest parent — should collapse to one mount.
        {"host_path": str(gcloud_dir / "access_tokens.db"), "container_path": "/root/.config/gcloud/access_tokens.db"},
        # Different guest parent — gets its own mount.
        {"host_path": str(op_dir / "session.json"), "container_path": "/root/.op/session.json"},
    ]
    monkeypatch.setattr(
        "tools.credential_files.get_skills_directory_mount",
        lambda **kw: [],
    )
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: fake_credentials,
    )
    env = stub_env_factory()
    mounts = env.config.get("extra_mounts") or []
    guest_paths = sorted(m["guest_path"] for m in mounts)
    assert guest_paths == ["/root/.config/gcloud", "/root/.op"]
    assert all(m["readonly"] is True for m in mounts)

    # SECURITY: each credential mount carries an allowed_files allowlist.
    # The daemon's ShadowProvider treats anything NOT in this list as
    # ENOENT, so the agent cannot read sibling files in the parent dir
    # (state.json, leftover-debug.log, etc).
    by_guest = {m["guest_path"]: m for m in mounts}
    gcloud_mount = by_guest["/root/.config/gcloud"]
    assert "allowed_files" in gcloud_mount, (
        f"mount missing allowed_files (sibling files would be exposed): {gcloud_mount!r}"
    )
    assert sorted(gcloud_mount["allowed_files"]) == [
        "/access_tokens.db", "/credentials.json",
    ]
    op_mount = by_guest["/root/.op"]
    assert "allowed_files" in op_mount
    assert op_mount["allowed_files"] == ["/session.json"]


@requires_node
def test_projection_can_be_disabled(stub_env_factory, monkeypatch, tmp_path):
    """Setting project_skills/project_credentials to False suppresses
    the auto-projection — useful for paranoid configs."""
    skills_host = tmp_path / "skills"; skills_host.mkdir()
    cred_dir = tmp_path / "creds"; cred_dir.mkdir()
    (cred_dir / "f").write_text("")
    monkeypatch.setattr(
        "tools.credential_files.get_skills_directory_mount",
        lambda **kw: [{"host_path": str(skills_host), "container_path": "/y"}],
    )
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{"host_path": str(cred_dir / "f"), "container_path": "/c/d"}],
    )
    env = stub_env_factory(config={"project_skills": False, "project_credentials": False})
    assert "extra_mounts" not in env.config


# ---- container_persistent lifecycle ---------------------------------------
#
# Docker's container_persistent: True/False contract is about whether the
# per-task sandbox bind dirs survive cleanup. Persistence of the rootfs
# layer itself (apt-installs across sessions) is a separate feature docker
# also doesn't ship today. Gondolin matches docker's bind-dir contract:
# workspace_dir survives cleanup when persistent=True, gets rm'd when
# persistent=False. The workspace_dir is where the agent does its actual
# work, so this controls whether code/edits survive between sessions.

@requires_node
def test_workspace_dir_survives_cleanup_when_persistent(tmp_path):
    """persistent_filesystem=True: workspace contents remain on disk after
    cleanup so a subsequent GondolinEnvironment for the same task_id sees
    the agent's prior files. Matches docker's container_persistent=True
    behavior."""
    sandbox = tmp_path / "task-persist"
    env = GondolinEnvironment(
        sandbox_dir=str(sandbox),
        stub_vm=True,
        persistent_filesystem=True,
    )
    workspace = env.workspace_dir
    (workspace / "agent-output.txt").write_text("important-work-product")
    env.cleanup()

    # Dir + content survive.
    assert workspace.exists()
    assert (workspace / "agent-output.txt").read_text() == "important-work-product"


@requires_node
def test_workspace_dir_removed_on_cleanup_when_ephemeral(tmp_path):
    """persistent_filesystem=False: workspace dir is rm'd on cleanup. Matches
    docker's container_persistent=False behavior (sandbox dirs removed
    alongside `docker rm -f`)."""
    sandbox = tmp_path / "task-ephemeral"
    env = GondolinEnvironment(
        sandbox_dir=str(sandbox),
        stub_vm=True,
        persistent_filesystem=False,
    )
    workspace = env.workspace_dir
    (workspace / "throwaway.txt").write_text("ephemeral")
    assert workspace.exists()
    env.cleanup()

    # workspace_dir is gone.
    assert not workspace.exists(), (
        f"non-persistent workspace must be removed; found {list(sandbox.iterdir())}"
    )


@requires_node
def test_persistent_default_is_false_for_safety(tmp_path):
    """Default is persistent_filesystem=False so a constructor with no
    explicit persistence arg gets ephemeral semantics — matches the safer
    of the two failure modes (a stale workspace dir leaking content into
    the next task is worse than re-creating an empty one). The
    factory/terminal_tool layer is responsible for setting True when the
    user opts in via container_persistent."""
    sandbox = tmp_path / "task-default"
    env = GondolinEnvironment(sandbox_dir=str(sandbox), stub_vm=True)
    workspace = env.workspace_dir
    (workspace / "marker.txt").write_text("x")
    env.cleanup()
    assert not workspace.exists()



# ---- _run_bash login threading -----------------------------------------
#
# BaseEnvironment hands ``login=True`` to ``_run_bash`` only during
# ``init_session`` (snapshot capture) and ``login=False`` for every
# subsequent command. Each backend’s ``_run_bash`` is responsible
# for translating that into the right shell invocation. For gondolin the
# right invocation is to forward ``--login`` to the wrapper subprocess,
# which in turn ships ``params.login=True`` on the RPC, which lets the
# daemon build ``[bash, "-l", "-c", cmd]`` argv instead of the
# default ``[bash, "-c", cmd]``.
#
# Prior to this contract, gondolin’s ``_run_bash`` silently
# discarded the login flag (the docstring openly admitted "has no
# effect") and the SDK’s default ``/bin/sh -lc`` wrap fired
# /etc/profile on every command — leaking /opt/conda/bin/xz from
# images like devcontainers/universal:6 whose nvs.sh uses a bashism
# (``&>``) in a dash-sourced script.


def test_run_bash_appends_login_flag_to_wrapper_argv_when_login_true(
    stub_env_factory, monkeypatch
):
    """login=True must surface as ``--login`` in the wrapper subprocess argv
    so the wire-level ``params.login`` is set on the exec RPC."""
    env = stub_env_factory()
    captured: list[list[str]] = []

    def fake_popen(argv, stdin_data=None):
        captured.append(list(argv))
        # Return any minimal object; the test never reads it.
        class _Stub:
            pass
        return _Stub()

    monkeypatch.setattr(gondolin_mod, "_popen_bash", fake_popen)
    env._run_bash("echo hi", login=True, timeout=5)

    assert len(captured) == 1, f"expected exactly one _popen_bash call, got {captured}"
    argv = captured[0]
    assert "--login" in argv, (
        f"login=True must add --login to wrapper argv, got: {argv}"
    )


def test_run_bash_omits_login_flag_when_login_false(stub_env_factory, monkeypatch):
    """login=False (the default for every steady-state command) must NOT
    add --login. Otherwise profile.d fires on every call — defeating
    the BaseEnvironment snapshot mechanism that’s supposed to source
    profile once per session."""
    env = stub_env_factory()
    captured: list[list[str]] = []

    def fake_popen(argv, stdin_data=None):
        captured.append(list(argv))
        class _Stub:
            pass
        return _Stub()

    monkeypatch.setattr(gondolin_mod, "_popen_bash", fake_popen)
    env._run_bash("echo hi", login=False, timeout=5)

    assert len(captured) == 1
    argv = captured[0]
    assert "--login" not in argv, (
        f"login=False must not add --login to wrapper argv, got: {argv}"
    )
