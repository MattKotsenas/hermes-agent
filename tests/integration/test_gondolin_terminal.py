"""KVM-gated end-to-end integration test for GondolinEnvironment.

Exercises the full Hermes execution path through a real Gondolin VM:
- GondolinEnvironment.__init__ spawns daemon, boots real VM
- BaseEnvironment.init_session captures env snapshot inside the VM
- BaseEnvironment.execute runs a command, parses CWD marker
- cleanup() tears the whole stack down

These are slow (~30s each) so they live in tests/integration/ — they're
NOT part of the default test run unless QEMU/KVM are available, and
they auto-skip otherwise. Run explicitly with:

    pytest tests/integration/test_gondolin_terminal.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NODE_DAEMON = REPO_ROOT / "tools" / "environments" / "gondolin_host" / "src" / "daemon.mjs"

_HAS_KVM = os.path.exists("/dev/kvm")
_HAS_QEMU = shutil.which("qemu-system-x86_64") is not None
_HAS_NODE = shutil.which("node") is not None and NODE_DAEMON.exists()
_HAS_GONDOLIN_DEPS = _HAS_KVM and _HAS_QEMU and _HAS_NODE

requires_gondolin = pytest.mark.skipif(
    not _HAS_GONDOLIN_DEPS,
    reason="Gondolin integration needs /dev/kvm + qemu-system-x86_64 + node",
)


@pytest.fixture
def gondolin_env(tmp_path):
    """A real GondolinEnvironment with a real VM. ~16s cold boot."""
    from tools.environments.gondolin import GondolinEnvironment
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sandbox"),
        cwd="/root",  # use a path that exists inside the helper image
        timeout=60,
        init_timeout=120.0,
        stub_vm=False,
    )
    try:
        yield env
    finally:
        env.cleanup()


@requires_gondolin
def test_execute_runs_a_command_inside_the_vm(gondolin_env):
    """The agent-facing execute() roundtrip works: shell evaluates and
    output comes back."""
    result = gondolin_env.execute("echo hello-from-real-vm")
    assert result["returncode"] == 0, f"non-zero exit: {result}"
    assert "hello-from-real-vm" in result["output"]


@requires_gondolin
def test_session_snapshot_persists_env_vars_across_calls(gondolin_env):
    """init_session captures an env snapshot. A var exported in one call
    is visible in a subsequent call only if BaseEnvironment's snapshot
    machinery is working through the gondolin transport."""
    # First call: capture snapshot (BaseEnvironment.init_session)
    gondolin_env.init_session()
    # Second call: read a value the snapshot should have captured.
    # PATH is set by the helper rootfs on first call and should persist.
    result = gondolin_env.execute("echo PATH_LEN=$(echo -n $PATH | wc -c)")
    assert result["returncode"] == 0
    assert "PATH_LEN=" in result["output"]
    # The number after PATH_LEN= must be > 0 (PATH is non-empty).
    line = [l for l in result["output"].splitlines() if "PATH_LEN=" in l][0]
    path_len = int(line.split("PATH_LEN=", 1)[1].split()[0])
    assert path_len > 0, f"PATH was empty inside VM: {result['output']!r}"


@requires_gondolin
def test_cwd_persists_across_calls(gondolin_env):
    """BaseEnvironment's cwd-marker machinery threads through gondolin."""
    # First call: cd somewhere known and writable.
    r1 = gondolin_env.execute("cd /tmp && pwd")
    assert r1["returncode"] == 0
    assert "/tmp" in r1["output"]
    # Second call should still be in /tmp.
    r2 = gondolin_env.execute("pwd")
    assert r2["returncode"] == 0
    assert "/tmp" in r2["output"]


@requires_gondolin
def test_nonzero_exit_codes_propagate(gondolin_env):
    """A command that fails inside the VM surfaces a non-zero returncode
    in the execute() result."""
    result = gondolin_env.execute("exit 42")
    assert result["returncode"] == 42


# ----------------------------------------------------------------------
# Workspace bind-mount: host <-> guest filesystem sharing.
# These tests prove the vfs.mounts + RealFSProvider wiring actually works
# end-to-end inside a real VM.
# ----------------------------------------------------------------------

@pytest.fixture
def gondolin_env_workspace(tmp_path):
    """Real Gondolin VM with the default /workspace bind mount."""
    from tools.environments.gondolin import GondolinEnvironment
    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sandbox"),
        cwd="/workspace",
        timeout=60,
        init_timeout=120.0,
        stub_vm=False,
    )
    try:
        yield env
    finally:
        env.cleanup()


@requires_gondolin
def test_host_write_visible_in_guest(gondolin_env_workspace):
    """A file written on the host under workspace_dir/ appears at the
    corresponding path inside the guest /workspace/.

    Note: writes go to ``workspace_dir`` (the bind-mounted subdir), NOT
    ``sandbox_dir`` itself — the root holds infra like ``gondolin.sock``
    that the guest must not see."""
    env = gondolin_env_workspace
    # Host side: drop a file directly in the workspace subdir.
    (Path(env.workspace_dir) / "from-host.txt").write_text("hello from host\n")
    # Guest side: read it back via the VM.
    result = env.execute("cat /workspace/from-host.txt")
    assert result["returncode"] == 0, f"cat failed: {result}"
    assert "hello from host" in result["output"]


@requires_gondolin
def test_guest_write_visible_on_host(gondolin_env_workspace):
    """A file written inside the guest under /workspace/ appears on the
    host under workspace_dir/."""
    env = gondolin_env_workspace
    result = env.execute(
        "printf 'hello from guest\\n' > /workspace/from-guest.txt"
    )
    assert result["returncode"] == 0, f"write failed: {result}"
    # Host side: file is present with the expected content.
    host_file = Path(env.workspace_dir) / "from-guest.txt"
    assert host_file.exists(), f"file not on host: {host_file}"
    assert host_file.read_text() == "hello from guest\n"


@requires_gondolin
def test_workspace_mount_is_writable(gondolin_env_workspace):
    """Sanity check that the bind mount is read-write, not read-only."""
    env = gondolin_env_workspace
    result = env.execute(
        "touch /workspace/.write-probe && rm /workspace/.write-probe && echo ok"
    )
    assert result["returncode"] == 0, f"write probe failed: {result}"
    assert "ok" in result["output"]


@requires_gondolin
def test_daemon_socket_not_visible_in_guest_workspace(gondolin_env_workspace):
    """The daemon's gondolin.sock lives at sandbox_dir root, NOT inside the
    bind-mounted workspace_dir. Listing /workspace inside the guest must
    NOT see gondolin.sock — exposing it would let a malicious in-VM
    process speak to the host-side daemon directly.

    Regression for the cosmetic+security finding from the first E2E smoke,
    where binding the entire sandbox_dir leaked the socket."""
    env = gondolin_env_workspace
    result = env.execute("ls -A /workspace")
    assert result["returncode"] == 0, f"ls failed: {result}"
    assert "gondolin.sock" not in result["output"], (
        f"daemon socket leaked into guest /workspace listing:\n{result['output']}"
    )
