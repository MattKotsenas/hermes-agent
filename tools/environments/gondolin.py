"""GondolinEnvironment — terminal backend that runs commands inside a
per-session Gondolin microVM via a long-lived Node daemon.

Architecture (see docs/design/gondolin-terminal-backend.md):

- One Node daemon per Hermes session, spawned at backend construction.
  The daemon owns the Gondolin VM and listens on an AF_UNIX socket
  inside the session's sandbox directory.
- Every ``_run_bash`` call spawns the Python wrapper
  (``tools.environments.gondolin_rpc_call``) as a subprocess. The
  wrapper opens a short-lived connection to the socket, sends one
  ``exec`` RPC, writes the daemon's stdout/stderr back, exits with the
  in-VM exit code. BaseEnvironment's existing Popen-shape machinery
  consumes that subprocess unchanged.
- ``cleanup()`` sends a ``shutdown`` RPC, waits briefly, and SIGKILLs
  the daemon if it doesn't exit. The daemon tears down its VM on
  receipt of SIGTERM as a backstop.

The environment is stateful about the daemon process and socket path
but stateless about individual exec calls — a wrapper crash, daemon
crash, or VM hang surfaces as a clean tool error on the next call.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.gondolin_secret_refresh import SecretRefresher

logger = logging.getLogger(__name__)


# Run once at import: clean up overlay mounts leaked by a prior hard
# crash so the next sandbox's rmtree(sandbox_dir) doesn't fail with EBUSY.
try:
    _SWEEP_DONE
except NameError:
    _SWEEP_DONE = False


def _overlay_safe_name(guest_path: str) -> str:
    """Filesystem-safe per-mount directory name under sandbox/overlays/.

    Sanitizes the guest path and appends a short hash so two mounts
    with similar paths can't collide.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", guest_path).strip("_") or "mount"
    h = hashlib.sha1(guest_path.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}_{h}"


def _setup_overlay_mounts(
    extra_mounts: list[dict], sandbox_dir: Path
) -> list[str]:
    """For each extra_mounts entry with ``overlay: True``, spawn
    fuse-overlayfs and rewrite the entry's host_path to point at the
    merged dir. Returns the list of merged paths that must be
    unmounted on cleanup.

    Layout (per mount): ``<sandbox_dir>/overlays/<safe>/{upper,work,merged}``.
    The user's live vault dir becomes the lower layer. Writes from the
    sandbox land in upper/ and never touch the host vault.

    Freshness caveat: fuse-overlayfs treats the lower as point-in-time at
    mount; host-side edits made while the sandbox is running are NOT
    visible inside the sandbox. Each new sandbox sees the current host
    state at spawn. See docs/design/gondolin-terminal-backend.md (the
    "DO NOT MERGE" revisit list) for the open follow-ups.

    Cleanup is the caller's responsibility — if the daemon init fails,
    pair this with ``_teardown_overlay_mounts``.
    """
    if not extra_mounts:
        return []
    if not any(entry.get("overlay") for entry in extra_mounts):
        # Strip stray False/None overlay keys so the daemon never sees them.
        for entry in extra_mounts:
            entry.pop("overlay", None)
        return []

    if shutil.which("fuse-overlayfs") is None:
        raise RuntimeError(
            "gondolin: overlay mount requested but fuse-overlayfs is not "
            "installed. Install it with `sudo apt install fuse-overlayfs` "
            "(WSL/Debian/Ubuntu) or remove `overlay: true` from the mount "
            "config to bind directly."
        )

    overlays_root = sandbox_dir / "overlays"
    overlays_root.mkdir(parents=True, exist_ok=True)
    mounted: list[str] = []
    try:
        for entry in extra_mounts:
            if not entry.pop("overlay", False):
                continue
            if entry.get("readonly"):
                logger.warning(
                    "gondolin: ignoring overlay=true on readonly mount %s "
                    "(overlay only makes sense for read-write mounts)",
                    entry.get("guest_path", "?"),
                )
                continue
            lower = entry["host_path"]
            if not os.path.isdir(lower):
                raise RuntimeError(
                    f"gondolin: overlay lower dir does not exist or is not "
                    f"a directory: {lower}"
                )
            safe = _overlay_safe_name(entry["guest_path"])
            odir = overlays_root / safe
            upper = odir / "upper"
            work = odir / "work"
            merged = odir / "merged"
            for p in (upper, work, merged):
                p.mkdir(parents=True, exist_ok=True)
            # fuse-overlayfs runs as the host user that spawned the daemon
            # (e.g. matt, uid 1000). gondolin's sandboxfs proxies guest I/O
            # to the host as that same uid, so as long as upper/work are
            # owned by the daemon user, writes from the guest "root"
            # (which is actually uid 1000 on the host side) succeed.
            # We deliberately do NOT pass squash_to_root: that flag makes
            # fuse-overlayfs attempt copy-up writes as uid 0, which fails
            # because the daemon lacks CAP_SETUID -- the guest then sees
            # EACCES on every write into the merged tree.
            opts = (
                f"lowerdir={lower},upperdir={upper},workdir={work},"
                f"noatime"
            )
            proc = subprocess.run(
                ["fuse-overlayfs", "-o", opts, str(merged)],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"fuse-overlayfs mount failed for {entry['guest_path']} "
                    f"(rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
                )
            entry["host_path"] = str(merged)
            mounted.append(str(merged))
            logger.debug(
                "gondolin: overlay mounted %s -> %s (lower=%s)",
                entry["guest_path"], merged, lower,
            )
    except Exception:
        _teardown_overlay_mounts(mounted)
        raise
    return mounted


def _teardown_overlay_mounts(merged_paths: list[str]) -> None:
    """Lazy-unmount each fuse-overlayfs mount. Best-effort; never raises.

    Lazy unmount (-z) detaches even if something inside still has open
    file handles — important because the daemon may not have fully
    released the rootfs yet when cleanup runs. Without -z we'd see EBUSY
    and leak the mount forever.
    """
    umount = shutil.which("fusermount3") or shutil.which("fusermount")
    if umount is None:
        return
    for m in merged_paths:
        try:
            subprocess.run(
                [umount, "-u", "-z", m],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("gondolin overlay unmount failed for %s: %s", m, exc)


def _sweep_stale_overlay_mounts() -> None:
    """One-shot cleanup of fuse-overlayfs mounts under our sandboxes dir.

    Called once at module import so a hard-killed prior process (kill
    -9, OOM, WSL restart) doesn't leave us with mounts the next gateway
    can't safely rmtree. Scoped strictly to mountpoints under
    ``~/.hermes/sandboxes/gondolin/*/overlays/*/merged`` so we can't
    accidentally unmount something unrelated.
    """
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return
    sandboxes_root = Path.home() / ".hermes" / "sandboxes" / "gondolin"
    try:
        sandboxes_root_resolved = sandboxes_root.resolve()
    except Exception:  # noqa: BLE001
        return
    if not sandboxes_root_resolved.exists():
        return
    umount = shutil.which("fusermount3") or shutil.which("fusermount")
    if umount is None:
        return
    sandboxes_prefix = str(sandboxes_root_resolved) + os.sep
    for line in mountinfo.splitlines():
        if "fuse-overlayfs" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        mp = parts[4]
        try:
            mp_resolved = Path(mp).resolve(strict=False)
        except Exception:  # noqa: BLE001
            continue
        mp_str = str(mp_resolved)
        if not (mp_str == str(sandboxes_root_resolved) or mp_str.startswith(sandboxes_prefix)):
            continue
        if mp_resolved.name != "merged":
            continue
        logger.warning("gondolin: sweeping stale overlay mount %s", mp)
        try:
            subprocess.run(
                [umount, "-u", "-z", mp],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("gondolin stale-mount sweep failed for %s: %s", mp, exc)


def _start_daemon_stderr_reaper(
    proc: Any, log: logging.Logger
) -> threading.Thread | None:
    """Drain the Node daemon's stderr pipe on a background thread.

    The daemon is spawned with ``stderr=subprocess.PIPE`` so the operator
    can see node-side diagnostics (boot failures, krun errors, V8
    deprecation warnings). But subprocess.PIPE without an active reader
    deadlocks: the kernel buffer (~64 KB on Linux) fills, then the
    daemon's next ``write(2)`` on stderr blocks indefinitely and the
    daemon's event loop wedges.

    This reaper reads the pipe line-by-line and forwards each line to
    the Hermes logger at WARNING level (daemon stderr is, by
    definition, something we want surfaced). The thread exits cleanly
    on EOF (daemon closed stderr — usually because the daemon process
    exited).

    Returns the thread handle so cleanup() can join it, or None when
    ``proc.stderr`` is unavailable (caller chose DEVNULL).
    """
    if proc.stderr is None:
        return None

    def _reap() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                if not raw:
                    break
                # decode with errors='replace' cannot raise on bytes.
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    log.warning("gondolin daemon stderr: %s", line)
        except (OSError, ValueError):
            # ValueError: I/O on closed file (pipe closed under us during
            # cleanup). OSError: pipe broken / EBADF on shutdown race.
            # Either way, the reaper's job is done.
            pass

    t = threading.Thread(
        target=_reap, name="gondolin-daemon-stderr-reaper", daemon=True
    )
    t.start()
    return t


def _ensure_msgpack() -> None:
    """Lazy-install msgpack on demand. Idempotent — fast no-op once installed.

    The gondolin wire format is length-prefixed msgpack between the Python
    wrapper and the Node daemon. msgpack is declared as the
    `terminal.gondolin` extra in `pyproject.toml` and `tools/lazy_deps.py`,
    so users who never select the gondolin backend never pay for it. We
    call this once per process from `_load_msgpack` below — the actual
    import happens lazily on first GondolinEnvironment instantiation,
    NOT at module import time. Importing this module (e.g. for
    introspection, doctor probes, or sandbox-inventory's classifier)
    must not trigger a pip install.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("terminal.gondolin", prompt=False)
    except ImportError:
        # No lazy_deps available (very old / stripped install). Fall
        # through and let the import below produce its own diagnostic.
        pass
    except Exception as e:
        raise ImportError(str(e))


# Module-level msgpack handle, populated by _load_msgpack on first call.
# None until somebody actually constructs a GondolinEnvironment (or calls
# _rpc_call directly), so simply importing this module — e.g. from doctor
# or sandbox-inventory — never triggers the lazy-install machinery.
_msgpack = None


def _load_msgpack():
    """Return the msgpack module, lazy-installing on first call."""
    global _msgpack
    if _msgpack is None:
        _ensure_msgpack()
        import msgpack as _m  # noqa: E402 — must follow _ensure_msgpack()
        _msgpack = _m
    return _msgpack

# Location of the Node daemon source file relative to this module.
_HERE = Path(__file__).resolve().parent
_DAEMON_JS = _HERE / "gondolin_host" / "src" / "daemon.mjs"

# In-process cap on the number of live Gondolin VMs. Each VM costs
# ~256-512 MB of host memory at default settings; a gateway hosting many
# parallel chats could exhaust memory without a cap. Override at runtime
# by re-binding this module attribute (used by tests and by the factory
# when the user sets TERMINAL_GONDOLIN_MAX_CONCURRENT_VMS).
#
# Set to <= 0 to disable the cap entirely. This module-level value is the
# IN-PROCESS cap. When a lock_dir is also configured, we additionally hold
# a flock() on a slot file so the cap is honored host-wide across the CLI,
# subagents, the gateway, and cron jobs. flock() is released on process
# exit by the kernel, so a crashed process doesn't leak slots.
try:
    _max_concurrent_vms = int(os.environ.get("TERMINAL_GONDOLIN_MAX_CONCURRENT_VMS", "0") or "0")
except ValueError:
    _max_concurrent_vms = 0

# Default cross-process lock directory. Resolved lazily so HERMES_HOME /
# tests can override via the config knob. Empty string = in-process only.
_DEFAULT_LOCK_DIR = os.environ.get("TERMINAL_GONDOLIN_LOCK_DIR", "")

# Active VM count + lock. Module-level state because the cap is process-wide.
_vm_count_lock = threading.Lock()
_active_vm_count = 0


@dataclass
class _Slot:
    """A reserved concurrent-VM slot.

    ``fd`` is set when the slot is backed by a cross-process flock; closing
    the fd releases the kernel-side lock. ``in_process`` is True when we
    bumped the module counter — released by decrementing it.
    """
    fd: int | None
    lock_path: str | None
    in_process: bool


def _acquire_vm_slot(*, lock_dir: str | None = None) -> _Slot:
    """Reserve a slot under the concurrent-VM cap. Raises if at limit.

    When ``lock_dir`` is set, also takes a non-blocking flock() on one of
    slot-0.lock..slot-{N-1}.lock so the cap is honored across processes.
    The kernel releases flock() on process exit, so crash-recovery is free.

    Must be paired with _release_vm_slot() on cleanup / init failure.
    """
    global _active_vm_count
    cap = _max_concurrent_vms

    # In-process counter is always the first gate. Cheap, no syscalls.
    with _vm_count_lock:
        if cap > 0 and _active_vm_count >= cap:
            raise RuntimeError(
                f"gondolin: refusing to spawn another VM — at max_concurrent_vms cap ({cap}). "
                f"Either raise TERMINAL_GONDOLIN_MAX_CONCURRENT_VMS / "
                f"terminal.gondolin.max_concurrent_vms, or wait for an existing session "
                f"to clean up. Set the cap to 0 to disable entirely."
            )
        _active_vm_count += 1
    in_process_acquired = True

    # Cross-process flock layer — opt-in via lock_dir. Without it, the cap
    # is in-process only (legacy default; documented).
    fd: int | None = None
    lock_path: str | None = None
    if cap > 0 and lock_dir:
        try:
            os.makedirs(lock_dir, exist_ok=True)
            for slot_idx in range(cap):
                candidate = os.path.join(lock_dir, f"slot-{slot_idx}.lock")
                candidate_fd = os.open(candidate, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(candidate_fd)
                    continue
                # Got the lock. Stamp the file with our PID for forensics —
                # not used for enforcement (flock alone is authoritative).
                try:
                    os.ftruncate(candidate_fd, 0)
                    os.write(candidate_fd, f"{os.getpid()}\n".encode("ascii"))
                except OSError:
                    pass
                fd = candidate_fd
                lock_path = candidate
                break
            if fd is None:
                # All slot files held by other processes. Roll back the
                # in-process counter we already bumped above.
                with _vm_count_lock:
                    if _active_vm_count > 0:
                        _active_vm_count -= 1
                in_process_acquired = False
                raise RuntimeError(
                    f"gondolin: refusing to spawn another VM — host-wide cap "
                    f"({cap}) reached (slot files in {lock_dir} all held by other "
                    f"processes). Either raise max_concurrent_vms, wait for an "
                    f"existing session to release, or set the cap to 0 to disable."
                )
        except BaseException:
            # Any other failure during flock setup: roll back the counter.
            if in_process_acquired:
                with _vm_count_lock:
                    if _active_vm_count > 0:
                        _active_vm_count -= 1
            raise

    return _Slot(fd=fd, lock_path=lock_path, in_process=in_process_acquired)


def _release_vm_slot(slot: _Slot | None) -> None:
    """Release a slot reserved via _acquire_vm_slot(). Safe to call once
    per slot; subsequent calls with the same slot are no-ops (fd cleared)."""
    global _active_vm_count
    if slot is None:
        return
    if slot.fd is not None:
        try:
            # Closing the fd releases flock automatically (kernel-side).
            os.close(slot.fd)
        except OSError:
            pass
        slot.fd = None
    if slot.in_process:
        with _vm_count_lock:
            if _active_vm_count > 0:
                _active_vm_count -= 1
        slot.in_process = False


def _ensure_node_available() -> None:
    if not shutil.which("node"):
        raise RuntimeError(
            "Node.js is not installed or not in PATH. "
            "Gondolin terminal backend requires node >= 20. "
            "Install with: apt install nodejs (or use nvm)."
        )


def _wait_for_socket(sock_path: str, timeout: float = 10.0) -> None:
    """Poll-connect until the daemon's socket accepts connections or timeout."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.close()
            return
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"gondolin daemon socket {sock_path} never came up ({last_err})"
    )


def _rpc_call(sock_path: str, request: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """One JSON-RPC roundtrip over a fresh AF_UNIX connection.

    Wire format: length-prefixed msgpack frames (u32 BE length + payload).
    Matches ``tools/environments/gondolin_host/src/rpc.mjs``.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    msgpack = _load_msgpack()
    try:
        s.connect(sock_path)
        payload: bytes = msgpack.packb(request, use_bin_type=True)  # type: ignore[assignment]
        header = len(payload).to_bytes(4, "big")
        s.sendall(header + payload)
        buf = bytearray()
        # Read until we have at least one complete frame (4-byte header
        # + payload). The daemon writes exactly one response frame and
        # closes its end of the socket, so we just drain until EOF or
        # until we've seen the full frame.
        while True:
            if len(buf) >= 4:
                n = int.from_bytes(buf[:4], "big")
                if len(buf) >= 4 + n:
                    break
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        if len(buf) < 4:
            raise RuntimeError("daemon closed connection without response")
        n = int.from_bytes(buf[:4], "big")
        if len(buf) < 4 + n:
            raise RuntimeError(
                f"daemon response truncated: expected {n} bytes, got {len(buf) - 4}"
            )
        return msgpack.unpackb(bytes(buf[4:4 + n]), raw=False)
    finally:
        try:
            s.close()
        except OSError:
            pass


class GondolinEnvironment(BaseEnvironment):
    """Run commands inside a Gondolin microVM via a per-session Node daemon."""

    def __init__(
        self,
        sandbox_dir: str,
        cwd: str = "/workspace",
        timeout: int = 180,
        config: dict | None = None,
        *,
        stub_vm: bool = False,
        init_timeout: float = 60.0,
        daemon_path: str | None = None,
        persistent_filesystem: bool = False,
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        _ensure_node_available()

        # Resolve the cross-process lock directory. Order of precedence:
        # 1) config["lock_dir"] (explicit per-call), 2) env var, 3) None
        # (in-process cap only). The env var is mostly for testing /
        # fleet ops; normal Hermes wiring sets the config knob.
        resolved_config = dict(config or {})
        lock_dir = resolved_config.pop("lock_dir", None) or _DEFAULT_LOCK_DIR or None
        # Also respect a config-supplied override of the in-process cap so
        # tests / per-process knobs don't have to monkeypatch the module.
        if "max_concurrent_vms" in resolved_config:
            try:
                cap_override = int(resolved_config.pop("max_concurrent_vms"))
            except (TypeError, ValueError):
                cap_override = None
            if cap_override is not None:
                global _max_concurrent_vms
                _max_concurrent_vms = cap_override

        # Reserve a slot against the concurrent-VM cap BEFORE spawning
        # anything. If we're at the cap, this raises immediately and no
        # daemon/VM resources are touched. Released on cleanup or on
        # init failure (see except block at the end).
        self._slot: _Slot | None = _acquire_vm_slot(lock_dir=lock_dir)
        # Persistence lifecycle: matches docker's container_persistent
        # contract. True keeps the workspace dir on disk across cleanup
        # so the next session for the same task_id resumes the agent's
        # work; False rmtree's it. Stashed here (not in `config`) because
        # it's a host-side lifecycle concern that the daemon doesn't see.
        self._persistent_filesystem = bool(persistent_filesystem)
        try:
            self._init_after_slot(
                sandbox_dir=sandbox_dir,
                config=resolved_config,
                stub_vm=stub_vm,
                init_timeout=init_timeout,
                daemon_path=daemon_path,
                cwd=cwd,
            )
        except BaseException:
            # Any failure between slot acquire and successful init must
            # release the slot AND terminate the daemon, regardless of
            # source (slot logic, daemon spawn, RPC, KeyboardInterrupt).
            #
            # _init_after_slot's inner `except Exception` handles
            # ordinary failures (it calls _terminate_daemon then
            # re-raises). But KeyboardInterrupt / SystemExit slip past
            # that `Exception` filter and reach us here. Without an
            # explicit terminate, a Ctrl-C during _wait_for_socket or
            # the init RPC leaks the 256-512 MB Node+VM allocation.
            # Same applies to any future BaseException-derived
            # exception. _terminate_daemon is idempotent (no-op when
            # _daemon_proc is None or already reaped), so calling it
            # here can't double-kill the inner handler's work.
            try:
                self._terminate_daemon()
            except Exception:  # noqa: BLE001
                pass
            if self._slot is not None:
                _release_vm_slot(self._slot)
                self._slot = None
            raise

    def _init_after_slot(
        self,
        *,
        sandbox_dir: str,
        config: dict | None,
        stub_vm: bool,
        init_timeout: float,
        daemon_path: str | None,
        cwd: str,
    ) -> None:
        self.sandbox_dir = Path(sandbox_dir)
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.sock_path = str(self.sandbox_dir / "gondolin.sock")
        self.config = dict(config or {})
        self.stub_vm = stub_vm

        # Workspace lives in a SUBDIR of sandbox_dir, not at the root. The
        # root holds infra the agent has no business seeing (the daemon's
        # gondolin.sock, future per-session lock/state files). Binding the
        # subdir keeps that infra out of the guest's /workspace listing
        # while still letting the host pick up files the agent wrote.
        self.workspace_dir = self.sandbox_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Default workspace mount: bind workspace_dir to the in-VM cwd via
        # Gondolin's vfs.mounts. This is what makes file tools work against
        # /workspace in the guest — the same bytes appear on the host under
        # workspace_dir, so read_file/write_file/patch can either route
        # through the VM (terminal-based) or use the host path directly.
        # User can opt out by setting `workspace_mount: False` in config; a
        # power-user policy_script that defines its own vfs may want that.
        if "workspace_mount" not in self.config:
            self.config["workspace_mount"] = {
                "guest_path": cwd,
                "host_path": str(self.workspace_dir),
            }
        elif self.config["workspace_mount"] is False:
            # Sentinel for "opt out" — strip so the daemon doesn't see a
            # non-dict and trip its validation.
            self.config.pop("workspace_mount")

        # Hermes-wide skill + credential projection. docker/singularity
        # bind-mount these into the sandbox so skills can reference their
        # own scripts and authenticated CLIs (gh, gcloud, op) find the
        # credential files they expect. Gondolin's vfs.mounts gives us
        # the same primitive via ReadonlyProvider(RealFSProvider(path)).
        # The daemon-side `extra_mounts` config key takes a list of
        # {guest_path, host_path, readonly} entries; we populate it from
        # tools/credential_files.py here so a user setting
        # `terminal.backend: gondolin` gets parity with the other
        # sandbox backends without further config.
        #
        # Users can opt out by setting `project_skills: False` or
        # `project_credentials: False` in the gondolin config — useful
        # for paranoid setups where the sandbox should be as bare as
        # possible.
        project_skills = self.config.pop("project_skills", True)
        project_credentials = self.config.pop("project_credentials", True)
        # Deep-copy: this list (and its entries) is mutated below — overlay
        # setup rewrites host_path and pops "overlay", credential grouping
        # appends to allowed_files. Without a deepcopy two GondolinEnvironment
        # instances built from the same config dict alias their entries and
        # the second instance binds the first instance's overlay merged dir
        # instead of creating its own — defeating overlay isolation.
        import copy as _copy
        extra_mounts = _copy.deepcopy(list(self.config.get("extra_mounts") or []))
        if project_skills or project_credentials:
            try:
                from tools.credential_files import (
                    get_credential_file_mounts,
                    get_skills_directory_mount,
                )
            except ImportError:
                # Hermes core may not be importable in unusual test
                # configs (e.g. running the daemon standalone). Fall
                # through without skills/credentials — the agent still
                # has a working VM.
                get_credential_file_mounts = lambda: []
                get_skills_directory_mount = lambda: []
            if project_skills:
                for m in get_skills_directory_mount():
                    extra_mounts.append({
                        "guest_path": m["container_path"],
                        "host_path": m["host_path"],
                        "readonly": True,
                    })
            if project_credentials:
                # Credential files are individual files. Gondolin's
                # RealFSProvider takes a directory rootPath, so mounting
                # a single file via vfs.mounts doesn't work the way
                # docker's -v $f:$g:ro does — the daemon-side validation
                # accepts files, but gondolin's RealFSProvider does not.
                # As a workaround, group credential files by their
                # parent directory: mount the parent read-only AND emit
                # an `allowed_files` allowlist of the credential
                # basenames so the daemon wraps the provider in a
                # ShadowProvider — every other file in the parent dir
                # surfaces as ENOENT. That preserves docker's file-level
                # isolation property even though we have to mount a
                # directory. See daemon.mjs buildExtraMountProvider.
                # For entries where parent grouping would cause path
                # collisions (same guest_parent → different
                # host_parent), the per-credential mount is skipped and
                # a WARN is logged — the user can mount that credential
                # via wire-injected `secrets:` instead.
                seen_parents: dict[str, str] = {}
                grouped: dict[str, dict] = {}
                for m in get_credential_file_mounts():
                    host_parent = os.path.dirname(m["host_path"])
                    guest_parent = os.path.dirname(m["container_path"])
                    if not host_parent or not guest_parent:
                        continue
                    if guest_parent in seen_parents and seen_parents[guest_parent] != host_parent:
                        logger.warning(
                            "gondolin: skipping credential mount %s (guest parent %s already maps to %s)",
                            m["container_path"], guest_parent, seen_parents[guest_parent],
                        )
                        continue
                    seen_parents[guest_parent] = host_parent
                    basename = os.path.basename(m["container_path"])
                    entry = grouped.setdefault(guest_parent, {
                        "guest_path": guest_parent,
                        "host_path": host_parent,
                        "readonly": True,
                        "allowed_files": [],
                    })
                    # Allowed paths are provider-rooted (absolute under
                    # the mount root); leading "/" + basename.
                    rel = "/" + basename
                    if rel not in entry["allowed_files"]:
                        entry["allowed_files"].append(rel)
                extra_mounts.extend(grouped.values())
        if extra_mounts:
            self.config["extra_mounts"] = extra_mounts

        # Per-mount fuse-overlayfs setup. For each extra_mounts entry
        # with overlay: True, we mount the live host dir as the lower
        # layer of an overlayfs whose upper+work live under this
        # sandbox's directory. The entry's host_path is rewritten in
        # place to point at the merged dir, so the daemon (and the VM
        # via vfs.mounts) sees a normal bind whose writes never touch
        # the host vault. Two concurrent sandboxes thus have fully
        # isolated upper layers — no spooky action across sandboxes.
        # See website/docs/user-guide/configuration.md "Overlay mounts".
        global _SWEEP_DONE
        if not _SWEEP_DONE:
            _sweep_stale_overlay_mounts()
            _SWEEP_DONE = True
        self._overlay_mounts: list[str] = _setup_overlay_mounts(
            extra_mounts, self.sandbox_dir
        )

        # Captured from the init response; useful for tests and for
        # higher-level code that wants to know where the workspace lives.
        # None when workspace_mount is disabled.
        self.workspace_mount: dict | None = None
        # Captured from the init response. Each entry is a dict with keys
        # name (string), type (from_env|from_command|none), error (string),
        # and optionally stderr/stdout. Empty list when all secrets
        # resolved cleanly. Surfaced by `hermes doctor` and logged at WARN
        # so unattended cron jobs don't silently run without credentials.
        self.secret_diagnostics: list[dict] = []
        # Background loop that re-runs `refresh_command` for any secret
        # configured with `refresh: true`. None when no secret needs
        # refresh (the common case). Started after init succeeds.
        self.secret_refresher: SecretRefresher | None = None

        daemon_js = Path(daemon_path) if daemon_path else _DAEMON_JS
        if not daemon_js.exists():
            raise RuntimeError(f"gondolin daemon source not found at {daemon_js}")

        env_vars = os.environ.copy()
        env_vars["GONDOLIN_DAEMON_QUIET"] = "1"
        if stub_vm:
            env_vars["GONDOLIN_DAEMON_STUB_VM"] = "1"

        # Stale socket from a previous crash would make `bind` fail; the
        # daemon also cleans up but pre-clear here to be safe on retries.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

        self._daemon_proc: subprocess.Popen | None = subprocess.Popen(
            ["node", str(daemon_js), "--socket", self.sock_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env_vars,
        )
        # Drain the daemon's stderr pipe so the kernel buffer never
        # fills. Without this, ~64 KB of stderr output is enough
        # to wedge the daemon's write(2) and stall the whole VM.
        self._daemon_stderr_reaper = _start_daemon_stderr_reaper(
            self._daemon_proc, log=logger
        )

        try:
            _wait_for_socket(self.sock_path, timeout=init_timeout)
            response = _rpc_call(
                self.sock_path,
                {"id": 1, "method": "init", "params": {"config": self.config}},
                timeout=init_timeout,
            )
            if response.get("error") is not None:
                err = response["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                # Friendly hint for the rootfs-cap failure mode: the user
                # set `terminal.gondolin.rootfs_size_mb` on an image that
                # doesn't ship resize2fs (or pinned `rootfs.mode='memory'`
                # / `'readonly'`). Point at the fix instead of leaving them
                # with a raw "rootfs.size requires..." line.
                lower = msg.lower()
                if "rootfs.size" in lower or "resize2fs" in lower:
                    msg = (
                        f"{msg}\n\n"
                        f"You set `terminal.gondolin.rootfs_size_mb` but the "
                        f"guest image doesn't ship e2fsprogs (resize2fs), or "
                        f"the rootfs is pinned to a non-resizable mode. Either:\n"
                        f"  • remove `rootfs_size_mb` (the rootfs will be the "
                        f"image's natural size — usually what you want), or\n"
                        f"  • use an image that includes e2fsprogs.\n"
                        f"See website/docs/user-guide/configuration.md "
                        f"'Rootfs cap' for the trade-off."
                    )
                raise RuntimeError(f"gondolin daemon init failed: {msg}")
            result = response.get("result") or {}
            wm = result.get("workspaceMount")
            if isinstance(wm, dict):
                self.workspace_mount = wm
            # Surface secret resolution failures at WARN so they don't get
            # buried in DEBUG. The agent still runs without the credential;
            # this lets the user / cron operator notice and fix.
            sd = result.get("secretDiagnostics")
            if isinstance(sd, list) and sd:
                self.secret_diagnostics = list(sd)
                for entry in self.secret_diagnostics:
                    name = entry.get("name", "?")
                    src = entry.get("type", "?")
                    err = entry.get("error", "?")
                    extra = ""
                    stderr_txt = entry.get("stderr") or ""
                    if stderr_txt:
                        extra = f" stderr={stderr_txt[:500]!r}"
                    logger.warning(
                        "gondolin secret %s (%s) unresolved: %s%s",
                        name, src, err, extra,
                    )

            # Spin up the secret refresh loop for any secret configured
            # with `refresh: true`. Done after init succeeds so we never
            # leak a thread on a failed daemon init.
            self._start_secret_refresher_if_needed()
        except Exception:
            self._terminate_daemon()
            _teardown_overlay_mounts(self._overlay_mounts)
            self._overlay_mounts = []
            raise

    # ------------------------------------------------------------------
    # BaseEnvironment hooks
    # ------------------------------------------------------------------

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> subprocess.Popen:
        """Spawn the gondolin_rpc_call wrapper subprocess.

        ``login`` is accepted for BaseEnvironment compatibility but has no
        effect: Gondolin's VM helper runs each command via ``bash -c`` with
        no profile to source. The session-snapshot machinery still works —
        it captures env vars from the first call and re-sources them on
        subsequent ones — but ``bash -l`` semantics aren't available inside
        the VM.

        Output is streamed by default — the wrapper writes stdout/stderr
        chunks to its pipes as they arrive from the daemon rather than
        buffering until exit. BaseEnvironment's select() drain hands those
        chunks to the agent UI in real time, so a long-running command
        (test suite, build) doesn't appear hung. Set ``stream: False`` in
        the env config to opt out (one-shot exec, full result at end).
        """
        argv = [
            sys.executable,
            "-m",
            "tools.environments.gondolin_rpc_call",
            self.sock_path,
            cmd_string,
            "--timeout-ms",
            str(timeout * 1000),
        ]
        # Streaming defaults to True. Honored per-env via config["stream"].
        if self.config.get("stream", True):
            argv.append("--stream")
        return _popen_bash(argv, stdin_data)

    def _start_secret_refresher_if_needed(self) -> None:
        """Spawn a SecretRefresher if any configured secret has `refresh: true`.

        Refresh config (per-secret) — opt-in:
          refresh: true                     # required to enable refresh
          ttl_seconds: 3600                 # fallback when value isn't a JWT
          refresh_before_expiry_seconds: 300  # optional, default 300

        The refresh command defaults to the secret's ``from_command``. If
        the secret only has ``value`` or ``from_env``, refresh requires an
        explicit ``refresh_command`` (otherwise there's nothing to re-run).
        """
        secrets = self.config.get("secrets") or {}
        refresh_entries: list[dict] = []
        for name, cfg in secrets.items():
            if not isinstance(cfg, dict) or not cfg.get("refresh"):
                continue
            refresh_command = cfg.get("refresh_command") or cfg.get("from_command")
            if not refresh_command:
                logger.warning(
                    "gondolin secret %s: refresh: true but no refresh_command "
                    "or from_command to re-run — refresh disabled",
                    name,
                )
                continue
            tmo = cfg.get("timeout_ms")
            initial = cfg.get("value")
            env = cfg.get("env")
            refresh_entries.append({
                "name": name,
                "refresh_command": refresh_command,
                "ttl_seconds": cfg.get("ttl_seconds"),
                "refresh_before_expiry_seconds": int(
                    cfg.get("refresh_before_expiry_seconds", 300)
                ),
                "initial_value": initial if isinstance(initial, str) else None,
                # Per-secret env: dict that the refresh subprocess sees,
                # on top of the safe POSIX baseline (PATH, HOME, etc.).
                # See _build_safe_env in gondolin_secret_refresh.py.
                "env": env if isinstance(env, dict) else None,
                # Per-secret refresh subprocess timeout (ms). Surfaces
                # the same `timeout_ms` knob that hooks.mjs reads for
                # init-time from_command, so a slow refresh chain gets
                # the same opt-out runway. `bool` is a subclass of `int`
                # in Python, so explicitly exclude it.
                "timeout_ms": tmo if isinstance(tmo, int) and not isinstance(tmo, bool) else None,
            })

        if not refresh_entries:
            return

        self.secret_refresher = SecretRefresher(env_set_secret=self.set_secret)
        for entry in refresh_entries:
            self.secret_refresher.add_secret(**entry)
        self.secret_refresher.start()
        logger.info(
            "gondolin secret refresher started for %d secret(s): %s",
            len(refresh_entries),
            ", ".join(e["name"] for e in refresh_entries),
        )

    def set_secret(
        self,
        name: str,
        *,
        value: str | None = None,
        hosts: list[str] | None = None,
    ) -> None:
        """Update a configured secret's value and/or host list without
        restarting the VM. Routes through the daemon's set_secret RPC,
        which calls Gondolin's secretManager.updateSecret(). At least
        one of ``value`` or ``hosts`` must be provided.

        Use case: a credential-refresh loop (AAD tokens last ~1h) calls
        this when the cached token gets close to expiry, so wire-level
        injection picks up the new value on the next outbound request.

        Raises RuntimeError on unknown secret name or transport error.
        """
        params: dict[str, Any] = {"name": name}
        if value is not None:
            params["value"] = value
        if hosts is not None:
            params["hosts"] = list(hosts)
        if "value" not in params and "hosts" not in params:
            raise ValueError("set_secret: provide at least 'value' or 'hosts'")

        response = _rpc_call(
            self.sock_path,
            {"id": int(time.time() * 1000) & 0xFFFFFFFF, "method": "set_secret", "params": params},
            timeout=10.0,
        )
        err = response.get("error")
        if err is not None:
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"gondolin set_secret({name}) failed: {msg}")

    def cleanup(self) -> None:
        """Send shutdown RPC, wait for daemon exit, kill if it hangs."""
        # Stop the refresh loop FIRST — it calls set_secret, and once the
        # daemon's torn down those calls would surface as scary RPC errors.
        if self.secret_refresher is not None:
            try:
                self.secret_refresher.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("gondolin secret refresher stop error: %s", exc)
            self.secret_refresher = None

        if not self._daemon_proc:
            return

        # Best-effort graceful shutdown via RPC. Time-limited so a wedged
        # daemon doesn't block the calling session forever. Catch any
        # exception (msgpack format errors, truncated frames, unexpected
        # wire shapes) — we're about to SIGTERM anyway, and letting the
        # exception escape would skip _terminate_daemon, workspace
        # rmtree, and slot release, exhausting the concurrent-VM cap.
        try:
            _rpc_call(
                self.sock_path,
                {"id": 9999, "method": "shutdown", "params": {}},
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("gondolin shutdown rpc failed (will SIGTERM): %s", exc)

        self._terminate_daemon()

        # Unmount any per-mount fuse-overlayfs overlays this session
        # spawned. Must happen BEFORE the outer sandbox_dir is rmtree'd
        # (by the lifecycle layer above this class), otherwise the
        # rmtree races with the still-mounted merged dir and fails with
        # EBUSY. Lazy unmount (-z) keeps us safe even if something in
        # the VM still has open file descriptors at this instant.
        if getattr(self, "_overlay_mounts", None):
            _teardown_overlay_mounts(self._overlay_mounts)
            self._overlay_mounts = []

        # Honor the persistence lifecycle. Non-persistent sessions rm the
        # workspace dir so its content doesn't leak into the next session
        # for the same task_id. Persistent sessions leave it untouched
        # (the next GondolinEnvironment for the same task_id will see and
        # re-mount the existing dir). Matches docker.py:cleanup() which
        # does shutil.rmtree on the workspace + home dirs when persistent
        # is False.
        if not self._persistent_filesystem:
            try:
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("gondolin workspace cleanup failed: %s", exc)

        # Release the concurrent-VM slot so the next session can spawn.
        # Guarded against double-cleanup (cleanup called twice would
        # otherwise under-count active VMs).
        if self._slot is not None:
            _release_vm_slot(self._slot)
            self._slot = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _terminate_daemon(self) -> None:
        proc = self._daemon_proc
        if proc is None:
            return
        self._daemon_proc = None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except OSError as exc:
            logger.debug("gondolin daemon teardown error: %s", exc)
        # Close the captured stderr pipe so the reaper exits and the
        # parent's fd doesn't accumulate across long-lived sessions
        # that create/destroy many envs. The reaper, mid-readline on
        # this fd, sees the close as EOF or ValueError and exits.
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass
        reaper = getattr(self, "_daemon_stderr_reaper", None)
        if reaper is not None:
            reaper.join(timeout=1.0)
            self._daemon_stderr_reaper = None
        # Clean up any leftover socket file (daemon's own SIGTERM
        # handler also tries, but races with our terminate()).
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
