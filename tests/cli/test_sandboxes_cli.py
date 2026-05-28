"""Tests for hermes_cli.sandboxes — the CLI face for sandbox inventory.

These mock out ``get_sandbox_dir()`` so the tests stay hermetic and don't
touch ``~/.hermes/sandboxes/``.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from hermes_cli import sandboxes as cli


def _make_dir(root: Path, rel: str, *, age_days: float = 0.0, size_kb: int = 4) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "blob").write_bytes(b"x" * (size_kb * 1024))
    if age_days > 0:
        mtime = time.time() - age_days * 86400
        os.utime(d / "blob", (mtime, mtime))
        os.utime(d, (mtime, mtime))
    return d


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``get_sandbox_dir`` at an isolated directory for the duration
    of one test."""
    root = tmp_path / "sandboxes"
    root.mkdir()
    monkeypatch.setattr(
        "hermes_cli.sandboxes.get_sandbox_dir",
        lambda: root,
    )
    return root


def _ns(**kwargs) -> argparse.Namespace:
    """Build an argparse Namespace with the prune defaults filled in."""
    defaults = {
        "older_than": 7.0,
        "backend": None,
        "dry_run": False,
        "verbose": False,
        "limit": 20,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestStatus:
    def test_status_empty_root(self, sandbox_root, capsys):
        rc = cli.cmd_status(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "Total size:      0 B" in out
        assert "Entries:         0" in out

    def test_status_shows_per_backend_breakdown(self, sandbox_root, capsys):
        _make_dir(sandbox_root, "docker/t1", size_kb=64)
        _make_dir(sandbox_root, "gondolin/t2", size_kb=128)
        rc = cli.cmd_status(_ns())
        out = capsys.readouterr().out
        assert rc == 0
        assert "docker" in out
        assert "gondolin" in out
        # Both classes are prunable per-task dirs.
        assert "yes" in out

    def test_status_singularity_is_not_prunable(self, sandbox_root, capsys):
        _make_dir(sandbox_root, "singularity/cache", size_kb=32)
        cli.cmd_status(_ns())
        out = capsys.readouterr().out
        # singularity row should be marked not-prunable. Match the BACKEND
        # column rather than substring-on-line — the Sandbox-root header
        # may also contain the word if pytest's tmp_path happens to.
        sing_rows = [
            l for l in out.splitlines()
            if l.startswith("  singularity")
        ]
        assert len(sing_rows) == 1
        assert "no" in sing_rows[0]

    def test_status_verbose_lists_per_task(self, sandbox_root, capsys):
        _make_dir(sandbox_root, "docker/some_task_id", size_kb=16)
        cli.cmd_status(_ns(verbose=True))
        out = capsys.readouterr().out
        # Verbose mode adds a TASK_ID column header.
        assert "TASK_ID" in out
        assert "some_task_id" in out


class TestPrune:
    def test_prune_nothing_stale(self, sandbox_root, capsys):
        _make_dir(sandbox_root, "docker/fresh", age_days=0.5)
        rc = cli.cmd_prune(_ns(older_than=7))
        out = capsys.readouterr().out
        assert rc == 0
        assert "No sandboxes older than" in out

    def test_prune_dry_run(self, sandbox_root, capsys):
        d = _make_dir(sandbox_root, "docker/old", age_days=30)
        rc = cli.cmd_prune(_ns(older_than=7, dry_run=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert d.exists()
        assert "Would delete" in out
        assert "Dry run" in out

    def test_prune_actual(self, sandbox_root, capsys):
        d = _make_dir(sandbox_root, "docker/old", age_days=30, size_kb=64)
        rc = cli.cmd_prune(_ns(older_than=7))
        out = capsys.readouterr().out
        assert rc == 0
        assert not d.exists()
        assert "Deleted 1 sandbox" in out

    def test_prune_backend_filter(self, sandbox_root, capsys):
        gd = _make_dir(sandbox_root, "gondolin/old", age_days=30)
        dk = _make_dir(sandbox_root, "docker/old", age_days=30)
        rc = cli.cmd_prune(_ns(older_than=7, backend="gondolin"))
        out = capsys.readouterr().out
        assert rc == 0
        # Only the gondolin dir should be gone; docker survives.
        assert not gd.exists()
        assert dk.exists()
        assert "gondolin" in out

    def test_prune_never_touches_singularity(self, sandbox_root, capsys):
        sing = _make_dir(sandbox_root, "singularity/cache", age_days=365)
        rc = cli.cmd_prune(_ns(older_than=7))
        out = capsys.readouterr().out
        assert rc == 0
        # singularity is shared scratch — must survive even ancient.
        assert sing.exists()
        assert "No sandboxes older than" in out


class TestRegister:
    def test_register_cli_wires_subcommands(self):
        """Smoke test: parser registers status + prune subcommands."""
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        # Bare invocation defaults to status.
        ns = parser.parse_args([])
        assert ns.func is cli.cmd_status

        ns = parser.parse_args(["status"])
        assert ns.func is cli.cmd_status

        ns = parser.parse_args(["prune", "--dry-run", "--older-than", "3"])
        assert ns.func is cli.cmd_prune
        assert ns.dry_run is True
        assert ns.older_than == 3.0
