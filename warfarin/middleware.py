"""HTTP middleware: security headers, session priming and anonymous CSRF cookies."""
from __future__ import annotations

import logging
import secrets
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from warfarin.config import get_settings
from warfarin.deps import ANON_CSRF_COOKIE, get_session

logger = logging.getLogger(__name__)

# All CSS, fonts and JavaScript are served from /static, so nothing outside
# this origin needs to load. That also means the app renders correctly on a
# hospital network that blocks external CDNs.
# 'unsafe-inline' is still required for the inline <style>/<script> blocks the
# templates use for per-page chart setup.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "media-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(self)",
    "Content-Security-Policy": CSP,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers, and HSTS when served over HTTPS."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if get_settings().cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class SessionMiddleware(BaseHTTPMiddleware):
    """Load the session once per request and keep an anonymous CSRF cookie alive."""

    async def dispatch(self, request: Request, call_next):
        request.state.session = None
        request.state.user = None
        try:
            get_session(request)
        except Exception:
            logger.exception("Session load failed; continuing as anonymous")
            request.state.session = None

        # The anonymous CSRF token must exist *before* the page renders, so
        # the very first public form already carries a token the next POST
        # can be checked against.
        existing = request.cookies.get(ANON_CSRF_COOKIE)
        request.state.anon_csrf = existing or secrets.token_urlsafe(24)

        response = await call_next(request)

        if not existing:
            settings = get_settings()
            response.set_cookie(
                ANON_CSRF_COOKIE,
                request.state.anon_csrf,
                httponly=False,   # public forms read it back from the cookie
                samesite="lax",
                secure=settings.cookie_secure,
                max_age=60 * 60 * 24 * 7,
                path="/",
            )
        return response


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Structured access log with a request id and duration."""

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed id=%s method=%s path=%s",
                request_id, request.method, request.url.path,
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        if duration_ms > 1000 or response.status_code >= 500:
            logger.warning(
                "slow_or_failed id=%s %s %s -> %s in %.0fms",
                request_id, request.method, request.url.path,
                response.status_code, duration_ms,
            )
        else:
            logger.debug(
                "request id=%s %s %s -> %s in %.0fms",
                request_id, request.method, request.url.path,
                response.status_code, duration_ms,
            )
        return response
