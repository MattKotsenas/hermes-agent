"""Tests for SecretRefresher — the background loop that periodically re-runs
``refresh_command`` and pushes new values via ``env.set_secret()`` so
short-lived credentials (AAD ~1h tokens, etc.) don't expire mid-session.

The refresher is decoupled from GondolinEnvironment for testability: it
takes a callable that does the secret push (so tests don't need a real
daemon) and a ``time_source`` so we can fast-forward the schedule.

The integration with GondolinEnvironment (env wires up the refresher when
any secret config has refresh metadata) is tested separately against the
real env in test_gondolin_environment.py.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest

from tools.environments.gondolin_secret_refresh import (
    SecretRefresher,
    parse_jwt_exp,
    plan_refresh_schedule,
)


# ---- JWT exp parsing ---------------------------------------------------

def _make_jwt(exp_unix: int | None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_dict = {"exp": exp_unix} if exp_unix is not None else {}
    payload = base64.urlsafe_b64encode(
        json.dumps(payload_dict).encode()
    ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def test_parse_jwt_exp_extracts_unix_timestamp():
    token = _make_jwt(exp_unix=1893456000)  # 2030-01-01
    assert parse_jwt_exp(token) == 1893456000


def test_parse_jwt_exp_returns_none_for_non_jwt():
    assert parse_jwt_exp("not-a-jwt") is None
    assert parse_jwt_exp("opaque-api-key-12345") is None


def test_parse_jwt_exp_returns_none_when_exp_missing():
    token = _make_jwt(exp_unix=None)
    assert parse_jwt_exp(token) is None


def test_parse_jwt_exp_handles_padding_correctly():
    # JWT base64url omits padding; parser must handle that without crashing.
    # Construct a payload whose b64 length needs padding.
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 1893456000, "extra": "padding-pads"}).encode()
    ).rstrip(b"=").decode()
    token = f"abc.{payload}.xyz"
    assert parse_jwt_exp(token) == 1893456000


# ---- Schedule planning -------------------------------------------------

def test_plan_refresh_schedule_uses_jwt_exp_when_available():
    """When the cached value is a JWT with exp, schedule refresh
    `refresh_before_expiry_seconds` before exp."""
    now = 1_000_000
    token_exp = now + 3600  # expires in 1h
    token = _make_jwt(exp_unix=token_exp)

    next_at = plan_refresh_schedule(
        value=token,
        ttl_seconds=None,
        refresh_before_expiry_seconds=300,
        now=now,
    )
    # 1h - 5min = 55min from now.
    assert next_at == now + 3600 - 300


def test_plan_refresh_schedule_falls_back_to_ttl_when_not_jwt():
    """Opaque tokens (API keys, PATs without exp) use the configured TTL."""
    now = 1_000_000
    next_at = plan_refresh_schedule(
        value="opaque-pat-xxx",
        ttl_seconds=7200,
        refresh_before_expiry_seconds=300,
        now=now,
    )
    assert next_at == now + 7200 - 300


def test_plan_refresh_schedule_returns_none_when_no_strategy_available():
    """Opaque token + no TTL = nothing to schedule. The refresher should
    skip this secret entirely rather than guess."""
    now = 1_000_000
    assert plan_refresh_schedule(
        value="opaque-pat",
        ttl_seconds=None,
        refresh_before_expiry_seconds=300,
        now=now,
    ) is None


def test_plan_refresh_schedule_clamps_past_due_to_now():
    """If the JWT exp is already in the past (or within the lead time),
    schedule immediately rather than negative."""
    now = 1_000_000
    expired_token = _make_jwt(exp_unix=now - 100)  # already expired

    next_at = plan_refresh_schedule(
        value=expired_token,
        ttl_seconds=None,
        refresh_before_expiry_seconds=300,
        now=now,
    )
    # Past-due → immediate (now), not negative.
    assert next_at == now


# ---- SecretRefresher loop ----------------------------------------------

class _FakeClock:
    """Deterministic clock + sleep that lets tests fast-forward time."""

    def __init__(self, start: float = 1_000_000.0):
        self._t = start
        self._cond = threading.Condition()

    def now(self) -> float:
        with self._cond:
            return self._t

    def sleep(self, seconds: float) -> None:
        # Wait until either the clock advances past our deadline or the
        # refresher gets stopped (cond notify).
        deadline = self._t + seconds
        with self._cond:
            while self._t < deadline:
                # Cap wait so a buggy test doesn't deadlock forever.
                self._cond.wait(timeout=2.0)
                if self._t < deadline:
                    # spurious wake or test still advancing — loop
                    pass

    def advance(self, seconds: float) -> None:
        with self._cond:
            self._t += seconds
            self._cond.notify_all()


def test_refresher_runs_refresh_command_and_pushes_value():
    """Happy path: the refresher waits until refresh time, runs the
    refresh_command, and calls set_secret with the new value."""
    clock = _FakeClock(start=1_000_000.0)
    pushes: list[tuple[str, str]] = []
    push_event = threading.Event()

    def fake_set_secret(name: str, *, value: str) -> None:
        pushes.append((name, value))
        push_event.set()

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
    )
    refresher.add_secret(
        name="AAD_TOKEN",
        refresh_command="echo refreshed-token-v2",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial-token",
    )
    refresher.start()
    try:
        # Advance to just past the scheduled refresh time. 3600 - 300 = 3300s.
        clock.advance(3301)
        assert push_event.wait(timeout=5.0), "refresher did not push"
        assert pushes == [("AAD_TOKEN", "refreshed-token-v2")]
    finally:
        refresher.stop()


def test_refresher_does_not_leak_stderr_in_warn_log_by_default(caplog, monkeypatch):
    """SECURITY: a refresh_command can write secret material to stderr
    (a partial token, an OAuth response body, a JWT echoed in a verbose
    error). The WARN log line that surfaces refresh failures must NOT
    include captured stderr by default — it lands in errors.log and the
    doctor surface, which are not where secret tails belong.

    Opt-in via HERMES_GONDOLIN_DEBUG_SECRETS=1 (verified by the next test).
    """
    monkeypatch.delenv("HERMES_GONDOLIN_DEBUG_SECRETS", raising=False)
    clock = _FakeClock(start=1_000_000.0)
    push_event = threading.Event()
    attempts = {"n": 0}
    leaked = "eyJhbGciOiJub25lIn0.PARTIAL-TOKEN-LEAK"

    def fake_set_secret(name, *, value):
        push_event.set()

    def fake_run_command(cmd):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return (1, "", leaked)
        return (0, "ok-token", "")

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="AAD_TOKEN",
        refresh_command="auth-script",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial",
    )
    refresher.start()
    try:
        import logging
        with caplog.at_level(logging.WARNING, logger="tools.environments.gondolin_secret_refresh"):
            clock.advance(3301)
            time.sleep(0.05)
            clock.advance(11)
            assert push_event.wait(timeout=5.0)
    finally:
        refresher.stop()

    warn_lines = [r.getMessage() for r in caplog.records if r.levelno >= 30]
    assert any("refresh failed" in line for line in warn_lines), warn_lines
    for line in warn_lines:
        assert leaked not in line, (
            f"stderr leaked into WARN log: {line!r}"
        )
        assert "stderr=" not in line, (
            f"stderr= key present without opt-in: {line!r}"
        )


def test_refresher_opts_into_stderr_capture_with_debug_env(caplog, monkeypatch):
    """When HERMES_GONDOLIN_DEBUG_SECRETS=1, the WARN log includes the
    stderr tail so operators debugging a broken refresh script can see what
    the helper actually emitted. The env var is host-side only; the daemon
    never propagates it to the guest."""
    monkeypatch.setenv("HERMES_GONDOLIN_DEBUG_SECRETS", "1")
    clock = _FakeClock(start=1_000_000.0)
    push_event = threading.Event()
    attempts = {"n": 0}

    def fake_set_secret(name, *, value):
        push_event.set()

    def fake_run_command(cmd):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return (1, "", "specific-debug-marker-XYZ")
        return (0, "ok-token", "")

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="AAD_TOKEN",
        refresh_command="auth-script",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial",
    )
    refresher.start()
    try:
        import logging
        with caplog.at_level(logging.WARNING, logger="tools.environments.gondolin_secret_refresh"):
            clock.advance(3301)
            time.sleep(0.05)
            clock.advance(11)
            assert push_event.wait(timeout=5.0)
    finally:
        refresher.stop()

    warn_lines = [r.getMessage() for r in caplog.records if r.levelno >= 30]
    assert any("specific-debug-marker-XYZ" in line for line in warn_lines), (
        f"opt-in stderr capture missing from WARN log: {warn_lines}"
    )


def test_refresher_handles_command_failure_with_warn_and_retry():
    """A failing refresh_command does NOT crash the loop and does NOT push
    a bad value. The error is logged WARN and the refresher retries with
    backoff."""
    clock = _FakeClock(start=1_000_000.0)
    pushes: list[tuple[str, str]] = []
    push_event = threading.Event()
    attempts = {"n": 0}

    def fake_set_secret(name: str, *, value: str) -> None:
        pushes.append((name, value))
        push_event.set()

    # First two refresh-command invocations fail, third succeeds.
    def fake_run_command(cmd: str) -> tuple[int, str, str]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return (1, "", "transient auth blip")
        return (0, "good-token", "")

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="AAD_TOKEN",
        refresh_command="auth-script",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial",
    )
    refresher.start()
    try:
        # First scheduled refresh attempt: t = 3300. Fails → backoff 10s.
        clock.advance(3301)
        # Give the worker thread time to run the failed attempt and
        # re-enter sleep before we advance again.
        time.sleep(0.05)
        clock.advance(11)
        time.sleep(0.05)
        clock.advance(31)
        assert push_event.wait(timeout=5.0), (
            f"refresher gave up after {attempts['n']} attempts; pushes={pushes}"
        )
        assert attempts["n"] == 3
        assert pushes == [("AAD_TOKEN", "good-token")]
    finally:
        refresher.stop()


def test_refresher_skips_secrets_with_no_refresh_strategy():
    """An opaque token (not a JWT) with no ttl_seconds has no refresh
    strategy — the refresher logs INFO and does NOT spawn work for it."""
    clock = _FakeClock(start=1_000_000.0)
    pushes: list[tuple[str, str]] = []

    def fake_set_secret(name: str, *, value: str) -> None:
        pushes.append((name, value))

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
    )
    refresher.add_secret(
        name="OPAQUE_API_KEY",
        refresh_command="echo new",
        ttl_seconds=None,  # nothing to schedule
        refresh_before_expiry_seconds=300,
        initial_value="opaque-value",
    )
    refresher.start()
    try:
        # Advance far past any reasonable refresh window.
        clock.advance(86400)
        time.sleep(0.05)  # let any racing thread schedule a push (it shouldn't)
        assert pushes == []
    finally:
        refresher.stop()


def test_refresher_stop_is_clean_and_idempotent():
    """stop() must terminate the background thread promptly and be safe
    to call multiple times (cleanup path is sometimes invoked twice)."""
    clock = _FakeClock(start=1_000_000.0)
    refresher = SecretRefresher(
        env_set_secret=lambda *a, **kw: None,
        time_source=clock.now,
        sleep_fn=clock.sleep,
    )
    refresher.add_secret(
        name="X",
        refresh_command="echo y",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="x",
    )
    refresher.start()
    refresher.stop()
    refresher.stop()  # idempotent
    # Thread should be gone.
    assert not refresher.is_running()


def test_refresher_supports_multiple_secrets():
    """Multiple secrets with different schedules all get refreshed."""
    clock = _FakeClock(start=1_000_000.0)
    pushes: list[tuple[str, str]] = []
    both_pushed = threading.Event()
    lock = threading.Lock()

    def fake_set_secret(name: str, *, value: str) -> None:
        with lock:
            pushes.append((name, value))
            if len({n for n, _ in pushes}) == 2:
                both_pushed.set()

    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
    )
    refresher.add_secret(
        name="FAST",
        refresh_command="echo fast-v2",
        ttl_seconds=600,
        refresh_before_expiry_seconds=60,
        initial_value="fast-v1",
    )
    refresher.add_secret(
        name="SLOW",
        refresh_command="echo slow-v2",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="slow-v1",
    )
    refresher.start()
    try:
        # FAST refresh at 540s; SLOW refresh at 3300s.
        clock.advance(541)
        time.sleep(0.05)
        clock.advance(2760)
        assert both_pushed.wait(timeout=5.0), f"got only {pushes}"
        names_pushed = {n for n, _ in pushes}
        assert names_pushed == {"FAST", "SLOW"}
    finally:
        refresher.stop()
