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
