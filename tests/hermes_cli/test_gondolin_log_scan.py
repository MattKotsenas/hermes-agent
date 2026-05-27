"""Tests for the doctor probe that scans errors.log for gondolin secret
resolution failures. Pure parsing + dedup logic, no IO except the read
helper test."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hermes_cli.gondolin_log_scan import (
    SecretWarning,
    humanize_age,
    latest_per_secret,
    parse_lines,
    scan_errors_log,
)


# ---- parse_lines -------------------------------------------------------

INIT_FAIL_LINE = (
    "2026-05-26 14:23:45 WARNING tools.environments.gondolin: "
    "gondolin secret AAD_TOKEN (from_command) unresolved: "
    "command exited with code 7 stderr='broken-creds\\nauth failed'"
)
REFRESH_FAIL_LINE = (
    "2026-05-26 15:10:02 WARNING tools.environments.gondolin_secret_refresh: "
    "gondolin secret AAD_TOKEN refresh failed: refresh command exited 1 "
    "stderr='transient blip' — retrying"
)
ENV_UNSET_LINE = (
    "2026-05-26 14:23:45 WARNING tools.environments.gondolin: "
    "gondolin secret GITHUB_PAT (from_env) unresolved: env var GITHUB_PAT is unset or empty"
)
UNRELATED_LINE = (
    "2026-05-26 14:23:45 WARNING agent.cache: cache miss for foo"
)


def test_parse_lines_extracts_init_failure_fields():
    warnings = parse_lines([INIT_FAIL_LINE])
    assert len(warnings) == 1
    w = warnings[0]
    assert w.name == "AAD_TOKEN"
    assert w.kind == "init"
    assert w.type == "from_command"
    assert "exit" in w.error and "7" in w.error
    assert "broken-creds" in w.stderr
    assert w.when == datetime(2026, 5, 26, 14, 23, 45)


def test_parse_lines_extracts_refresh_failure_fields():
    warnings = parse_lines([REFRESH_FAIL_LINE])
    assert len(warnings) == 1
    w = warnings[0]
    assert w.name == "AAD_TOKEN"
    assert w.kind == "refresh"
    assert w.type == ""  # not set for refresh kind
    assert "1" in w.error
    assert "transient blip" in w.stderr


def test_parse_lines_handles_from_env_with_no_stderr():
    warnings = parse_lines([ENV_UNSET_LINE])
    assert len(warnings) == 1
    w = warnings[0]
    assert w.name == "GITHUB_PAT"
    assert w.type == "from_env"
    assert "GITHUB_PAT" in w.error
    assert w.stderr == ""


def test_parse_lines_ignores_unrelated_warnings():
    warnings = parse_lines([UNRELATED_LINE, INIT_FAIL_LINE, UNRELATED_LINE])
    assert len(warnings) == 1
    assert warnings[0].name == "AAD_TOKEN"


def test_parse_lines_handles_iso_t_timestamp_format():
    """The logging formatter usually writes 'YYYY-MM-DD HH:MM:SS' but ISO
    'T' separator should also parse — defensive against config changes."""
    iso_line = INIT_FAIL_LINE.replace("2026-05-26 14:23:45", "2026-05-26T14:23:45")
    warnings = parse_lines([iso_line])
    assert len(warnings) == 1
    assert warnings[0].when == datetime(2026, 5, 26, 14, 23, 45)


# ---- latest_per_secret -------------------------------------------------

def test_latest_per_secret_dedups_keeping_most_recent():
    """Multiple warnings for the same (name, kind) — keep only the latest."""
    old = SecretWarning(
        name="AAD", kind="init", type="from_command",
        error="old error", stderr="", when=datetime(2026, 5, 25, 10, 0, 0),
        raw_line="",
    )
    new = SecretWarning(
        name="AAD", kind="init", type="from_command",
        error="new error", stderr="", when=datetime(2026, 5, 26, 11, 0, 0),
        raw_line="",
    )
    result = latest_per_secret([old, new])
    assert len(result) == 1
    assert result[0].error == "new error"


def test_latest_per_secret_keeps_separate_entries_per_kind():
    """init and refresh for the same secret are different problems —
    surface both so the user knows refresh broke even if init was fine."""
    init_fail = SecretWarning(
        name="AAD", kind="init", type="from_command",
        error="init err", stderr="", when=datetime(2026, 5, 25, 10, 0, 0),
        raw_line="",
    )
    refresh_fail = SecretWarning(
        name="AAD", kind="refresh", type="",
        error="refresh err", stderr="", when=datetime(2026, 5, 26, 11, 0, 0),
        raw_line="",
    )
    result = latest_per_secret([init_fail, refresh_fail])
    assert len(result) == 2
    # Most recent first.
    assert result[0].kind == "refresh"


def test_latest_per_secret_sorts_newest_first():
    a = SecretWarning(
        name="A", kind="init", type="from_command",
        error="", stderr="", when=datetime(2026, 5, 25, 10, 0, 0), raw_line="",
    )
    b = SecretWarning(
        name="B", kind="init", type="from_command",
        error="", stderr="", when=datetime(2026, 5, 26, 11, 0, 0), raw_line="",
    )
    c = SecretWarning(
        name="C", kind="init", type="from_command",
        error="", stderr="", when=datetime(2026, 5, 24, 9, 0, 0), raw_line="",
    )
    result = latest_per_secret([a, b, c])
    assert [w.name for w in result] == ["B", "A", "C"]


# ---- scan_errors_log ---------------------------------------------------

def test_scan_errors_log_returns_empty_when_missing(tmp_path):
    """Fresh install — no errors.log yet — surfaces nothing."""
    assert scan_errors_log(tmp_path / "errors.log") == []


def test_scan_errors_log_reads_and_dedups(tmp_path):
    log_path = tmp_path / "errors.log"
    log_path.write_text("\n".join([
        INIT_FAIL_LINE,
        UNRELATED_LINE,
        # Same secret a day later with different error — should win.
        INIT_FAIL_LINE.replace(
            "2026-05-26 14:23:45", "2026-05-27 09:00:00"
        ).replace("code 7", "code 12"),
        ENV_UNSET_LINE,
    ]))
    result = scan_errors_log(log_path)
    assert len(result) == 2
    by_name = {w.name: w for w in result}
    assert "12" in by_name["AAD_TOKEN"].error  # latest of two
    assert by_name["GITHUB_PAT"].type == "from_env"


# ---- humanize_age ------------------------------------------------------

def test_humanize_age_renders_compact_relative_times():
    now = datetime(2026, 5, 26, 12, 0, 0)
    assert humanize_age(now - timedelta(seconds=5), now=now) == "5s ago"
    assert humanize_age(now - timedelta(minutes=3), now=now) == "3m ago"
    assert humanize_age(now - timedelta(hours=2), now=now) == "2h ago"
    assert humanize_age(now - timedelta(days=4), now=now) == "4d ago"


def test_humanize_age_handles_future_timestamps():
    """Clock skew on a fleet machine could produce a future timestamp;
    don't crash."""
    now = datetime(2026, 5, 26, 12, 0, 0)
    result = humanize_age(now + timedelta(minutes=5), now=now)
    assert "future" in result
