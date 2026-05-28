"""Background loop that periodically re-runs a secret's ``refresh_command``
and pushes the new value into a live Gondolin VM via ``env.set_secret``.

The wire-injection layer (Gondolin ``secretManager.updateSecret``) is the
plumbing that makes the new value take effect on the next outbound HTTPS
request without restarting the VM. This module owns the *schedule*: when
to fetch, when to retry, when to give up.

Lives in its own module (not glued into ``GondolinEnvironment``) so the
loop's logic — schedule planning, JWT expiry parsing, retry — can be
unit-tested without spawning a daemon. ``GondolinEnvironment`` constructs
a ``SecretRefresher``, hands it ``self.set_secret`` plus per-secret
configs, and stops it on cleanup.

Two strategies for picking the next refresh time:

  - JWT exp claim (preferred): if the current value looks like a JWT, we
    base64-decode the payload and read ``exp``. Refresh fires
    ``refresh_before_expiry_seconds`` (default 300) before that.
  - Fixed TTL (fallback): for opaque tokens (PATs, API keys with a
    documented lifetime), the user sets ``ttl_seconds`` and we schedule
    accordingly.

If neither applies (opaque value, no TTL) we skip that secret — refresh
is impossible to schedule sensibly without info. The init-time WARN
diagnostic is the user's signal that refresh isn't configured.

Failure handling: a single refresh failure does NOT crash the loop and
does NOT push the bad value. The next attempt uses backoff
(10s → 30s → 60s, capped) until success or the next regularly-scheduled
refresh, whichever comes first. Every failure is logged at WARN with the
captured stderr tail so unattended cron jobs surface the problem in
``errors.log``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# Backoff schedule (seconds) on transient refresh failures. Caps at 60s
# so a sustained outage doesn't go silent — once we hit the cap, every
# subsequent attempt still fires every 60s with a WARN per attempt.
_RETRY_BACKOFF = (10, 30, 60)


def parse_jwt_exp(token: str) -> int | None:
    """Return the JWT ``exp`` claim (unix epoch seconds) if ``token`` looks
    like a JWT with a parseable payload, else None.

    No signature verification — we're trusting our own auth toolchain.
    The point is scheduling, not authentication.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    # JWT uses base64url without padding; add it back so urlsafe_b64decode works.
    pad = "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return int(exp)


def plan_refresh_schedule(
    *,
    value: str | None,
    ttl_seconds: int | None,
    refresh_before_expiry_seconds: int,
    now: float,
) -> float | None:
    """Pick the next refresh time (unix epoch seconds) for one secret.

    Returns None when no strategy applies — caller should skip the secret.
    Clamps past-due schedules to ``now`` so an already-expired token
    triggers immediate refresh instead of negative sleep.
    """
    # JWT exp wins when available — accurate to the token's real lifetime.
    if isinstance(value, str):
        exp = parse_jwt_exp(value)
        if exp is not None:
            return max(float(now), float(exp - refresh_before_expiry_seconds))

    # Fallback: configured TTL relative to "now" (treated as when the
    # current value was issued).
    if ttl_seconds is not None and ttl_seconds > 0:
        return max(float(now), float(now + ttl_seconds - refresh_before_expiry_seconds))

    return None


@dataclass
class _SecretState:
    name: str
    refresh_command: str
    ttl_seconds: int | None
    refresh_before_expiry_seconds: int
    current_value: str | None
    next_refresh_at: float | None = None
    retry_index: int = 0  # next backoff slot when retrying after failure
    # Marker for "no strategy available" — the loop reads this once and
    # never schedules another refresh for the secret.
    skip: bool = False
    # Pre-built explicit env for ``refresh_command`` invocations. None
    # means "use the safe baseline alone" — the common case. Set by
    # add_secret() from the per-secret ``env:`` config.
    env: dict[str, str] | None = None
    # Per-secret subprocess timeout in seconds. None → 30s default in
    # _default_run_command. Driven by the per-secret ``timeout_ms``
    # config (ms, converted to seconds here for subprocess.run).
    timeout: float | None = None


def _interpolate_env_vars(value: str) -> str:
    """Expand ``${VAR}`` references against ``os.environ``.

    Matches ``tools/mcp_tool.py:_interpolate_env_vars`` (and
    ``hooks.mjs:_interpolateEnvVars``) so the per-secret ``env:`` knob
    behaves the same way across MCP servers and gondolin secrets.
    Unset names expand to the empty string (same as MCP).
    """
    return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


# Environment variables that are safe to inherit from the host into a
# refresh_command subprocess. Mirrors
# ``tools/mcp_tool.py:_SAFE_ENV_KEYS`` and
# ``tools/environments/gondolin_host/src/hooks.mjs:_SAFE_ENV_KEYS`` —
# any addition here must land in those files too. Hermes-wide
# convention: config-driven subprocesses (MCP servers, docker
# forward_env, gondolin secrets) get a baseline POSIX env and
# anything else is explicit opt-in. Prevents a malicious config from
# exfiltrating credentials held in the Hermes process env.
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _build_safe_env(user_env: dict | None) -> dict[str, str]:
    """Build the env dict that gets passed to ``subprocess.run`` for a
    refresh_command.

    Starts from the safe POSIX baseline (PATH, HOME, USER, …, XDG_*) and
    merges any caller-specified ``env:`` dict on top, with ``${VAR}``
    interpolation against ``os.environ``. Non-string values are dropped
    with a WARN so a typo'd YAML number doesn't silently disappear.

    Returned dict is the explicit env for the subprocess — no inheritance.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        for key, value in user_env.items():
            if not isinstance(value, str):
                logger.warning(
                    "gondolin secret env: ignoring non-string value for %s (got %s)",
                    key, type(value).__name__,
                )
                continue
            env[key] = _interpolate_env_vars(value)
    return env


def _default_run_command(
    cmd: str, env: dict | None = None, timeout: float = 30.0
) -> tuple[int, str, str]:
    """Run ``cmd`` via shell, capture stdout/stderr, return (rc, stdout, stderr).

    Trim whitespace on stdout (a leading/trailing newline from ``echo`` is
    not meaningful). Stderr is preserved as-is for diagnostic display.

    SECURITY (env isolation): ``cmd`` runs under ``subprocess.run(...,
    shell=True)`` but with an EXPLICIT env built from the safe baseline
    (PATH, HOME, USER, …, XDG_*) plus any ``env:`` dict the user opted
    into per secret. The Hermes process env is NOT inherited, so a
    refresh_command cannot exfiltrate the host's API keys / tokens by
    writing ``env | curl ...``. This matches MCP's filtering
    (``tools/mcp_tool.py:_build_safe_env``) and docker_forward_env's
    explicit-opt-in shape. The ``shell=True`` is still required so users
    can write ``op signin && op read 'op://...'`` style chains, but the
    surface that "shell=True is dangerous" usually defends against —
    leaking host credentials — no longer applies.

    A shell-injection bug in a user-authored ``refresh_command`` can
    still execute arbitrary code at host scope under that filtered env,
    same as a misbehaving MCP server. See
    docs/design/gondolin-terminal-backend.md "Trust model".
    """
    proc = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env if env is not None else _build_safe_env(None),
    )
    return (proc.returncode, (proc.stdout or "").strip(), proc.stderr or "")


class SecretRefresher:
    """Background loop that calls ``env.set_secret`` when refresh is due.

    Lifecycle:
        r = SecretRefresher(env_set_secret=env.set_secret)
        r.add_secret(name=..., refresh_command=..., ttl_seconds=..., ...)
        r.start()   # spawns one daemon thread
        ...
        r.stop()    # idempotent; waits up to 2s for the thread to exit

    The single background thread iterates over registered secrets, picks
    the soonest next_refresh_at, sleeps until then, runs the command,
    pushes the value, and reschedules. One thread (not one-per-secret) so
    even a fleet of 10+ secrets per VM stays cheap.
    """

    def __init__(
        self,
        *,
        env_set_secret: Callable[..., None],
        time_source: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        run_command: Callable[[str, dict | None, float], tuple[int, str, str]] | None = None,
    ):
        self._env_set_secret = env_set_secret
        self._now = time_source or time.time
        self._run_command = run_command or _default_run_command
        self._secrets: dict[str, _SecretState] = {}
        self._stop_event = threading.Event()
        # Default sleep is the stop event's wait(): returns immediately
        # when stop() sets the event, so cleanup doesn't have to outwait
        # a 50-minute "until next refresh" interval. Plain time.sleep
        # would be uninterruptible and leak the thread past stop()'s
        # join timeout. Tests inject their own sleep_fn (a fake clock)
        # and don't care which default we'd otherwise pick.
        self._sleep = sleep_fn or self._stop_event.wait
        self._thread: threading.Thread | None = None
        # Tracks the most-recently-spawned thread independently of
        # self._thread, so start()'s liveness check can detect a stop()
        # that timed out and refuse to spawn a duplicate worker.
        self._prev_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def add_secret(
        self,
        *,
        name: str,
        refresh_command: str,
        ttl_seconds: int | None,
        refresh_before_expiry_seconds: int,
        initial_value: str | None,
        env: dict | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        """Register a secret for refresh. Safe to call before ``start()``.

        ``env`` is the per-secret ``env:`` dict from config. It is
        resolved against the safe POSIX baseline + ${VAR} interpolation
        once at registration; the resolved dict is what every refresh
        tick passes to the subprocess. Pass None (or omit) for the
        common case of "the refresh command needs no extra env beyond
        PATH/HOME/etc.".

        ``timeout_ms`` is the per-secret subprocess timeout in
        milliseconds. None → 30s default. Lets a slow refresh chain
        (``op signin && op read ...``) opt out of the default cap.
        """
        resolved_env = _build_safe_env(env) if env is not None else None
        timeout_s = (timeout_ms / 1000.0) if timeout_ms is not None else None
        with self._lock:
            state = _SecretState(
                name=name,
                refresh_command=refresh_command,
                ttl_seconds=ttl_seconds,
                refresh_before_expiry_seconds=refresh_before_expiry_seconds,
                current_value=initial_value,
                env=resolved_env,
                timeout=timeout_s,
            )
            state.next_refresh_at = plan_refresh_schedule(
                value=initial_value,
                ttl_seconds=ttl_seconds,
                refresh_before_expiry_seconds=refresh_before_expiry_seconds,
                now=self._now(),
            )
            if state.next_refresh_at is None:
                state.skip = True
                logger.info(
                    "gondolin secret %s: no refresh strategy "
                    "(not a JWT and no ttl_seconds) — refresh disabled",
                    name,
                )
            self._secrets[name] = state

    def start(self) -> None:
        # Refuse to spawn while a previous worker is still alive. stop()
        # nulls self._thread *before* join (so concurrent is_running()
        # returns False once stop() has been called), but the old thread
        # may not have actually exited yet — most commonly because its
        # in-flight refresh subprocess hasn't returned. Spawning a second
        # worker would race with it on the secret table; clearing
        # stop_event would also un-cancel the old worker. self._prev_thread
        # is tracked independently of self._thread so stop()'s bookkeeping
        # can't fool the liveness check.
        if self._prev_thread is not None and self._prev_thread.is_alive():
            logger.warning(
                "gondolin secret refresher: start() called while a previous "
                "worker is still running (stop() join timed out). Refusing "
                "to spawn a duplicate worker."
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="gondolin-secret-refresh", daemon=True
        )
        self._thread.start()
        # Track this thread so the next stop()/start() pair can see it.
        self._prev_thread = self._thread

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        try:
            thread.join(timeout=timeout)
        except RuntimeError:
            pass
        # Clear _prev_thread only if the join actually succeeded —
        # otherwise leave it set so the next start() can detect the
        # still-live previous worker and refuse to spawn a duplicate.
        if not thread.is_alive():
            self._prev_thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                due_states = [
                    s for s in self._secrets.values()
                    if not s.skip and s.next_refresh_at is not None
                ]
            if not due_states:
                # Nothing to do (all secrets opted out). Sleep briefly so a
                # late add_secret() can wake us; the thread is daemon=True
                # so it won't block process exit either way.
                self._sleep(1.0)
                continue

            now = self._now()
            # Pick the soonest next-refresh.
            soonest = min(due_states, key=lambda s: s.next_refresh_at or 0.0)
            wait = max(0.0, (soonest.next_refresh_at or 0.0) - now)
            if wait > 0:
                self._sleep(wait)
                if self._stop_event.is_set():
                    break

            self._refresh_one(soonest)

    def _refresh_one(self, state: _SecretState) -> None:
        try:
            rc, stdout, stderr = self._run_command(
                state.refresh_command,
                state.env,
                state.timeout if state.timeout is not None else 30.0,
            )
        except subprocess.TimeoutExpired:
            self._schedule_retry(state, "refresh command timed out", stderr="")
            return
        except Exception as exc:  # noqa: BLE001 — defensive: never crash the loop
            self._schedule_retry(state, f"refresh command raised: {exc}", stderr="")
            return

        if rc != 0 or not stdout:
            reason = (
                f"refresh command exited {rc}"
                if rc != 0 else
                "refresh command exited 0 but produced empty output"
            )
            self._schedule_retry(state, reason, stderr=stderr)
            return

        # Push the new value through the wire-injection layer.
        try:
            self._env_set_secret(state.name, value=stdout)
        except Exception as exc:  # noqa: BLE001
            # set_secret failure: log + retry. Don't update current_value
            # because we don't know whether it was persisted.
            self._schedule_retry(state, f"set_secret failed: {exc}", stderr="")
            return

        # Success: reschedule based on the new value's JWT exp / TTL.
        with self._lock:
            state.current_value = stdout
            state.retry_index = 0
            state.next_refresh_at = plan_refresh_schedule(
                value=stdout,
                ttl_seconds=state.ttl_seconds,
                refresh_before_expiry_seconds=state.refresh_before_expiry_seconds,
                now=self._now(),
            )
            if state.next_refresh_at is None:
                state.skip = True
        logger.info("gondolin secret %s refreshed; next at %s", state.name, state.next_refresh_at)

    def _schedule_retry(self, state: _SecretState, reason: str, *, stderr: str) -> None:
        # SECURITY: captured stderr from a failing refresh_command can
        # contain partial secrets — a token half-written before the helper
        # crashed, a JWT echoed in a verbose error, the raw response body
        # from a misconfigured auth endpoint. The WARN log line flows to
        # errors.log + `hermes doctor` + `gondolin_log_scan`; neither is
        # the right place for secret tails. Default behavior omits stderr.
        # Set HERMES_GONDOLIN_DEBUG_SECRETS=1 to opt in when debugging a
        # broken refresh script.
        debug_capture = os.environ.get("HERMES_GONDOLIN_DEBUG_SECRETS") == "1"
        stderr_tail = stderr.strip()[-500:] if (debug_capture and stderr) else ""
        extra = f" stderr={stderr_tail!r}" if stderr_tail else ""
        logger.warning(
            "gondolin secret %s refresh failed: %s%s — retrying",
            state.name, reason, extra,
        )
        with self._lock:
            idx = min(state.retry_index, len(_RETRY_BACKOFF) - 1)
            backoff = _RETRY_BACKOFF[idx]
            state.retry_index += 1
            state.next_refresh_at = self._now() + backoff
