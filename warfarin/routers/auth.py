"""Login, logout and self-service password change."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request

from warfarin import staff
from warfarin.audit import log_audit_standalone
from warfarin.config import get_settings
from warfarin.db import db
from warfarin.deps import (
    client_ip,
    csrf_protect,
    get_session,
    redirect,
    require_user,
)
from warfarin.security import (
    SESSION_COOKIE,
    clear_login_attempts,
    create_session,
    destroy_session,
    destroy_sessions_for_user,
    is_login_locked,
    login_attempts_remaining,
    record_login_failure,
)
from warfarin.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"], dependencies=[Depends(csrf_protect)])


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not target:
        return "/dashboard"
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return "/dashboard"
    return target


@router.get("/")
def root(request: Request):
    return redirect("/dashboard" if get_session(request) else "/login")


@router.get("/login")
def login_page(request: Request, next: str = "/dashboard"):
    if get_session(request):
        return redirect("/dashboard")
    return render(request, "login.html", {"error": "", "next": _safe_next(next)})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/dashboard"),
):
    ip = client_ip(request)
    target = _safe_next(next)

    if is_login_locked(ip):
        minutes = get_settings().login_lockout_seconds // 60
        return render(request, "login.html", {
            "error": f"พยายามเข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารอ {minutes} นาทีแล้วลองใหม่",
            "next": target,
        })

    account = staff.authenticate(username.strip(), password)
    if account is None:
        record_login_failure(ip)
        remaining = login_attempts_remaining(ip)
        log_audit_standalone("login_failed", "staff", username[:40], ip, "")
        return render(request, "login.html", {
            "error": f"ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง (เหลืออีก {remaining} ครั้ง)",
            "next": target,
        })

    clear_login_attempts(ip)
    session_id, _ = create_session(
        account, ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    log_audit_standalone(
        "login", "staff", account["staff_id"], account["username"], f"ip={ip}"
    )

    if account.get("must_change_password"):
        target = "/account/password"
    response = redirect(target)
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_hours * 3600,
        path="/",
    )
    return response


@router.get("/logout")
def logout(request: Request):
    session = get_session(request)
    if session:
        log_audit_standalone(
            "logout", "staff", session.get("staff_id"), session.get("username"), ""
        )
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response = redirect("/login", "ออกจากระบบเรียบร้อยแล้ว", "info")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/account/password")
def password_page(request: Request):
    session = get_session(request)
    if session is None:
        return redirect("/login")
    return render(request, "change_password.html", {
        "user": session,
        "forced": bool(session.get("must_change_password")),
        "error": "",
    })


@router.post("/account/password")
def password_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    session = get_session(request)
    if session is None:
        return redirect("/login")
    if new_password != confirm_password:
        return render(request, "change_password.html", {
            "user": session, "forced": bool(session.get("must_change_password")),
            "error": "รหัสผ่านใหม่และการยืนยันไม่ตรงกัน",
        })
    try:
        with db() as conn:
            staff.change_own_password(
                conn, session["username"], current_password, new_password
            )
    except staff.StaffError as exc:
        return render(request, "change_password.html", {
            "user": session, "forced": bool(session.get("must_change_password")),
            "error": str(exc),
        })

    # A password change invalidates every session of that user, including this one.
    destroy_sessions_for_user(session["username"])
    response = redirect("/login", "เปลี่ยนรหัสผ่านเรียบร้อย กรุณาเข้าสู่ระบบใหม่", "success")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/account")
def account_page(request: Request, user: dict = Depends(require_user)):
    return render(request, "account.html", {"user": user})
