"""Tests for init/cleanup error paths in GondolinEnvironment.

KeyboardInterrupt during init leaks the daemon subprocess.
GondolinEnvironment.__init__ wraps _init_after_slot in `except
BaseException` (to release the slot on Ctrl-C), but _init_after_slot
itself only catches `except Exception` — KeyboardInterrupt slips
past the inner handler (which would have killed the daemon) and
reaches the outer one (which only releases the slot, NOT the
daemon). Result: a 256–512 MB Node+VM process is orphaned every
time a user Ctrl-Cs during boot.

A malformed shutdown response from the daemon raises a msgpack
exception that cleanup()'s narrow `except (OSError, RuntimeError)`
doesn't catch. The exception escapes cleanup, skipping
_terminate_daemon, workspace rmtree, and slot release — silently
exhausting the cap over time as malformed shutdowns accumulate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.environments import gondolin as gondolin_mod


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NODE_DAEMON = REPO_ROOT / "tools" / "environments" / "gondolin_host" / "src" / "daemon.mjs"
NODE_AVAILABLE = shutil.which("node") is not None and NODE_DAEMON.exists()
requires_node = pytest.mark.skipif(not NODE_AVAILABLE, reason="node or daemon.mjs missing")


# ----------------------------------------------------------------------
# KeyboardInterrupt during init must terminate the daemon
# ----------------------------------------------------------------------

@requires_node
def test_keyboardinterrupt_during_init_terminates_daemon(monkeypatch, tmp_path):
    """A Ctrl-C arriving while __init__ is blocked in _wait_for_socket
    (or _rpc_call(init)) must NOT leave the daemon process orphaned.

    Pre-fix: __init__'s outer `except BaseException` released the slot
    but did not call _terminate_daemon — the inner `except Exception`
    in _init_after_slot doesn't catch KeyboardInterrupt, so the
    daemon-cleanup branch was skipped.
    """
    spawned = {}

    real_popen = subprocess.Popen

    def capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned["proc"] = proc
        return proc

    monkeypatch.setattr(subprocess, "Popen", capturing_popen)

    # Patch _wait_for_socket so it raises KeyboardInterrupt the moment
    # __init__ blocks on it — simulating a Ctrl-C during boot, but
    # deterministically rather than racing a real signal.
    def kbd_interrupt(*_a, **_kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(gondolin_mod, "_wait_for_socket", kbd_interrupt)

    # Hold the (partially-failed) env reference so __del__ can't run
    # and mask the bug. In production the env is stored in agent state
    # — see cli.py / batch_runner — so __del__'s rescue path doesn't
    # apply.
    held_envs = []

    real_init = gondolin_mod.GondolinEnvironment.__init__

    def init_holding(self_, *a, **kw):
        held_envs.append(self_)  # keep alive even if __init__ raises
        return real_init(self_, *a, **kw)

    monkeypatch.setattr(gondolin_mod.GondolinEnvironment, "__init__", init_holding)

    with pytest.raises(KeyboardInterrupt):
        gondolin_mod.GondolinEnvironment(
            sandbox_dir=str(tmp_path / "sandbox"),
            cwd="/root",
            timeout=10,
            init_timeout=5,
            stub_vm=True,
        )

    # Check liveness IMMEDIATELY — before any finalizer or GC can reap.
    # In production the env object lives in agent state for minutes;
    # the daemon must be terminated as part of __init__'s error-handler
    # contract, not as a side effect of later object death.
    proc = spawned.get("proc")
    assert proc is not None, "test bug: Popen was never invoked"
    # The fix should have called _terminate_daemon() which issues
    # SIGTERM + wait(5). Block on wait() rather than polling so the test
    # doesn't go flaky on a loaded CI box where signal handling is slow.
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        # Daemon still alive — clean up so we don't leak across the
        # rest of the test run, then fail with a useful message.
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass
        pytest.fail(
            "daemon orphaned after KeyboardInterrupt during init. "
            "GondolinEnvironment.__init__'s outer `except BaseException` "
            "must call self._terminate_daemon() so a Ctrl-C during boot "
            "doesn't leak the 256-512 MB Node+VM allocation. Daemon "
            "process was still alive 5 seconds after KeyboardInterrupt "
            "propagated out of __init__."
        )


# ----------------------------------------------------------------------
# malformed shutdown response must not leak workspace/slot
# ----------------------------------------------------------------------

@requires_node
def test_malformed_shutdown_response_does_not_leak_slot(monkeypatch, tmp_path):
    """If _rpc_call raises something cleanup() doesn't anticipate
    (msgpack format errors, value errors, anything outside
    OSError/RuntimeError), the original code let the exception escape
    cleanup() — skipping _terminate_daemon, workspace rmtree, and slot
    release. Over many bad shutdowns the slot counter would exhaust
    and new envs would be refused with 'at max_concurrent_vms cap'.
    """
    env = gondolin_mod.GondolinEnvironment(
        sandbox_dir=str(tmp_path / "sandbox"),
        cwd="/root",
        timeout=10,
        init_timeout=10,
        stub_vm=True,
    )

    # Sanity: slot was acquired during __init__.
    assert env._slot is not None, "test setup: slot should be live after __init__"
    workspace = env.workspace_dir
    assert workspace.exists(), "test setup: workspace dir should exist"

    # Inject a misbehaving _rpc_call: simulate a daemon that writes a
    # truncated msgpack frame on shutdown. The real msgpack library
    # would raise FormatError / ExtraData — not OSError, not
    # RuntimeError. Use a generic ValueError as a stand-in for the
    # exception class hierarchy; the point is "anything outside the
    # narrow tuple cleanup currently catches."
    def malformed_response(*_a, **_kw):
        raise ValueError("truncated msgpack frame from daemon")

    monkeypatch.setattr(gondolin_mod, "_rpc_call", malformed_response)

    # cleanup() must NOT propagate the malformed-response error. It
    # may log it, but the daemon-terminate / workspace-rmtree / slot-
    # release branches must still run.
    env.cleanup()  # should not raise

    # Slot released, workspace gone, daemon process dead.
    assert env._slot is None, (
        "cleanup() let a malformed-shutdown exception escape, "
        "skipping slot release. Slot leak compounds across sessions."
    )
    assert not workspace.exists(), (
        "cleanup() let an exception escape before workspace "
        "rmtree. Disk leak across sessions."
    )
    # Daemon process should be reaped within a couple of seconds.
    proc = getattr(env, "_daemon_proc", None)
    # _terminate_daemon nulls _daemon_proc on success, so checking
    # `is None` is the contract.
    assert proc is None, (
        "cleanup() did not call _terminate_daemon — "
        "self._daemon_proc still set to a live process."
    )
