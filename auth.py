import os
import secrets
import time

# ── Login credentials ─────────────────────────────────────────────────────────
# Edit these to change the sign-in for every environment, or leave them and set
# AUTH_USERNAME / AUTH_PASSWORD instead — the environment always wins.
#
# This password guards live AWS credentials entered through the UI, so pick
# something long. Anything committed here is readable by everyone with repo
# access; prefer the environment variables for anything deployed.
DEFAULT_USERNAME = "Admin"
DEFAULT_PASSWORD = "Admin@123"

SESSION_COOKIE = "cca_session"
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

_USERNAME = os.environ.get("AUTH_USERNAME", DEFAULT_USERNAME)
_PASSWORD = os.environ.get("AUTH_PASSWORD", DEFAULT_PASSWORD)

# token -> expiry epoch. In-process, so a restart signs everyone out. That
# matches the rest of the app, which is already single-process by design.
_sessions: dict[str, float] = {}


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time comparison so response timing does not leak the values."""
    user_ok = secrets.compare_digest(username or "", _USERNAME)
    pass_ok = secrets.compare_digest(password or "", _PASSWORD)
    return user_ok and pass_ok


def create_session() -> str:
    _purge_expired()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL_SECONDS
    return token


def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry < time.time():
        del _sessions[token]
        return False
    return True


def destroy_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def session_max_age() -> int:
    return _SESSION_TTL_SECONDS


def _purge_expired() -> None:
    now = time.time()
    for token in [t for t, expiry in _sessions.items() if expiry < now]:
        del _sessions[token]
