"""Passwords, sessions, CSRF and brute-force protection.

Password hashes use the same `pbkdf2:sha256:<iters>$<salt>$<hex>` format as
Werkzeug, so hashes can be moved between this system and the TB tracker.
Hashes produced by the pre-2.0 unsalted scheme are still accepted at login and
transparently upgraded, so no one is locked out by the upgrade.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict
from datetime import timedelta

from warfarin.config import get_settings
from warfarin.db import db, read_db
from warfarin.time_utils import now, now_dt, parse_dt

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD_LENGTH = 8

ROLES = {
    "admin": "ผู้ดูแลระบบ",
    "pharmacist": "เภสัชกร",
    "nurse": "พยาบาล",
    "staff": "เจ้าหน้าที่",
}
# Roles allowed to change clinical data (doses, labs, plans).
CLINICAL_ROLES = ("admin", "pharmacist", "nurse")
DEFAULT_ROLE = "nurse"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()
    return f"pbkdf2:sha256:{iterations}${salt}${digest}"


def _verify_pbkdf2(stored: str, password: str) -> bool:
    try:
        method, salt, digest = stored.split("$", 2)
        parts = method.split(":")
        iterations = int(parts[2]) if len(parts) > 2 and parts[2] else 260_000
        algo = parts[1] if len(parts) > 1 else "sha256"
    except (ValueError, IndexError):
        return False
    try:
        calc = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), salt.encode("utf-8"), iterations
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, digest)


def _legacy_hash(password: str) -> str:
    """Pre-2.0 scheme: sha256(password + SECRET_KEY), unsalted."""
    return hashlib.sha256((password + get_settings().secret_key).encode()).hexdigest()


def verify_password(stored_hash: str, password: str) -> bool:
    """Check a password against either the current or the legacy hash format."""
    if not stored_hash or password is None:
        return False
    if stored_hash.startswith("pbkdf2:"):
        return _verify_pbkdf2(stored_hash, password)
    # Legacy unsalted SHA-256 (upgraded on the next successful login).
    return hmac.compare_digest(_legacy_hash(password), stored_hash)


def needs_rehash(stored_hash: str) -> bool:
    return not stored_hash.startswith("pbkdf2:")


def validate_password(password: str) -> str | None:
    """Return a Thai error message, or None when the password is acceptable."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร"
    if password.isdigit():
        return "รหัสผ่านต้องไม่ใช่ตัวเลขล้วน"
    if password.lower() in ("password", "admin123", "12345678", "warfarin"):
        return "รหัสผ่านนี้เดาง่ายเกินไป กรุณาตั้งรหัสผ่านอื่น"
    return None


# ---------------------------------------------------------------------------
# Brute-force protection (per-process, sliding window)
# ---------------------------------------------------------------------------
_login_attempts: dict[str, list[float]] = defaultdict(list)
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_MAX_TRACKED_KEYS = 10_000


def _prune(store: dict[str, list[float]], window: int) -> None:
    """Drop stale keys so a flood of unique IPs cannot grow memory forever."""
    if len(store) <= _MAX_TRACKED_KEYS:
        return
    cutoff = time.time() - window
    for key in [k for k, v in store.items() if not v or max(v) < cutoff]:
        store.pop(key, None)


def is_login_locked(ip: str) -> bool:
    cfg = get_settings()
    current = time.time()
    _login_attempts[ip] = [
        t for t in _login_attempts[ip] if current - t < cfg.login_lockout_seconds
    ]
    return len(_login_attempts[ip]) >= cfg.max_login_attempts


def record_login_failure(ip: str) -> None:
    _login_attempts[ip].append(time.time())
    _prune(_login_attempts, get_settings().login_lockout_seconds)


def clear_login_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)


def login_attempts_remaining(ip: str) -> int:
    cfg = get_settings()
    return max(0, cfg.max_login_attempts - len(_login_attempts.get(ip, [])))


def is_rate_limited(key: str, max_requests: int, window_seconds: int) -> bool:
    """Sliding-window limiter. Records the hit and returns True to reject."""
    current = time.time()
    bucket = [t for t in _rate_buckets[key] if current - t < window_seconds]
    limited = len(bucket) >= max_requests
    if not limited:
        bucket.append(current)
    _rate_buckets[key] = bucket
    _prune(_rate_buckets, window_seconds)
    return limited


def reset_security_state() -> None:
    """Clear in-memory limiter state (used by tests)."""
    _login_attempts.clear()
    _rate_buckets.clear()


# ---------------------------------------------------------------------------
# Server-side sessions
# ---------------------------------------------------------------------------
SESSION_COOKIE = "session_id"


def create_session(staff: dict, ip: str = "", user_agent: str = "") -> tuple[str, str]:
    """Persist a new session row. Returns (session_id, csrf_token)."""
    cfg = get_settings()
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    created = now_dt()
    expires = created + timedelta(hours=max(1, cfg.session_hours))
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, staff_id, username, full_name, role, "
            "csrf_token, ip, user_agent, must_change_password, created_at, last_seen_at, "
            "expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                staff.get("staff_id"),
                staff.get("username"),
                staff.get("full_name"),
                staff.get("role", DEFAULT_ROLE),
                csrf_token,
                ip[:64],
                (user_agent or "")[:255],
                1 if staff.get("must_change_password") else 0,
                created.isoformat(timespec="seconds"),
                created.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
    return session_id, csrf_token


def load_session(session_id: str | None) -> dict | None:
    """Fetch a live session, refreshing last_seen_at. None when invalid."""
    if not session_id:
        return None
    with read_db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if row is None:
        return None
    session = dict(row)
    expires = parse_dt(session.get("expires_at"))
    if expires is None or now_dt() > expires:
        destroy_session(session_id)
        return None
    idle_minutes = get_settings().session_idle_minutes
    if idle_minutes > 0:
        last_seen = parse_dt(session.get("last_seen_at"))
        if last_seen and now_dt() - last_seen > timedelta(minutes=idle_minutes):
            destroy_session(session_id)
            return None
    _touch_session(session_id)
    return session


def _touch_session(session_id: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                (now(), session_id),
            )
    except Exception:  # pragma: no cover - a touch failure must never 500
        logger.warning("Failed to refresh session timestamp", exc_info=True)


def destroy_session(session_id: str | None) -> None:
    if not session_id:
        return
    try:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    except Exception:  # pragma: no cover
        logger.warning("Failed to destroy session", exc_info=True)


def destroy_sessions_for_user(username: str, conn=None) -> int:
    """Revoke every session of a user (password change, deactivation).

    Pass `conn` when calling from inside an open write transaction — opening
    a second write connection against the same SQLite file would deadlock
    until the busy timeout expires.
    """
    if conn is not None:
        return conn.execute(
            "DELETE FROM sessions WHERE username=?", (username,)
        ).rowcount or 0
    with db() as own_conn:
        return own_conn.execute(
            "DELETE FROM sessions WHERE username=?", (username,)
        ).rowcount or 0


def purge_expired_sessions() -> int:
    with db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
        return cur.rowcount or 0


def rotate_csrf_token(session_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET csrf_token=? WHERE session_id=?", (token, session_id)
        )
    return token


def csrf_token_matches(session: dict | None, submitted: str | None) -> bool:
    if not session or not submitted:
        return False
    expected = session.get("csrf_token") or ""
    return bool(expected) and hmac.compare_digest(expected, submitted)


# ---------------------------------------------------------------------------
# Signed tokens for public patient links
# ---------------------------------------------------------------------------
def sign_value(value: str) -> str:
    """HMAC a public identifier so links cannot be forged by enumeration."""
    mac = hmac.new(
        get_settings().secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    )
    return mac.hexdigest()[:32]


def verify_signature(value: str, signature: str) -> bool:
    return hmac.compare_digest(sign_value(value), signature or "")


def new_access_token() -> str:
    """Unguessable token used for patient self-service links."""
    return secrets.token_urlsafe(24)
