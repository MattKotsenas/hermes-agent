"""daemon stderr pipe must be drained, not left to fill.

GondolinEnvironment spawns the Node daemon with stderr=subprocess.PIPE.
Until the reaper landed, no code in the Python layer ever read that pipe — so the
kernel buffer (default ~64 KB on Linux) would fill, then the daemon's
next write(2) on stderr would block in-kernel indefinitely. Any noisy
diagnostic (V8 deprecation warning, Node debug log, OOM trace, the
log() helper inside the daemon itself) is enough to fill 64 KB on a
long session, after which the daemon's event loop wedges.

These tests pin the contract: the reaper must drain stderr regardless
of how fast the daemon writes, and the bytes must be surfaced to the
Hermes logger so a user has a chance to see what went wrong.
"""

from __future__ import annotations

import logging
import os
import threading


from tools.environments import gondolin as gondolin_mod


def test_drain_daemon_stderr_helper_exists():
    """The reaper helper is a callable on the gondolin module so we can
    test it in isolation from the full env spinup. Pre-fix, it didn't
    exist at all (raw stderr=PIPE with no reader)."""
    assert hasattr(gondolin_mod, "_start_daemon_stderr_reaper"), (
        "Missing helper: gondolin._start_daemon_stderr_reaper(proc, log) "
        "should exist to drain the daemon's stderr pipe. Without it, the "
        "daemon will wedge after ~64 KB of stderr output."
    )


def test_daemon_stderr_does_not_block_after_64kb():
    """Push >64KB through a fake daemon stderr pipe; the reaper must keep
    draining so the writer never blocks. Without the reaper the writer
    blocks at the kernel buffer cap.

    Failure shape: the writer-thread join below times out (the writes are
    deadlocked at the kernel pipe buffer cap), which surfaces as
    `writer.is_alive()` after a 5s join. We don't time the writes — that
    flakes on loaded CI. We just assert "the writer thread can finish."
    """
    # Set up a real OS pipe to play the role of subprocess.PIPE stderr.
    read_fd, write_fd = os.pipe()
    try:
        # Wrap in a fake Popen-like with the .stderr attribute the reaper expects.
        class _FakeProc:
            def __init__(self, fd):
                self.stderr = os.fdopen(fd, "rb", buffering=0)
                self.returncode = None
            def poll(self):
                return self.returncode

        fake = _FakeProc(read_fd)
        reaper = gondolin_mod._start_daemon_stderr_reaper(
            fake, log=logging.getLogger("test.stderr_reaper")
        )
        assert reaper is not None, "reaper should return the thread handle"

        # Write 200 KB (3x the typical 64 KB pipe buffer) from a worker
        # thread. If the reaper is broken, the worker blocks at the 64th
        # KB and never finishes; if it's working, the worker returns
        # promptly. We assert on thread liveness, not on elapsed time.
        payload = b"X" * 1024  # 1 KB chunks
        chunks = 200

        def _writer():
            for _ in range(chunks):
                os.write(write_fd, payload)

        writer = threading.Thread(target=_writer, name="stderr-writer")
        writer.start()
        writer.join(timeout=5.0)
        assert not writer.is_alive(), (
            "writer thread blocked writing 200 KB of fake daemon stderr — "
            "the reaper is not draining the pipe. The real daemon would "
            "wedge after the first 64 KB in production."
        )

        # Signal EOF so the reaper exits cleanly.
        os.close(write_fd)
        write_fd = -1
        reaper.join(timeout=2.0)
        assert not reaper.is_alive(), "reaper should exit when stderr pipe is closed"
    finally:
        if write_fd >= 0:
            try:
                os.close(write_fd)
            except OSError:
                pass


def test_daemon_stderr_bytes_are_surfaced_to_logger(caplog):
    """Bytes the daemon writes to stderr should be available to the
    operator via the Hermes logger. Going dark on diagnostics (e.g.
    redirecting to DEVNULL) is worse than the 64 KB stall — any future
    'vm boot failed' or 'krun: unknown option' would silently
    disappear.
    """
    read_fd, write_fd = os.pipe()
    try:
        class _FakeProc:
            def __init__(self, fd):
                self.stderr = os.fdopen(fd, "rb", buffering=0)
                self.returncode = None
            def poll(self):
                return self.returncode

        fake = _FakeProc(read_fd)
        logger = logging.getLogger("test.stderr_reaper.surface")
        logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger="test.stderr_reaper.surface"):
            reaper = gondolin_mod._start_daemon_stderr_reaper(fake, log=logger)
            assert reaper is not None
            os.write(write_fd, b"krun: unknown option --foo\n")
            os.write(write_fd, b"vm boot failed: ENOSPC\n")
            os.close(write_fd)
            write_fd = -1
            reaper.join(timeout=2.0)

        # At least one of the messages should appear in the captured log.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "krun: unknown option --foo" in joined, (
            f"daemon stderr should be surfaced to the Hermes logger; "
            f"got:\n{joined}"
        )
        assert "vm boot failed: ENOSPC" in joined, (
            f"daemon stderr should be surfaced; got:\n{joined}"
        )
    finally:
        if write_fd >= 0:
            try:
                os.close(write_fd)
            except OSError:
                pass
