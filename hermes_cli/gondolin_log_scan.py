"""Parse recent gondolin secret-resolution warnings out of errors.log.

Used by ``hermes doctor`` to surface failures the user might have missed
without re-running auth commands (which would be slow / side-effect-y).

The shape is deliberately narrow: read the tail of errors.log, regex out
gondolin secret lines, deduplicate by secret name keeping the most recent
occurrence, return a tidy list of dicts. Doctor renders them as info lines.

Stale entries are not filtered here — doctor surfaces "happened N hours
ago" so the user can decide whether to act. We don't run probes against
the actual auth commands because (a) slow and (b) doctor is read-only by
contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Format: "2026-05-26 14:23:45 WARNING ... tools.environments.gondolin: gondolin secret AAD_TOKEN (from_command) unresolved: command exited with code 7 stderr='...'"
# Also matches refresh-loop failures: "gondolin secret AAD_TOKEN refresh failed: refresh command exited 1 stderr='...' — retrying"
_TS_PATTERN = r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
_INIT_FAIL_RE = re.compile(
    _TS_PATTERN
    + r".*tools\.environments\.gondolin.*"
    + r"gondolin secret (?P<name>\S+) \((?P<type>from_env|from_command|none)\) unresolved: (?P<error>.+?)(?: stderr=(?P<stderr>'.+?'))?$"
)
_REFRESH_FAIL_RE = re.compile(
    _TS_PATTERN
    + r".*tools\.environments\.gondolin.*"
    + r"gondolin secret (?P<name>\S+) refresh failed: (?P<error>.+?)(?: stderr=(?P<stderr>'.+?'))?(?: — retrying)?$"
)


@dataclass
class SecretWarning:
    """A single gondolin secret warning parsed from errors.log."""
    name: str           # secret name, e.g. "AAD_TOKEN"
    kind: str           # "init" (init-time resolve fail) or "refresh"
    type: str           # "from_command" / "from_env" / "none" — empty for refresh-kind
    error: str          # human-readable cause
    stderr: str         # captured stderr tail, empty when not present
    when: datetime      # parsed timestamp
    raw_line: str       # original log line for forensics


def _parse_ts(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_lines(lines: list[str]) -> list[SecretWarning]:
    """Parse a list of log lines, return SecretWarning entries in file order."""
    warnings: list[SecretWarning] = []
    for line in lines:
        m = _INIT_FAIL_RE.search(line)
        if m:
            ts = _parse_ts(m.group(1).replace("T", " "))
            if ts is None:
                continue
            warnings.append(SecretWarning(
                name=m.group("name"),
                kind="init",
                type=m.group("type"),
                error=m.group("error").strip(),
                stderr=(m.group("stderr") or "").strip("'"),
                when=ts,
                raw_line=line.rstrip(),
            ))
            continue
        m = _REFRESH_FAIL_RE.search(line)
        if m:
            ts = _parse_ts(m.group(1).replace("T", " "))
            if ts is None:
                continue
            warnings.append(SecretWarning(
                name=m.group("name"),
                kind="refresh",
                type="",
                error=m.group("error").strip(),
                stderr=(m.group("stderr") or "").strip("'"),
                when=ts,
                raw_line=line.rstrip(),
            ))
    return warnings


def latest_per_secret(warnings: list[SecretWarning]) -> list[SecretWarning]:
    """Keep only the most recent warning per (name, kind) tuple."""
    latest: dict[tuple[str, str], SecretWarning] = {}
    for w in warnings:
        key = (w.name, w.kind)
        prev = latest.get(key)
        if prev is None or w.when > prev.when:
            latest[key] = w
    # Sort newest first so doctor shows fresh issues at the top.
    return sorted(latest.values(), key=lambda w: w.when, reverse=True)


def scan_errors_log(
    errors_log_path: Path,
    *,
    max_lines: int = 2000,
) -> list[SecretWarning]:
    """Read the tail of errors.log and return deduped per-secret warnings.

    Returns an empty list when the file doesn't exist (fresh install) or
    can't be read. Doctor treats this as 'no known issues'.
    """
    if not errors_log_path.exists():
        return []
    try:
        # Read the whole file (rotated log handler caps at ~2MB so this is
        # bounded). Slicing to max_lines is a backstop.
        with open(errors_log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    tail = all_lines[-max_lines:]
    return latest_per_secret(parse_lines(tail))


def humanize_age(when: datetime, *, now: datetime | None = None) -> str:
    """Render the age of a warning timestamp in compact relative form."""
    ref = now or datetime.now()
    delta = ref - when
    if delta.total_seconds() < 0:
        return "in the future"  # clock skew
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"
