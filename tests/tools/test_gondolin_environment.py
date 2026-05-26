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
    import json
    import socket as _socket

    env = stub_env_factory(config={
        "secrets": {
            "GITHUB_TOKEN": {"hosts": ["github.com"], "value": "initial"},
        },
    })

    # Refresh via the Python helper.
    env.set_secret("GITHUB_TOKEN", value="refreshed")

    # Confirm via the daemon's stub-mode debug RPC (raw socket to avoid
    # circular dependency on the helper we just tested).
    def rpc(req):
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(env.sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.split(b"\n", 1)[0])

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
