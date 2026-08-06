"""Request-scoped dependencies: authentication, roles, CSRF and rate limits."""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from warfarin.security import SESSION_COOKIE, is_rate_limited, load_session

logger = logging.getLogger(__name__)


class RedirectException(Exception):
    """Raised to send a browser somewhere else (login, dashboard, ...)."""

    def __init__(self, location: str, status_code: int = 303):
        super().__init__(location)
        self.location = location
        self.status_code = status_code


def redirect(location: str, message: str = "", kind: str = "success") -> RedirectResponse:
    """303 redirect, optionally carrying a flash message in the query string."""
    if message:
        from urllib.parse import urlencode

        separator = "&" if "?" in location else "?"
        location = f"{location}{separator}{urlencode({'msg': message, 'kind': kind})}"
    return RedirectResponse(location, status_code=303)


def get_session(request: Request) -> dict | None:
    """Session for this request, loaded once and cached on request.state."""
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached
    session = load_session(request.cookies.get(SESSION_COOKIE))
    request.state.session = session
    request.state.user = session
    return session


def current_user(request: Request) -> dict | None:
    return get_session(request)


def require_user(request: Request) -> dict:
    """Dependency for staff-only pages — redirects to login when signed out."""
    session = get_session(request)
    if session is None:
        # JSON callers get a status they can branch on; browsers get the
        # login page with a return path.
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="ต้องเข้าสู่ระบบก่อน")
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise RedirectException(f"/login?next={target}")
    if session.get("must_change_password") and request.url.path != "/account/password":
        raise RedirectException("/account/password")
    return session


def require_role(*roles: str):
    """Dependency factory restricting a route to the given staff roles."""

    def dependency(request: Request) -> dict:
        session = require_user(request)
        if session.get("role") not in roles:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงส่วนนี้")
        return session

    return dependency


require_admin = require_role("admin")
require_clinical = require_role("admin", "pharmacist", "nurse")


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring one layer of reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def enforce_rate_limit(
    request: Request, bucket: str, max_requests: int, window_seconds: int
) -> None:
    """Raise 429 when a public endpoint is being hammered from one address."""
    key = f"{bucket}:{client_ip(request)}"
    if is_rate_limited(key, max_requests, window_seconds):
        raise HTTPException(
            status_code=429,
            detail="มีการเรียกใช้งานถี่เกินไป กรุณารอสักครู่แล้วลองใหม่",
        )


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def csrf_protect(request: Request) -> None:
    """Router-level dependency rejecting state-changing requests without a token.

    Runs as a dependency rather than middleware so it can read the parsed form
    from the same Request the endpoint uses — FastAPI caches `request.form()`,
    so the body is only consumed once.
    """
    from warfarin.config import get_settings

    if request.method not in UNSAFE_METHODS:
        return
    if not get_settings().csrf_enabled:
        return

    submitted = request.headers.get("x-csrf-token", "")
    if not submitted:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            submitted = str(form.get("csrf_token") or "")

    expected = expected_csrf_token(request)
    if not expected or not submitted or not _tokens_equal(expected, submitted):
        logger.warning(
            "CSRF check failed for %s %s from %s",
            request.method, request.url.path, client_ip(request),
        )
        raise HTTPException(
            status_code=403,
            detail="คำขอไม่ถูกต้อง (CSRF) กรุณารีเฟรชหน้าและลองใหม่อีกครั้ง",
        )


def _tokens_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


ANON_CSRF_COOKIE = "csrf_token"


def expected_csrf_token(request: Request) -> str:
    """Session token for signed-in staff; the anonymous cookie otherwise."""
    session = get_session(request)
    if session and session.get("csrf_token"):
        return str(session["csrf_token"])
    return request.cookies.get(ANON_CSRF_COOKIE) or getattr(
        request.state, "anon_csrf", ""
    )
