"""Admin area: staff accounts, audit log, notification log and system status."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from warfarin import audit, notifications, scheduler, staff, symptoms
from warfarin.config import get_settings
from warfarin.db import db, read_db
from warfarin.deps import csrf_protect, redirect, require_admin, require_user
from warfarin.line_service import (
    FLEX_AVAILABLE,
    RICHMENU_AVAILABLE,
    SDK_AVAILABLE,
    push_enabled,
)
from warfarin.migrations import LATEST_VERSION, schema_version
from warfarin.security import ROLES
from warfarin.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"], dependencies=[Depends(csrf_protect)])

PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Staff accounts
# ---------------------------------------------------------------------------
@router.get("/staff")
def list_staff(request: Request, user: dict = Depends(require_admin)):
    with read_db() as conn:
        accounts = staff.list_staff(conn)
    return render(request, "staff_list.html", {
        "user": user, "accounts": accounts, "staff_roles": ROLES,
    })


@router.get("/staff/new")
def new_staff_form(request: Request, user: dict = Depends(require_admin)):
    return render(request, "staff_form.html", {
        "user": user, "account": None, "staff_roles": ROLES, "error": "",
    })


@router.post("/staff/new")
async def create_staff(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    try:
        with db() as conn:
            staff.create_staff(
                conn,
                username=str(form.get("username") or ""),
                password=str(form.get("password") or ""),
                full_name=str(form.get("full_name") or ""),
                role=str(form.get("role") or "nurse"),
                performed_by=user["username"],
                must_change_password=form.get("must_change_password") in ("on", "1"),
            )
    except staff.StaffError as exc:
        return render(request, "staff_form.html", {
            "user": user, "account": None, "staff_roles": ROLES, "error": str(exc),
            "form": dict(form),
        }, status_code=400)
    return redirect("/staff", "เพิ่มบัญชีผู้ใช้เรียบร้อย")


@router.get("/staff/{staff_id}/edit")
def edit_staff_form(
    request: Request, staff_id: int, user: dict = Depends(require_admin)
):
    with read_db() as conn:
        account = staff.get_by_id(conn, staff_id)
    if account is None:
        raise HTTPException(404, "ไม่พบบัญชีผู้ใช้")
    return render(request, "staff_form.html", {
        "user": user, "account": account, "staff_roles": ROLES, "error": "",
    })


@router.post("/staff/{staff_id}/edit")
async def edit_staff(
    request: Request, staff_id: int, user: dict = Depends(require_admin)
):
    form = await request.form()
    try:
        with db() as conn:
            staff.update_staff(
                conn,
                staff_id=staff_id,
                full_name=str(form.get("full_name") or ""),
                role=str(form.get("role") or "nurse"),
                is_active=form.get("is_active") in ("on", "1", "true"),
                performed_by=user["username"],
                current_username=user["username"],
            )
    except staff.StaffError as exc:
        with read_db() as conn:
            account = staff.get_by_id(conn, staff_id)
        return render(request, "staff_form.html", {
            "user": user, "account": account, "staff_roles": ROLES, "error": str(exc),
        }, status_code=400)
    return redirect("/staff", "บันทึกบัญชีผู้ใช้เรียบร้อย")


@router.post("/staff/{staff_id}/reset-password")
async def reset_staff_password(
    request: Request, staff_id: int, user: dict = Depends(require_admin)
):
    form = await request.form()
    try:
        with db() as conn:
            username = staff.reset_password(
                conn, staff_id, str(form.get("password") or ""), user["username"]
            )
    except staff.StaffError as exc:
        return redirect(f"/staff/{staff_id}/edit", str(exc), "danger")
    return redirect(
        "/staff", f"รีเซ็ตรหัสผ่านของ {username} เรียบร้อย (ผู้ใช้ต้องตั้งรหัสใหม่เมื่อเข้าระบบ)"
    )


# ---------------------------------------------------------------------------
# Audit and notification logs
# ---------------------------------------------------------------------------
@router.get("/audit")
def audit_page(
    request: Request, page: int = 1, action: str = "", user: dict = Depends(require_admin)
):
    page = max(page, 1)
    offset = (page - 1) * PAGE_SIZE
    logs = audit.recent_entries(PAGE_SIZE, offset, action)
    total = audit.count_entries(action)
    return render(request, "audit.html", {
        "user": user, "logs": logs, "page": page, "action": action,
        "actions": audit.distinct_actions(), "total": total,
        "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    })


@router.get("/notifications")
def notifications_page(
    request: Request,
    page: int = 1,
    message_type: str = "",
    user: dict = Depends(require_user),
):
    page = max(page, 1)
    offset = (page - 1) * PAGE_SIZE
    logs = notifications.recent_notifications(PAGE_SIZE, offset, message_type)
    total = notifications.count_notifications(message_type)
    return render(request, "notifications.html", {
        "user": user, "logs": logs, "page": page, "message_type": message_type,
        "message_types": notifications.MESSAGE_TYPES, "total": total,
        "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "stats": notifications.delivery_stats(7),
    })


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------
@router.get("/system")
def system_page(request: Request, user: dict = Depends(require_admin)):
    settings = get_settings()
    return render(request, "system.html", {
        "user": user,
        "settings": settings,
        "schema_version": schema_version(),
        "latest_schema": LATEST_VERSION,
        "jobs": scheduler.job_status(),
        "job_names": list(scheduler.JOBS),
        "line": {
            "push": push_enabled(),
            "webhook": settings.line_webhook_enabled,
            "sdk": SDK_AVAILABLE,
            "flex": FLEX_AVAILABLE,
            "richmenu": RICHMENU_AVAILABLE,
            "staff_code": bool(settings.line_staff_register_code),
        },
        "recipients": symptoms.list_recipients(),
    })


@router.post("/system/run-job")
def run_job(
    request: Request, job: str = Form(""), user: dict = Depends(require_admin)
):
    try:
        detail = scheduler.run_job(job)
    except KeyError:
        return redirect("/system", "ไม่พบงานที่ระบุ", "danger")
    except Exception as exc:
        logger.exception("Manual job run failed: %s", job)
        return redirect("/system", f"งานล้มเหลว: {exc}", "danger")
    return redirect("/system", f"รันงาน {job} เรียบร้อย — {detail}")


@router.post("/system/rich-menu")
def refresh_rich_menu(request: Request, user: dict = Depends(require_admin)):
    from warfarin.rich_menu import create_rich_menu

    ok, detail = create_rich_menu(force=True)
    return redirect(
        "/system",
        f"อัปเดต Rich Menu: {detail}",
        "success" if ok else "danger",
    )
