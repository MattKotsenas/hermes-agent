"""Backend-agnostic sandbox-directory inventory + prune.

Scans ``~/.hermes/sandboxes/`` (or the override in TERMINAL_SANDBOX_DIR via
:func:`tools.environments.base.get_sandbox_dir`) and classifies what's there
so users can see disk usage and prune stale per-task sandboxes without
having to remember which backend wrote what.

Known layouts (all live side-by-side under one root):

  - ``docker/<task_id>/``      per-task, prunable
  - ``gondolin-<task_id>/``    per-task, prunable
  - ``singularity/``           shared scratch, NOT per-task — counted but
                               never pruned (deleting it would nuke the
                               SIF cache that's expensive to rebuild)

Anything else is reported under ``backend="unknown"`` and skipped by prune.
The CLI surfaces these so a curious user can investigate; an opt-in flag
could include them later.

Pure-functions over a directory tree. No subprocess, no backend imports —
this keeps it testable in isolation and safe to call from ``hermes
doctor`` (which must not import heavyweight tool modules).
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Per-task gondolin dir name: "gondolin-<task_id>" (task_id may contain
# alnum, dash, underscore).
_GONDOLIN_PREFIX = "gondolin-"
_SHARED_BACKEND_DIRS = {"singularity"}


@dataclass
class SandboxEntry:
    """One discovered directory under the sandbox root."""
    path: Path
    backend: str  # "docker", "gondolin", "singularity", "unknown"
    task_id: Optional[str]  # None for shared dirs
    size_bytes: int
    mtime: float
    prunable: bool  # False for shared backends (singularity) + unknown

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.mtime) / 86400.0)


@dataclass
class SandboxReport:
    """Result of a scan: every entry + aggregates."""
    root: Path
    entries: List[SandboxEntry] = field(default_factory=list)
    total_bytes: int = 0


@dataclass
class PruneResult:
    deleted: int = 0
    would_delete: int = 0
    skipped: int = 0
    bytes_freed: int = 0
    errors: List[str] = field(default_factory=list)


def _dir_size(path: Path) -> int:
    """Recursive byte count. Best-effort: errors on individual files are
    swallowed so a broken symlink doesn't poison the whole scan."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass
    return total


def _dir_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except (OSError, PermissionError):
        return 0.0


def _classify_docker_subdirs(docker_root: Path) -> List[SandboxEntry]:
    """`docker/` itself is a backend bucket — each child is one task."""
    out: List[SandboxEntry] = []
    if not docker_root.is_dir():
        return out
    for child in docker_root.iterdir():
        if not child.is_dir():
            continue
        out.append(SandboxEntry(
            path=child,
            backend="docker",
            task_id=child.name,
            size_bytes=_dir_size(child),
            mtime=_dir_mtime(child),
            prunable=True,
        ))
    return out


def _classify_gondolin(entry_path: Path) -> SandboxEntry:
    """`gondolin-<task_id>/` is a single per-task dir."""
    task_id = entry_path.name[len(_GONDOLIN_PREFIX):]
    return SandboxEntry(
        path=entry_path,
        backend="gondolin",
        task_id=task_id,
        size_bytes=_dir_size(entry_path),
        mtime=_dir_mtime(entry_path),
        prunable=True,
    )


def _classify_shared(entry_path: Path, backend: str) -> SandboxEntry:
    """Shared scratch (e.g. singularity SIF cache). Counted but not prunable."""
    return SandboxEntry(
        path=entry_path,
        backend=backend,
        task_id=None,
        size_bytes=_dir_size(entry_path),
        mtime=_dir_mtime(entry_path),
        prunable=False,
    )


def _classify_unknown(entry_path: Path) -> SandboxEntry:
    return SandboxEntry(
        path=entry_path,
        backend="unknown",
        task_id=None,
        size_bytes=_dir_size(entry_path),
        mtime=_dir_mtime(entry_path),
        prunable=False,
    )


def scan(root: Path) -> SandboxReport:
    """Walk ``root`` one level deep and classify each entry.

    Returns an empty report if the root doesn't exist (no sandboxes
    have ever been created — nothing to do).
    """
    root = Path(root)
    if not root.is_dir():
        return SandboxReport(root=root)

    entries: List[SandboxEntry] = []
    for child in root.iterdir():
        try:
            if not child.is_dir():
                continue
            name = child.name
            if name == "docker":
                entries.extend(_classify_docker_subdirs(child))
            elif name.startswith(_GONDOLIN_PREFIX) and len(name) > len(_GONDOLIN_PREFIX):
                entries.append(_classify_gondolin(child))
            elif name in _SHARED_BACKEND_DIRS:
                entries.append(_classify_shared(child, name))
            else:
                entries.append(_classify_unknown(child))
        except (OSError, PermissionError) as exc:
            logger.debug("Skipping sandbox entry %s: %s", child, exc)
            continue

    total = sum(e.size_bytes for e in entries)
    return SandboxReport(root=root, entries=entries, total_bytes=total)


def select_stale(
    report: SandboxReport,
    *,
    older_than_days: float,
    backends: Optional[Set[str]] = None,
) -> List[SandboxEntry]:
    """Filter the report down to prunable entries older than the threshold.

    Non-prunable entries (singularity, unknown) are never returned, even if
    they're ancient. ``backends`` further restricts to specific backends.
    """
    out: List[SandboxEntry] = []
    for e in report.entries:
        if not e.prunable:
            continue
        if backends is not None and e.backend not in backends:
            continue
        if e.age_days < older_than_days:
            continue
        out.append(e)
    return out


def prune(
    entries: Iterable[SandboxEntry],
    *,
    dry_run: bool = False,
) -> PruneResult:
    """Delete each entry's directory tree. Non-prunable entries are skipped.

    Safe to call with a mixed list (e.g. the full report's entries) —
    only ``prunable=True`` ones get touched.
    """
    result = PruneResult()
    for entry in entries:
        if not entry.prunable:
            result.skipped += 1
            continue
        if dry_run:
            result.would_delete += 1
            continue
        try:
            shutil.rmtree(entry.path)
            result.deleted += 1
            result.bytes_freed += entry.size_bytes
        except (OSError, PermissionError) as exc:
            result.errors.append(f"{entry.path}: {exc}")
            logger.warning("Failed to prune %s: %s", entry.path, exc)
    return result


def human_summary(report: SandboxReport) -> str:
    """One-line summary suitable for `hermes doctor` info output."""
    if not report.entries:
        return "sandboxes: empty"

    by_backend: dict[str, tuple[int, int]] = {}  # backend -> (count, bytes)
    oldest_age = 0.0
    for e in report.entries:
        count, total = by_backend.get(e.backend, (0, 0))
        by_backend[e.backend] = (count + 1, total + e.size_bytes)
        if e.prunable and e.age_days > oldest_age:
            oldest_age = e.age_days

    parts = []
    for backend in sorted(by_backend):
        count, total = by_backend[backend]
        parts.append(f"{backend}={count} ({_fmt_bytes(total)})")
    line = f"sandboxes: {', '.join(parts)}; total {_fmt_bytes(report.total_bytes)}"
    if oldest_age >= 1:
        line += f"; oldest {int(oldest_age)}d"
    return line


def should_flag_in_doctor(
    report: SandboxReport,
    *,
    min_total_mb: int = 500,
    max_age_days: int = 30,
) -> bool:
    """Decide whether ``hermes doctor`` should mention sandbox usage.

    Returns True if either:
      - total size > min_total_mb MB, or
      - any prunable entry older than max_age_days

    Both are user-tunable knobs (CLI flags pass them through).
    """
    if report.total_bytes / (1024 * 1024) > min_total_mb:
        return True
    for e in report.entries:
        if e.prunable and e.age_days > max_age_days:
            return True
    return False


def _fmt_bytes(n: int) -> str:
    """Mirror the formatter in hermes_cli.checkpoints so output looks
    consistent across `hermes doctor`, `hermes checkpoints`, and
    `hermes sandboxes`."""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n or 0)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
