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
import logging
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

    def fake_run_command(cmd, env=None, timeout=30.0):
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

    def fake_run_command(cmd, env=None, timeout=30.0):
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
    def fake_run_command(cmd: str, env: dict | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
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


def test_refresher_stop_does_not_resurrect_old_worker_on_restart():
    """B12: stop() must not return until the old worker thread is gone
    (or be honest that it hasn't), and start() must not silently spawn
    a duplicate worker against a still-live previous thread.

    Bug pre-B12: stop() set ``self._thread = None`` *before* join, so
    if join timed out (worker mid-subprocess, slow user sleep_fn), the
    old worker kept running. A subsequent start() saw self._thread is
    None, cleared stop_event (un-cancelling the old worker), and
    launched a SECOND thread. Two workers raced against the same
    secret table.

    Reproduction: a slow runner blocks past stop()'s 0.1s join; then
    start() is called. With the bug both threads were live; with the
    fix start() refuses to spawn while the old worker is still alive,
    so we only ever observe one live worker thread.
    """
    runner_in_call = threading.Event()
    runner_can_return = threading.Event()

    def slow_runner(cmd, env=None, timeout=None):
        runner_in_call.set()
        # Block here so stop()'s short join times out — simulates a
        # subprocess that's still running when cleanup fires.
        runner_can_return.wait(timeout=10.0)
        return (0, "v\n", "")

    refresher = SecretRefresher(
        env_set_secret=lambda *a, **kw: None,
        run_command=slow_runner,
        # Fast sleep so the worker reaches the runner immediately.
        sleep_fn=lambda s: time.sleep(min(s, 0.01)),
    )
    refresher.add_secret(
        name="TOK",
        refresh_command="echo y",
        ttl_seconds=1,
        refresh_before_expiry_seconds=0,
        initial_value="x",
    )
    refresher.start()
    t1 = refresher._thread
    assert t1 is not None
    # Wait until the worker is actually mid-runner so stop's join can't
    # complete in the 0.1s budget below.
    assert runner_in_call.wait(timeout=2.0), "worker never reached runner"

    # stop() with a short timeout — join times out, old worker still live.
    refresher.stop(timeout=0.1)
    assert t1.is_alive(), "test precondition: old worker should still be running"

    # The bug: start() here would spawn a SECOND thread + clear
    # stop_event, un-cancelling the first. Either no second thread is
    # created (start refuses), or stop_event stays set so the first
    # exits as soon as the runner unblocks. We must NOT end up with two
    # live workers racing.
    refresher.start()
    t2 = refresher._thread
    # Let the old worker unblock and finish (so we don't hang at teardown).
    runner_can_return.set()

    # The contract: never have two distinct live workers at the same time.
    # Acceptable shapes:
    #   (a) start() no-ops while old worker is alive (t2 is t1 OR t2 is None)
    #   (b) start() defers — t2 spawned only after old worker is gone
    if t2 is not None and t2 is not t1:
        # Brief moment to let the old worker drain.
        t1.join(timeout=2.0)
        assert not t1.is_alive(), (
            "BUG: stop()+start() left two live workers racing on the "
            "same secrets table. start() should have refused to spawn "
            "until the old worker exited."
        )

    refresher.stop(timeout=5.0)


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


def test_refresher_default_sleep_is_interruptible_by_stop():
    """Regression for G7: with no sleep_fn injected (production path), the
    refresher must default to an interruptible sleep so stop() wakes the
    thread immediately instead of waiting for the full refresh interval.

    Setup: TTL of one hour and refresh_before_expiry_seconds=300 schedules
    the next refresh ~3300s out. Before the fix, the thread sat in
    time.sleep(3300) and ignored stop() until that interval elapsed (or
    the process exited and tore the daemon thread down ungracefully).
    With the fix, stop() returns and is_running() flips to False well
    inside join's 2s timeout.
    """
    refresher = SecretRefresher(
        env_set_secret=lambda *a, **kw: None,
        # No time_source, no sleep_fn — exercise the production defaults.
    )
    refresher.add_secret(
        name="LONG",
        refresh_command="echo y",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="x",
    )
    refresher.start()
    # Give the loop a tick to enter its sleep.
    time.sleep(0.05)
    t0 = time.monotonic()
    refresher.stop(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert not refresher.is_running(), "thread did not exit after stop()"
    # 0.5s is generous; pre-fix this would have been ~3300s (or never).
    assert elapsed < 0.5, f"stop() took {elapsed:.2f}s — sleep not interruptible"


# ---- env isolation (G2-Python: refresh_command does not inherit host env) ----

def test_build_safe_env_filters_arbitrary_host_vars(monkeypatch):
    """The Hermes process env (API keys, tokens) must NOT leak into a
    refresh_command subprocess. Only the safe POSIX baseline passes
    through. Mirrors the JS-side regression in hooks.test.mjs."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("HERMES_TEST_NEVER_LEAK", "secret-token-do-not-leak")

    env = _build_safe_env(None)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"
    assert "HERMES_TEST_NEVER_LEAK" not in env


def test_build_safe_env_xdg_prefix_passes_through(monkeypatch):
    """XDG_* variables are part of the POSIX baseline (XDG Base
    Directory spec) and pass through alongside PATH/HOME/etc."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = _build_safe_env(None)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_build_safe_env_merges_user_env(monkeypatch):
    """Per-secret env: dict from config merges on top of the baseline.
    Literal values pass through verbatim."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    monkeypatch.setenv("PATH", "/usr/bin")
    env = _build_safe_env({"MY_OPT_IN": "value-from-config"})
    assert env["MY_OPT_IN"] == "value-from-config"
    assert env["PATH"] == "/usr/bin"  # baseline still present


def test_build_safe_env_interpolates_dollar_var(monkeypatch):
    """${VAR} references in the per-secret env: dict resolve against
    the Hermes process env. Same syntax as MCP server configs."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    monkeypatch.setenv("HERMES_TEST_SRC", "interp-resolved")
    env = _build_safe_env({"DERIVED": "${HERMES_TEST_SRC}"})
    assert env["DERIVED"] == "interp-resolved"


def test_build_safe_env_unset_var_expands_to_empty(monkeypatch):
    """Matches MCP semantics: ${UNSET} → "" rather than raising or
    leaving the literal ${UNSET} in place. Lets users write defensive
    configs without env_unset-style guards."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    monkeypatch.delenv("HERMES_TEST_DEFINITELY_UNSET", raising=False)
    env = _build_safe_env({"MAYBE": "${HERMES_TEST_DEFINITELY_UNSET}"})
    assert env["MAYBE"] == ""


def test_build_safe_env_drops_non_string_values(caplog):
    """A typo'd YAML number (e.g. `env: { KEY: 42 }`) is dropped with a
    WARN log so the misconfig is visible without crashing the daemon."""
    from tools.environments.gondolin_secret_refresh import _build_safe_env

    with caplog.at_level(logging.WARNING, logger="tools.environments.gondolin_secret_refresh"):
        env = _build_safe_env({"BAD": 42, "GOOD": "ok"})
    assert "BAD" not in env
    assert env["GOOD"] == "ok"
    assert any("ignoring non-string" in r.message for r in caplog.records)


def test_default_run_command_does_not_inherit_arbitrary_host_env(monkeypatch):
    """End-to-end regression: a refresh_command that asks for an env var
    NOT in the safe baseline and NOT opted in via env: should see it as
    unset. This is the malicious-config exfiltration shape from G2."""
    from tools.environments.gondolin_secret_refresh import _default_run_command

    monkeypatch.setenv("HERMES_TEST_LEAK_TARGET", "MUST_NOT_LEAK")
    # No env= passed to the subprocess explicitly — should default to
    # the safe baseline (PATH/HOME/etc.) only.
    rc, stdout, _ = _default_run_command(
        'echo "${HERMES_TEST_LEAK_TARGET:-NOT_SET}"',
    )
    assert rc == 0
    assert stdout == "NOT_SET", (
        f"refresh_command saw the host env var — env isolation broken: {stdout!r}"
    )


def test_default_run_command_sees_explicitly_opted_in_env(monkeypatch):
    """The escape hatch: when the per-secret env: dict opts a var in,
    the refresh subprocess sees it."""
    from tools.environments.gondolin_secret_refresh import (
        _build_safe_env,
        _default_run_command,
    )

    monkeypatch.setenv("HERMES_TEST_OPT_IN_SRC", "OPT_IN_VALUE")
    explicit_env = _build_safe_env({"HERMES_TEST_OPT_IN_SRC": "${HERMES_TEST_OPT_IN_SRC}"})
    rc, stdout, _ = _default_run_command(
        'echo "${HERMES_TEST_OPT_IN_SRC:-MISSING}"',
        env=explicit_env,
    )
    assert rc == 0
    assert stdout == "OPT_IN_VALUE"


def test_refresher_threads_per_secret_env_through_to_subprocess(monkeypatch):
    """End-to-end: a SecretRefresher started with a per-secret env=
    dict must pass the resolved env to its _run_command callback every
    refresh tick. Pins the contract that the env: YAML key actually
    reaches the subprocess."""
    from tools.environments.gondolin_secret_refresh import SecretRefresher

    monkeypatch.setenv("HERMES_TEST_OPT_IN_SRC", "opt-in-value-789")

    captured_envs: list[dict | None] = []
    push_event = threading.Event()

    def fake_set_secret(name, *, value):
        push_event.set()

    def fake_run_command(cmd, env=None, timeout=30.0):
        captured_envs.append(env)
        return (0, "refreshed-token", "")

    clock = _FakeClock(start=1_000_000.0)
    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="OP_TOKEN",
        refresh_command="op read 'op://Personal/Token/credential'",
        ttl_seconds=600,
        refresh_before_expiry_seconds=60,
        initial_value="initial-v1",
        env={"OP_SERVICE_ACCOUNT_TOKEN": "${HERMES_TEST_OPT_IN_SRC}"},
    )
    refresher.start()
    try:
        # First refresh at 540s (ttl - refresh_before).
        clock.advance(541)
        assert push_event.wait(timeout=5.0)
        assert captured_envs, "fake_run_command was never invoked"
        seen = captured_envs[0]
        assert seen is not None, "per-secret env: should produce a non-None subprocess env"
        # Interpolation happened.
        assert seen.get("OP_SERVICE_ACCOUNT_TOKEN") == "opt-in-value-789"
        # Baseline still present.
        assert "PATH" in seen
        # Arbitrary host vars still filtered.
        assert "HERMES_TEST_OPT_IN_SRC" not in seen, (
            "raw source var leaked instead of going through ${} interpolation"
        )
    finally:
        refresher.stop()


def test_refresher_no_env_means_safe_baseline_only(monkeypatch):
    """When add_secret() is called without env=, the subprocess sees the
    safe baseline only — env is None on the state object and
    _default_run_command falls back to _build_safe_env(None) at call
    time. (Existing tests pass run_command= so they don't exercise this
    contract; pin it explicitly.)"""
    from tools.environments.gondolin_secret_refresh import SecretRefresher

    captured_envs: list[dict | None] = []
    push_event = threading.Event()

    def fake_set_secret(name, *, value):
        push_event.set()

    def fake_run_command(cmd, env=None, timeout=30.0):
        captured_envs.append(env)
        return (0, "refreshed-token", "")

    clock = _FakeClock(start=1_000_000.0)
    refresher = SecretRefresher(
        env_set_secret=fake_set_secret,
        time_source=clock.now,
        sleep_fn=clock.sleep,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="X",
        refresh_command="echo y",
        ttl_seconds=600,
        refresh_before_expiry_seconds=60,
        initial_value="x",
        # no env=
    )
    refresher.start()
    try:
        clock.advance(541)
        assert push_event.wait(timeout=5.0)
        assert captured_envs == [None], (
            "secret added without env= should produce env=None at the run "
            f"boundary (so _default_run_command builds the safe baseline); "
            f"got {captured_envs!r}"
        )
    finally:
        refresher.stop()


def test_refresher_threads_timeout_ms_through_to_subprocess(monkeypatch):
    """B8: timeout_ms on a per-secret config flows from add_secret() down
    to the run_command call. Pre-B8 the refresher hardcoded 30s and
    silently ignored the user's timeout_ms — surfacing it via gondolin
    config did nothing in the background loop."""
    monkeypatch.delenv("HERMES_GONDOLIN_DEBUG_SECRETS", raising=False)
    captured_timeouts = []

    def fake_run_command(cmd, env=None, timeout=30.0):
        captured_timeouts.append(timeout)
        return (0, "new-token", "")

    refresher = SecretRefresher(
        env_set_secret=lambda *a, **k: None,
        time_source=lambda: 1_700_000_000.0,
        sleep_fn=lambda s: None,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="SLOW",
        refresh_command="op signin && op read 'op://x'",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial",
        timeout_ms=60_000,  # 60s for a slow chain
    )
    state = refresher._secrets["SLOW"]
    refresher._refresh_one(state)
    assert captured_timeouts == [60.0], (
        f"timeout_ms=60000 should arrive at run_command as 60.0s; "
        f"got {captured_timeouts!r}"
    )


def test_refresher_uses_default_timeout_when_unset(monkeypatch):
    """Without timeout_ms the refresher falls back to the 30s default,
    same as before B8."""
    monkeypatch.delenv("HERMES_GONDOLIN_DEBUG_SECRETS", raising=False)
    captured_timeouts = []

    def fake_run_command(cmd, env=None, timeout=30.0):
        captured_timeouts.append(timeout)
        return (0, "tok", "")

    refresher = SecretRefresher(
        env_set_secret=lambda *a, **k: None,
        time_source=lambda: 1_700_000_000.0,
        sleep_fn=lambda s: None,
        run_command=fake_run_command,
    )
    refresher.add_secret(
        name="X",
        refresh_command="echo tok",
        ttl_seconds=3600,
        refresh_before_expiry_seconds=300,
        initial_value="initial",
    )
    refresher._refresh_one(refresher._secrets["X"])
    assert captured_timeouts == [30.0]


def test_default_run_command_honors_per_call_timeout():
    """The timeout kwarg on _default_run_command actually reaches
    subprocess.run. A sleep 2 with timeout=0.3 raises TimeoutExpired
    instead of waiting the full 2 seconds."""
    import subprocess as _subprocess
    import time as _time
    from tools.environments.gondolin_secret_refresh import _default_run_command

    t0 = _time.monotonic()
    with pytest.raises(_subprocess.TimeoutExpired):
        _default_run_command("sleep 2", env=None, timeout=0.3)
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.5, (
        f"timeout=0.3s should raise quickly; took {elapsed:.2f}s — likely "
        f"the param was ignored and the hardcoded 30s was used."
    )
