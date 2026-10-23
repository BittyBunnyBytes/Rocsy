from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from threading import Lock
from pathlib import Path

from typing import Callable

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import ConfigError, settings

DATA_DIR = Path(__file__).resolve().parent / "data"


def _private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _session_key() -> str:
    """Half of the cookie-signing secret that never lives in .env: a leaked COOKIE_SECRET
    alone can't forge a session."""
    path = DATA_DIR / "session.key"
    try:
        key = path.read_text().strip()
    except FileNotFoundError:
        key = ""
    if not key:
        key = secrets.token_urlsafe(48)
        _private_file(path, key + "\n")
    return key


_serializer = URLSafeTimedSerializer(settings.cookie_secret + _session_key(), salt="bb_session")

COOKIE_MAX_AGE_SECONDS = settings.cookie_max_age_seconds
PASSWORD_FILE = DATA_DIR / "password.json"
_password_lock = Lock()


def _load_password_record() -> dict | None:
    try:
        record = json.loads(PASSWORD_FILE.read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or record.get("version") != 1
            or not all(isinstance(record.get(key), str) for key in ("salt", "hash", "revision"))):
        raise RuntimeError("Invalid saved Rocsy password record")
    return record


def _new_record(password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"version": 1, "salt": salt.hex(),
            "hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000).hex(),
            "revision": secrets.token_hex(16)}


def _save_record(record: dict) -> None:
    _private_file(PASSWORD_FILE, json.dumps(record) + "\n")


_password_record = _load_password_record()
if _password_record is None and not settings.no_password:
    # First start (or an install from before password.json): MASTER_PASSWORD in .env is
    # only a seed. Hash it here so the login never depends on the plaintext .env again.
    if not settings.master_password:
        raise ConfigError("MASTER_PASSWORD is required in BunnyBytes/.env until the gateway has saved "
                          "its hashed password (gateway/data/password.json).")
    _password_record = _new_record(settings.master_password)
    _save_record(_password_record)


def has_password() -> bool:
    return _password_record is not None or not settings.no_password


# Set by app.py to the Connections tracker's check, so one browser's session
# can be revoked without rotating the shared password.
is_session_revoked: Callable[[str], bool] = lambda sid: False


def _password_revision() -> str:
    if _password_record is not None:
        return _password_record["revision"]
    return "legacy"


def password_revision() -> str:
    return _password_revision()


def make_session_cookie(sid: str | None = None) -> str:
    """A minimal signed payload -- there's only one shared password, so no
    username is needed, just an issued-at marker the serializer already embeds
    in its signature. The payload itself carries a redundant marker for
    readability/debugging only; the itsdangerous timestamp is what's trusted."""
    return _serializer.dumps({"iat": int(time.time()), "revision": _password_revision(),
                              "sid": sid or secrets.token_urlsafe(12)})


def session_payload(value: str | None) -> dict | None:
    """The cookie's payload if it is a live session (signed, unexpired, current
    password revision, not revoked), else None."""
    if not value:
        return None
    try:
        payload = _serializer.loads(value, max_age=COOKIE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or payload.get("revision", "legacy") != _password_revision():
        return None
    sid = payload.get("sid")
    if isinstance(sid, str) and is_session_revoked(sid):
        return None
    return payload


def verify_session_cookie(value: str | None) -> bool:
    if not has_password():
        return True
    return session_payload(value) is not None


def check_password(candidate: str) -> bool:
    # hmac.compare_digest, never `==` -- avoids leaking password length/prefix
    # via timing side channels.
    if _password_record is not None:
        try:
            actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"),
                                         bytes.fromhex(_password_record["salt"]), 600_000)
            return hmac.compare_digest(actual, bytes.fromhex(_password_record["hash"]))
        except ValueError:
            return False
    return settings.no_password and candidate == ""


def set_password(current: str, new: str) -> bool:
    """Save a password change; return False if the current password is wrong."""
    global _password_record
    with _password_lock:
        if has_password() and not check_password(current):
            return False
        record = _new_record(new)
        _save_record(record)
        _password_record = record
        return True


def check_llama_api_key(candidate: str) -> bool:
    """Bearer-token check for /Llama/* -- same timing-safe comparison as the
    site password, but against a separate credential (LLAMA_API_KEY) since
    headless callers (Hermes agents) authenticate per-request, not via the
    browser cookie-login flow."""
    return hmac.compare_digest(candidate.encode("utf-8"), settings.llama_api_key.encode("utf-8"))


# --- Per-IP login rate limiter -------------------------------------------------
#
# Chosen numbers:
#   - MAX_ATTEMPTS = 5 failed attempts
#   - WINDOW_SECONDS = 300 (5 minutes) -- the rolling window in which those
#     attempts are counted
#   - BASE_LOCKOUT_SECONDS = 300 (5 minutes) -- the 5th failure in a window
#     locks that IP out for 5 minutes the first time.
#   - Repeat offenders escalate: each subsequent lockout (while the IP hasn't
#     gone OFFENSE_DECAY_SECONDS without a new lockout) doubles the previous
#     lockout, capped at MAX_LOCKOUT_SECONDS -- a bot that keeps coming back
#     after each lockout expires gets throttled progressively harder instead
#     of the same flat 5 minutes forever. A full day of good behavior resets
#     the escalation, so a shared/dynamic IP a legitimate user later reuses
#     isn't permanently penalized for someone else's earlier attempts.
#   - Within the 5-attempt allowance before a hard lockout even triggers, each
#     failure also gets a small added response delay (PER_ATTEMPT_DELAY_SECONDS
#     * attempt count, capped at MAX_DELAY_SECONDS) -- slows down fast
#     scripted guessing well before the lockout threshold is reached, since a
#     bot that never crosses MAX_ATTEMPTS in a single window would otherwise
#     never be slowed at all.
# State is an in-memory dict keyed by client IP. This is intentionally not
# persisted or shared across processes/restarts: the gateway runs as a single
# uvicorn process (no multi-worker fan-out), so an in-memory dict is a
# consistent single source of truth. A restart clears lockouts, which is an
# acceptable tradeoff for a self-hosted single-password gate.

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
BASE_LOCKOUT_SECONDS = 300
MAX_LOCKOUT_SECONDS = 6 * 3600
OFFENSE_DECAY_SECONDS = 24 * 3600
PER_ATTEMPT_DELAY_SECONDS = 0.6
MAX_DELAY_SECONDS = 4.0

# Global (all-IPs-combined) slowdown window: the per-IP lockout above is a
# sliding window keyed on MAX_ATTEMPTS within WINDOW_SECONDS, which a patient
# attacker can evade indefinitely either by pacing just under the threshold
# (e.g. 4 failures every 5 minutes, forever, never triggering a lockout) or
# by rotating source IPs (each gets its own fresh counter). This site has
# exactly one legitimate user, so there is no legitimate reason for a burst
# of failed logins across many different IPs in a short window -- treat that
# as a signal independent of any single IP's count and add extra delay for
# everyone while it's elevated. This never locks anyone out (a determined
# attacker distributed across enough IPs could otherwise turn a hard global
# lockout into a DoS against the real user); it only slows the response.
GLOBAL_WINDOW_SECONDS = 600
GLOBAL_DELAY_THRESHOLD = 10
GLOBAL_MAX_DELAY_SECONDS = 4.0


@dataclass
class _IPState:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    offense_count: int = 0
    last_offense_at: float = 0.0


class LoginRateLimiter:
    def __init__(self) -> None:
        self._state: dict[str, _IPState] = {}
        self._guard = Lock()
        self._global_failures: list[float] = []

    def _prune(self, state: _IPState, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        state.failures = [t for t in state.failures if t > cutoff]

    def is_locked_out(self, ip: str) -> tuple[bool, float]:
        """Returns (locked, seconds_remaining)."""
        now = time.time()
        with self._guard:
            state = self._state.get(ip)
            if state is None:
                return False, 0.0
            if state.locked_until > now:
                return True, state.locked_until - now
            return False, 0.0

    def begin_attempt(self, ip: str) -> tuple[bool, float, bool, float, int]:
        """Count an attempt as failed BEFORE its password is checked (record_success
        forgives it). Checking the lockout first and counting only after the slow
        password check let parallel requests all pass the check (dozens of guesses
        per lockout). Callers run this on the event loop with no await in between,
        so the two steps can't interleave with another request.
        Returns (refused, seconds_remaining, now_locked, lockout_seconds, attempt_count)."""
        locked, remaining = self.is_locked_out(ip)
        if locked:
            return True, remaining, False, 0.0, 0
        return (False, 0.0, *self.record_failure(ip))

    def record_failure(self, ip: str) -> tuple[bool, float, int]:
        """Record a failed attempt; returns (now_locked, lockout_seconds, attempt_count)."""
        now = time.time()
        with self._guard:
            state = self._state.setdefault(ip, _IPState())
            self._prune(state, now)
            state.failures.append(now)
            attempt_count = len(state.failures)

            global_cutoff = now - GLOBAL_WINDOW_SECONDS
            self._global_failures = [t for t in self._global_failures if t > global_cutoff]
            self._global_failures.append(now)

            if attempt_count >= MAX_ATTEMPTS:
                if state.last_offense_at and now - state.last_offense_at > OFFENSE_DECAY_SECONDS:
                    state.offense_count = 0
                state.offense_count += 1
                state.last_offense_at = now
                lockout = min(BASE_LOCKOUT_SECONDS * (2 ** (state.offense_count - 1)), MAX_LOCKOUT_SECONDS)
                state.locked_until = now + lockout
                state.failures = []
                return True, lockout, attempt_count
            return False, 0.0, attempt_count

    def record_success(self, ip: str) -> None:
        with self._guard:
            self._state.pop(ip, None)
            if self._global_failures:  # begin_attempt counted this attempt as a failure
                self._global_failures.pop()

    def delay_for(self, attempt_count: int) -> float:
        """Extra seconds to sleep before responding to a failed attempt.

        Grows with how many failures this IP has already racked up in the
        current window (independent of whether that crosses MAX_ATTEMPTS),
        plus a flat extra delay whenever recent failures across ALL IPs
        combined are elevated -- see GLOBAL_WINDOW_SECONDS above for why the
        per-IP count alone isn't enough on a single-user site."""
        per_ip = min(PER_ATTEMPT_DELAY_SECONDS * attempt_count, MAX_DELAY_SECONDS)
        with self._guard:
            global_count = len(self._global_failures)
        global_extra = GLOBAL_MAX_DELAY_SECONDS if global_count >= GLOBAL_DELAY_THRESHOLD else 0.0
        return per_ip + global_extra


rate_limiter = LoginRateLimiter()
