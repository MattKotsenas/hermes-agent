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


def _python_image_present() -> bool:
    """Module-import-safe probe for a python-capable gondolin image.

    Used by ``test_execute_code_round_trip`` to decide whether to run
    (image built) or skip (image not built — CI without the build step
    won't fail the test). Swallows all exceptions because pytest runs
    this at collection time and a broken probe must not fail the whole
    test run.

    Looks for the default-image tag the gondolin backend would resolve
    to via ``ensure_built``. On a host that's run ``hermes setup`` or
    ``hermes gondolin prebuild``, this is in the local image store.
    """
    try:
        from hermes_cli.gondolin_image import is_image_built, oci_image_tag
        from tools.terminal_tool import DEFAULT_GONDOLIN_IMAGE
        return is_image_built(oci_image_tag(DEFAULT_GONDOLIN_IMAGE))
    except Exception:  # noqa: BLE001
        return False


UNIVERSAL_DEVCONTAINER_IMAGE = "mcr.microsoft.com/devcontainers/universal:6"


def _universal_devcontainer_image_present() -> bool:
    """Probe for the universal:6 devcontainer image.

    Separate from ``_python_image_present`` because universal:6 is a
    distinct image (huge, ships ruby/rvm/conda/etc.) that historically
    surfaced two snapshot-capture regressions:

    1. ``nvs.sh`` uses a bashism (``&>``) sourced by dash, leaking
       ``/opt/conda/bin/xz`` onto every command's stdout
       (fixed in 1aec96cef: argv-form vm.exec, no /bin/sh -lc wrap).
    2. ``rvm.sh`` uses process substitution (``<(cmd)``), which fails
       because gondolin guests don't ship /dev/fd symlinks
       (fixed in fd2896a38: daemon-side setupGuestDevSymlinks).

    The regression test below depends on this image being built.
    """
    try:
        from hermes_cli.gondolin_image import is_image_built, oci_image_tag
        return is_image_built(oci_image_tag(UNIVERSAL_DEVCONTAINER_IMAGE))
    except Exception:  # noqa: BLE001
        return False


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


@requires_gondolin
def test_dev_fd_symlinks_exist(gondolin_env):
    """Regression: upstream gondolin guests don't ship the standard
    /dev/{fd,stdin,stdout,stderr} -> /proc/self/fd[/N] symlinks that
    runc, crun, systemd, and OpenRC all set up at boot. Without them,
    bash process substitution <(cmd) and anything that reads
    /dev/stdin as a path fails -- most visibly /etc/profile.d/rvm.sh
    on mcr.microsoft.com/devcontainers/universal:6, which prints
    'cat: /dev/fd/63: No such file or directory' on every login shell.

    The daemon installs the symlinks at init via setupGuestDevSymlinks().
    Drop the workaround -- and this test -- once
    https://github.com/earendil-works/gondolin/issues/118 ships upstream.
    """
    result = gondolin_env.execute(
        'for p in /dev/fd /dev/stdin /dev/stdout /dev/stderr; do '
        '  echo "$p -> $(readlink "$p" 2>/dev/null || echo NOT_A_SYMLINK)"; '
        'done'
    )
    output = result["output"]
    assert result["returncode"] == 0, f"non-zero exit: {result}"
    assert "/dev/fd -> /proc/self/fd" in output, output
    assert "/dev/stdin -> /proc/self/fd/0" in output, output
    assert "/dev/stdout -> /proc/self/fd/1" in output, output
    assert "/dev/stderr -> /proc/self/fd/2" in output, output


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


@pytest.fixture
def gondolin_env_with_python(tmp_path):
    """Real Gondolin VM with a python-capable image (default OCI image).

    Separate from ``gondolin_env_workspace`` because gondolin's stock
    alpine-base image is faster to boot but has no python3, and most
    integration tests don't need an interpreter. This fixture is for
    tests that exercise the in-VM Python runtime (``execute_code``,
    ``hermes_tools``-shipped scripts, etc.) and is automatically skipped
    by the test's own ``skipif(not _python_image_present())``.
    """
    from hermes_cli.gondolin_image import oci_image_tag
    from tools.environments.gondolin import GondolinEnvironment
    from tools.terminal_tool import DEFAULT_GONDOLIN_IMAGE

    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sandbox"),
        cwd="/workspace",
        timeout=60,
        init_timeout=180.0,  # bigger image, longer cold boot budget
        stub_vm=False,
        config={"image": oci_image_tag(DEFAULT_GONDOLIN_IMAGE)},
    )
    try:
        yield env
    finally:
        env.cleanup()


@pytest.fixture
def gondolin_env_universal_devcontainer(tmp_path):
    """Real Gondolin VM on the universal:6 devcontainer image.

    Used by the snapshot-capture regression below. universal:6 is the
    image that surfaced both prior steady-state-leak bugs (xz from nvs.sh,
    /dev/fd from rvm.sh) so we lock down clean behavior here.
    """
    from hermes_cli.gondolin_image import oci_image_tag
    from tools.environments.gondolin import GondolinEnvironment

    env = GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sandbox"),
        cwd="/workspace",
        timeout=60,
        init_timeout=240.0,  # universal:6 is ~5GB; allow extra cold-boot budget
        stub_vm=False,
        config={"image": oci_image_tag(UNIVERSAL_DEVCONTAINER_IMAGE)},
    )
    try:
        yield env
    finally:
        env.cleanup()


@requires_gondolin
@pytest.mark.skipif(
    not _universal_devcontainer_image_present(),
    reason="universal:6 image not built locally — run `hermes gondolin prebuild`",
)
def test_snapshot_capture_is_clean_on_universal_devcontainer(
    gondolin_env_universal_devcontainer,
):
    """Cross-cutting leak detector for the BaseEnvironment snapshot
    machinery on a real-world devcontainer image with profile.d scripts.

    universal:6 has historically broken two ways in steady-state output:
      1. ``/etc/profile.d/nvs.sh`` uses ``&>`` (bashism) → dash backgrounds
         ``command -v xz`` → ``/opt/conda/bin/xz`` leaks onto every command
         (fixed in 1aec96cef: argv-form vm.exec bypasses the SDK shell wrap).
      2. ``/etc/profile.d/rvm.sh`` uses process substitution ``<(cmd)`` →
         fails because gondolin guests ship no /dev/fd symlink
         (fixed in fd2896a38: daemon installs the symlinks at VM init).

    Both bugs were invisible to existing assertions because they only
    surfaced at session-snapshot time (login=True), not under the
    steady-state stub fixtures. This test exercises the real path on
    the canonical real-world image.

    Asserts:
      - ``init_session`` succeeds and sets ``_snapshot_ready = True``
        (no silent fallback to login-every-call).
      - A trivial ``echo`` round-trips with EXACT output (no extra bytes
        leaked from profile scripts).
      - No known leak patterns (xz path, /dev/fd errors) appear in output.
      - Process substitution works (sanity check for the /dev/fd fix).
    """
    env = gondolin_env_universal_devcontainer
    env.init_session()
    assert env._snapshot_ready, (
        "snapshot capture must succeed on universal:6 — if this is False, "
        "every execute() falls back to bash -l and pays the full profile "
        "cost on every call"
    )

    # Exact-match echo: any prefix/suffix from a profile script will trip this.
    r = env.execute("echo HERMES_PROBE_OK")
    assert r["returncode"] == 0, r
    assert r["output"] == "HERMES_PROBE_OK\n", (
        f"steady-state output has unexpected bytes: {r['output']!r}"
    )

    # Named-pattern leak detector across a second command.
    r2 = env.execute("printf clean")
    assert r2["output"] == "clean", repr(r2["output"])
    for needle in ("/opt/conda/bin/xz", "/dev/fd/", "No such file or directory"):
        assert needle not in r2["output"], (
            f"known-leak pattern {needle!r} appeared in output: {r2['output']!r}"
        )

    # Process substitution sanity (locks in the /dev/fd fix at the bash level).
    r3 = env.execute("cat <(echo procsub)")
    assert r3["returncode"] == 0
    assert r3["output"] == "procsub\n", repr(r3["output"])


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


@requires_gondolin
def test_stream_preserves_stderr_separation(gondolin_env):
    """``gondolin_rpc_call --stream`` keeps stdout and stderr tagged
    separately on the wire — a regression caught only with a real VM.

    The streaming exec path inside the daemon iterates Gondolin's
    ``ExecProcess.output()`` and yields ``{kind, data}`` chunks. Earlier
    revisions iterated the bare async iterable instead, which yielded
    ``string`` and silently flattened stderr into stdout (no ``kind``
    field). Unit tests with a stub VM can't catch this — only a real
    guest produces tagged chunks the daemon must thread through.
    """
    import subprocess
    import sys

    env = gondolin_env
    # Use the live env's daemon socket — no second VM, no extra boot.
    repo_root = Path(__file__).resolve().parent.parent.parent
    rpc_call = repo_root / "tools" / "environments" / "gondolin_rpc_call.py"
    # Print distinguishable markers to each stream so a flatten-into-stdout
    # regression appears as both markers landing on stdout.
    cmd = "printf 'OUT_MARKER\\n'; printf 'ERR_MARKER\\n' >&2; exit 7"
    proc = subprocess.run(
        [sys.executable, str(rpc_call), env.sock_path, cmd, "--stream"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 7, (
        f"exit code not propagated: {proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "OUT_MARKER" in proc.stdout, (
        f"stdout marker missing from stdout:\nstdout={proc.stdout!r}\n"
        f"stderr={proc.stderr!r}"
    )
    assert "ERR_MARKER" in proc.stderr, (
        f"stderr marker landed on the wrong stream — regression to "
        f"untagged-iterator bug. stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "OUT_MARKER" not in proc.stderr, (
        f"stdout marker leaked into stderr: stderr={proc.stderr!r}"
    )
    assert "ERR_MARKER" not in proc.stdout, (
        f"stderr marker leaked into stdout — likely the flatten regression: "
        f"stdout={proc.stdout!r}"
    )


@requires_gondolin
@pytest.mark.skipif(
    not _python_image_present(),
    reason=(
        "execute_code requires python3 in the guest. Run `hermes setup` "
        "or `hermes gondolin prebuild` to materialize the default OCI "
        "image. Without it the integration test skips."
    ),
)
def test_execute_code_round_trip(gondolin_env_with_python):
    """End-to-end ``execute_code`` against a real Gondolin VM.

    The flow:
      1. ``execute_code`` calls ``_env_temp_dir(env)`` to decide where to
         ship the script.
      2. ``_ship_file_to_remote`` pipes the script's bytes through the
         env's bash channel — so the path must exist (or be creatable)
         inside the guest.
      3. The script runs in-guest; stdout comes back through
         ``env.execute(...)``.

    Without a guest-side ``get_temp_dir()`` override on the environment,
    ``_env_temp_dir`` falls back to ``tempfile.gettempdir()`` on the HOST,
    which returns ``/tmp`` — a path that exists in the guest by accident
    on Alpine, but the host file shipping uses base64-pipe-into-cat which
    runs inside the guest, so the "wrong" path is actually still inside
    the guest. This means the existing flow may incidentally work; the
    test makes that explicit so any regression is caught.

    The test is intentionally minimal: it runs a script that imports the
    injected ``hermes_tools`` helper module, calls ``terminal('uname -a')``,
    and prints the result. Failure modes we want to catch:
      - ``hermes_tools`` not shipped (FileNotFoundError on import)
      - script can't reach ``/tmp`` (ship failure)
      - RPC dir doesn't exist (host-side path leaked to guest)
    """
    from tools.code_execution_tool import execute_code

    env = gondolin_env_with_python
    code = (
        "import json\n"
        "from hermes_tools import terminal\n"
        "result = terminal('uname -a', timeout=30)\n"
        "print('UNAME:', result['output'].strip())\n"
        "print('EXIT:', result['exit_code'])\n"
    )
    # execute_code dispatches via the global tool registry; we don't go
    # through that. Instead we go through the _execute_remote path which
    # accepts an explicit env. That's the seam under test.
    from tools.code_execution_tool import _execute_remote

    # Patch the global env-resolution helper so _execute_remote uses our
    # already-booted env instead of constructing a new one from
    # TERMINAL_ENV (which would spin up a SECOND VM).
    import tools.code_execution_tool as _cet

    # _get_or_create_env returns (env, env_type) — match that contract.
    original = _cet._get_or_create_env
    _cet._get_or_create_env = lambda *a, **kw: (env, "gondolin")
    try:
        result_str = _execute_remote(code, task_id="kvm-exec-code-probe", enabled_tools=[])
    finally:
        _cet._get_or_create_env = original

    # _execute_remote returns a formatted string. We don't know the exact
    # framing (varies by exit code, may include "Output:" / "Error:" prefixes)
    # so just assert the expected payload is in there.
    assert "UNAME:" in result_str, (
        f"missing UNAME marker in output:\n{result_str}"
    )
    assert "Linux" in result_str, f"Linux not in output:\n{result_str}"
    # If the script crashed at import or exec time, an error block would
    # be present. Fail loudly if that's what we see.
    assert "Traceback" not in result_str, (
        f"script raised an exception inside the VM:\n{result_str}"
    )


# ----------------------------------------------------------------------
# Overlay isolation across env lifecycles (matches docker behaviour)
# ----------------------------------------------------------------------

@requires_gondolin
def test_overlay_writes_do_not_leak_between_env_lifecycles(tmp_path):
    """A fresh GondolinEnvironment for the same sandbox_dir + same
    overlay extra_mount must NOT see writes made by the previous env's
    lifecycle into that mount.

    Matches the docker backend's per-init isolation: docker.py:508 stamps
    a fresh ``hermes-<uuid>`` container name on every ``_init``, so the
    next env after teardown gets a brand-new filesystem. Gondolin's
    sandbox_dir is deterministic from task_id, so without this guarantee
    a stale on-disk overlay scratch leaks session N's writes into
    session N+1.

    Asserts the BEHAVIOURAL invariant via ``execute()`` and does not
    inspect any host-side dir or mention fuse-overlayfs. A future
    migration to a custom upstream VFSProvider (see
    @earendil-works/gondolin's ``vfs/provider``) satisfies the same
    contract trivially — per-VM scratch is ephemeral by construction —
    and this test passes for free.
    """
    from tools.environments.gondolin import GondolinEnvironment

    # Lower layer: a host-side "vault" the agent should be able to read
    # but not pollute. Each env mounts this read-mostly under the overlay.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "host_file.txt").write_text("from host\n")

    sandbox_dir = tmp_path / "sandbox"

    def _mount_config():
        # Build a fresh config dict per lifecycle — overlay setup mutates
        # ``host_path`` (rewriting it to the merged dir) and pops the
        # ``overlay`` key, so we cannot reuse the same dict across envs.
        return {
            "extra_mounts": [{
                "host_path": str(vault),
                "guest_path": "/root/vault",
                "overlay": True,
                # Must be set explicitly: the daemon defaults
                # ``readonly`` to True (daemon.mjs ~line 216), which
                # would wrap the overlay in ReadonlyProvider and reject
                # the guest writes that make this test meaningful.
                "readonly": False,
            }],
            # Skip skill / credential projection — the test must not
            # depend on the host's hermes install layout.
            "project_skills": False,
            "project_credentials": False,
        }

    # ── Lifecycle 1: write a "secret" into the overlay, then tear down.
    env1 = GondolinEnvironment(
        sandbox_dir=str(sandbox_dir),
        cwd="/root",
        timeout=60,
        init_timeout=120.0,
        stub_vm=False,
        config=_mount_config(),
    )
    try:
        # Precondition: the host file IS visible, the secret file is NOT.
        # Asserting both proves the test inputs are wired correctly.
        r = env1.execute("cat /root/vault/host_file.txt")
        assert r["returncode"] == 0, f"precondition: cat host_file failed: {r}"
        assert "from host" in r["output"]
        r = env1.execute("test ! -e /root/vault/secret.txt && echo absent")
        assert "absent" in r["output"], (
            f"precondition: secret.txt must not pre-exist: {r}"
        )

        # Write a sandbox-only file into the overlay and confirm it lands.
        r = env1.execute(
            "printf 'session1 secret\\n' > /root/vault/secret.txt "
            "&& cat /root/vault/secret.txt"
        )
        assert r["returncode"] == 0, f"setup: write to overlay failed: {r}"
        assert "session1 secret" in r["output"]

        # And the host vault MUST NOT see the write (overlay correctness
        # — orthogonal to the leak we're testing, but a useful guard).
        assert not (vault / "secret.txt").exists(), (
            "overlay leaked guest write back to host vault"
        )
    finally:
        env1.cleanup()

    # ── Lifecycle 2: same sandbox_dir, same mount, a brand-new env.
    env2 = GondolinEnvironment(
        sandbox_dir=str(sandbox_dir),
        cwd="/root",
        timeout=60,
        init_timeout=120.0,
        stub_vm=False,
        config=_mount_config(),
    )
    try:
        # Lower is unchanged across lifecycles, so the host file is still
        # visible. (If this fails, the mount itself is broken — the next
        # assertion would be a false positive.)
        r = env2.execute("cat /root/vault/host_file.txt")
        assert r["returncode"] == 0, f"lower no longer visible: {r}"
        assert "from host" in r["output"]

        # THE invariant: session 1's write must NOT be visible to session 2.
        # Implementation-agnostic — true for fuse-overlayfs with per-init
        # scratch teardown, true for a per-VM ephemeral VFSProvider, false
        # for any backend that carries scratch forward.
        r = env2.execute("cat /root/vault/secret.txt 2>&1; echo rc=$?")
        assert "session1 secret" not in r["output"], (
            f"OVERLAY LEAK: session 2 read session 1's write: {r}"
        )
        assert "rc=1" in r["output"] or "rc=2" in r["output"], (
            f"expected ENOENT (rc=1 from cat) but got: {r}"
        )
    finally:
        env2.cleanup()



# ------------------------------------------------------------
# stdin forwarding: ShellFileOperations.write_file silently produced
# 0-byte files because the daemon dropped params.stdin. This test
# exercises the full write_file -> _exec -> _run_bash -> wrapper
# -> daemon -> vm.exec stack with a REAL Gondolin VM, against the
# default workspace bind mount.
#
# Pre-fix observation: WriteResult(bytes_written=0, error=None) plus a
# 0-byte file in the guest. This test fails loudly in that state.
# ------------------------------------------------------------

@requires_gondolin
def test_shell_file_operations_write_file_actually_writes_bytes(gondolin_env_workspace):
    """write_file must produce a file whose byte count matches the input
    on BOTH the in-guest filesystem and the host-side bind mount.

    Regression test for the daemon dropping params.stdin on the floor.
    Exercises text content (with newlines) and arbitrary binary bytes
    (PNG header + null + high-bit) to catch UTF-8 reinterpretation bugs
    in the wire-format path.
    """
    from tools.file_operations import ShellFileOperations

    env = gondolin_env_workspace
    fops = ShellFileOperations(env)

    text = "hello-from-write-file\n" * 6
    result = fops.write_file("/workspace/probe.txt", text)
    assert result.error is None, f"write_file errored: {result.error}"
    assert result.bytes_written == len(text), (
        f"bytes_written mismatch: want {len(text)}, got {result.bytes_written}. "
        f"Pre-fix this returned 0 because the daemon dropped params.stdin."
    )

    guest = env.execute("wc -c < /workspace/probe.txt")
    assert guest["returncode"] == 0, f"in-guest wc failed: {guest}"
    assert guest["output"].strip() == str(len(text)), (
        f"in-guest byte count mismatch: want {len(text)}, got {guest['output'].strip()}"
    )

    host = Path(env.workspace_dir) / "probe.txt"
    assert host.exists(), f"host-side file missing: {host}"
    assert host.stat().st_size == len(text), (
        f"host-side byte count mismatch: want {len(text)}, got {host.stat().st_size}"
    )
    assert host.read_text() == text, "host-side content mismatch"

    blob = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x01, 0xff, 0xfe, 0xfd]) * 100
    result2 = fops.write_file("/workspace/probe.bin", blob)
    assert result2.error is None, f"binary write_file errored: {result2.error}"
    assert result2.bytes_written == len(blob), (
        f"binary bytes_written mismatch: want {len(blob)}, got {result2.bytes_written}"
    )
    host_bin = Path(env.workspace_dir) / "probe.bin"
    assert host_bin.exists() and host_bin.stat().st_size == len(blob)
    assert host_bin.read_bytes() == blob, (
        "binary content drifted in transit (likely UTF-8 reinterpretation "
        "somewhere along the wrapper / msgpack / daemon path)"
    )
