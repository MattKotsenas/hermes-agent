"""Tests for tools.environments.sandbox_inventory — backend-agnostic
discovery and pruning of `~/.hermes/sandboxes/` per-task directories.

The inventory module reasons over three known layouts:

  sandboxes/docker/<task_id>/            (per-task, persistent_filesystem)
  sandboxes/gondolin/<task_id>/          (per-task, mirrors docker shape)
  sandboxes/gondolin/.locks/             (host-wide flock slots — counted
                                          but never pruned)
  sandboxes/singularity/                 (shared scratch — NOT per-task,
                                          counted but never pruned)

Anything else under the root is reported under backend='unknown' and is
also never pruned by default (the CLI surfaces it; a future `--all`
flag could include it).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tools.environments import sandbox_inventory as inv


def _make_dir(root: Path, rel: str, *, age_days: float = 0.0, size_kb: int = 4) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    # Drop a file so size > 0.
    (d / "blob").write_bytes(b"x" * (size_kb * 1024))
    if age_days > 0:
        mtime = time.time() - age_days * 86400
        # Set mtime on directory itself + the inner file.
        os.utime(d / "blob", (mtime, mtime))
        os.utime(d, (mtime, mtime))
    return d


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    """Isolated sandbox root.

    We cannot use ``tmp_path`` directly because the test environment's
    autouse fixtures drop a ``hermes_test/`` sibling into it; nesting our
    root one level down keeps the scan free of foreign children that
    would otherwise get classified as ``backend='unknown'``.
    """
    root = tmp_path / "sandboxes"
    root.mkdir()
    return root


class TestScanLayouts:
    def test_classifies_docker_subdirs(self, sandbox_root):
        _make_dir(sandbox_root, "docker/task_A")
        _make_dir(sandbox_root, "docker/task_B")
        report = inv.scan(sandbox_root)
        backends = {e.backend for e in report.entries}
        assert backends == {"docker"}
        assert len(report.entries) == 2
        assert {e.task_id for e in report.entries} == {"task_A", "task_B"}

    def test_classifies_gondolin_subdirs(self, sandbox_root):
        _make_dir(sandbox_root, "gondolin/abc123")
        _make_dir(sandbox_root, "gondolin/def456")
        report = inv.scan(sandbox_root)
        backends = {e.backend for e in report.entries}
        assert backends == {"gondolin"}
        assert {e.task_id for e in report.entries} == {"abc123", "def456"}

    def test_gondolin_locks_dir_counted_but_not_prunable(self, sandbox_root):
        # gondolin/.locks/ holds host-wide flock slot files. It shows up
        # in the inventory so a curious user sees it, but it's never
        # picked for pruning.
        _make_dir(sandbox_root, "gondolin/.locks")
        _make_dir(sandbox_root, "gondolin/task1")
        report = inv.scan(sandbox_root)
        locks = [e for e in report.entries if e.backend == "gondolin" and e.task_id is None]
        assert len(locks) == 1
        assert locks[0].prunable is False
        tasks = [e for e in report.entries if e.backend == "gondolin" and e.task_id is not None]
        assert [e.task_id for e in tasks] == ["task1"]

    def test_singularity_is_shared_scratch_not_per_task(self, sandbox_root):
        # singularity/ itself is a shared dir, not a per-task one.
        _make_dir(sandbox_root, "singularity/some-scratch-file-dir")
        report = inv.scan(sandbox_root)
        # The 'singularity' root is counted as a single shared entry,
        # NOT as per-task entries underneath.
        sing = [e for e in report.entries if e.backend == "singularity"]
        assert len(sing) == 1
        assert sing[0].task_id is None
        assert sing[0].prunable is False

    def test_unknown_dir_reported_but_not_prunable(self, sandbox_root):
        _make_dir(sandbox_root, "some_other_thing")
        report = inv.scan(sandbox_root)
        unk = [e for e in report.entries if e.backend == "unknown"]
        assert len(unk) == 1
        assert unk[0].prunable is False

    def test_mixed_layout_full_report(self, sandbox_root):
        _make_dir(sandbox_root, "docker/t1")
        _make_dir(sandbox_root, "gondolin/t2")
        _make_dir(sandbox_root, "singularity/cache")
        _make_dir(sandbox_root, "weird_thing")
        report = inv.scan(sandbox_root)
        assert len(report.entries) == 4
        assert report.total_bytes > 0
        # Two prunable (docker + gondolin), two not (singularity + unknown).
        assert sum(1 for e in report.entries if e.prunable) == 2

    def test_missing_root_returns_empty_report(self, sandbox_root):
        report = inv.scan(sandbox_root / "does_not_exist")
        assert report.entries == []
        assert report.total_bytes == 0


class TestAgeFiltering:
    def test_select_stale_by_age(self, sandbox_root):
        _make_dir(sandbox_root, "docker/fresh", age_days=0.5)
        _make_dir(sandbox_root, "docker/old", age_days=10)
        report = inv.scan(sandbox_root)
        stale = inv.select_stale(report, older_than_days=7)
        assert len(stale) == 1
        assert stale[0].task_id == "old"

    def test_age_filter_only_picks_prunable(self, sandbox_root):
        # Singularity dir is shared scratch and old, but must NOT be selected.
        _make_dir(sandbox_root, "singularity/cache", age_days=30)
        _make_dir(sandbox_root, "docker/old", age_days=30)
        stale = inv.select_stale(inv.scan(sandbox_root), older_than_days=7)
        assert {e.backend for e in stale} == {"docker"}

    def test_age_filter_backend_restriction(self, sandbox_root):
        _make_dir(sandbox_root, "docker/old", age_days=30)
        _make_dir(sandbox_root, "gondolin/old", age_days=30)
        stale = inv.select_stale(
            inv.scan(sandbox_root), older_than_days=7, backends={"gondolin"}
        )
        assert {e.backend for e in stale} == {"gondolin"}


class TestPrune:
    def test_prune_dry_run_does_not_delete(self, sandbox_root):
        d = _make_dir(sandbox_root, "docker/old", age_days=30)
        report = inv.scan(sandbox_root)
        result = inv.prune(report.entries, dry_run=True)
        assert d.exists()
        assert result.deleted == 0
        assert result.would_delete == 1
        assert result.bytes_freed == 0

    def test_prune_actual_delete(self, sandbox_root):
        d = _make_dir(sandbox_root, "docker/old", age_days=30, size_kb=16)
        report = inv.scan(sandbox_root)
        result = inv.prune(report.entries, dry_run=False)
        assert not d.exists()
        assert result.deleted == 1
        assert result.bytes_freed > 0

    def test_prune_skips_non_prunable(self, sandbox_root):
        sing = _make_dir(sandbox_root, "singularity/cache", age_days=30)
        unk = _make_dir(sandbox_root, "weird", age_days=30)
        report = inv.scan(sandbox_root)
        # Even when fed the full entry list, non-prunable entries survive.
        result = inv.prune(report.entries, dry_run=False)
        assert sing.exists()
        assert unk.exists()
        assert result.deleted == 0
        assert result.skipped == 2


class TestSummary:
    def test_human_summary_includes_counts_and_bytes(self, sandbox_root):
        _make_dir(sandbox_root, "docker/t1", size_kb=100)
        _make_dir(sandbox_root, "gondolin/t2", size_kb=200)
        report = inv.scan(sandbox_root)
        summary = inv.human_summary(report)
        assert "docker" in summary
        assert "gondolin" in summary
        # Total bytes are surfaced in human-readable form.
        assert "KB" in summary or "MB" in summary

    def test_summary_threshold_flagging(self, sandbox_root):
        _make_dir(sandbox_root, "docker/old", age_days=60, size_kb=4)
        report = inv.scan(sandbox_root)
        flag = inv.should_flag_in_doctor(
            report, min_total_mb=0, max_age_days=30
        )
        assert flag is True

    def test_summary_threshold_quiet(self, sandbox_root):
        _make_dir(sandbox_root, "docker/fresh", age_days=1, size_kb=4)
        report = inv.scan(sandbox_root)
        # Tiny + recent → don't bother the user.
        flag = inv.should_flag_in_doctor(
            report, min_total_mb=500, max_age_days=30
        )
        assert flag is False
