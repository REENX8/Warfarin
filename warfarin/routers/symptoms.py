"""Staff-side symptom triage: review, reply and resolve."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from warfarin import symptoms as service
from warfarin.clinical import SYMPTOM_STATUS_LABELS
from warfarin.db import db
from warfarin.deps import csrf_protect, redirect, require_role, require_user
from warfarin.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/symptoms", tags=["symptoms"], dependencies=[Depends(csrf_protect)])

PAGE_SIZE = 50
reply_allowed = require_role("admin", "pharmacist", "nurse")


@router.get("")
def list_symptoms(
    request: Request,
    status: str = "open",
    page: int = 1,
    user: dict = Depends(require_user),
):
    if status not in SYMPTOM_STATUS_LABELS and status not in ("open", "all"):
        status = "open"
    query_status = "" if status == "all" else status
    page = max(page, 1)
    offset = (page - 1) * PAGE_SIZE
    reports = service.list_reports(query_status, PAGE_SIZE, offset)
    total = service.count_reports(query_status)
    return render(request, "symptoms.html", {
        "user": user,
        "reports": reports,
        "status": status,
        "page": page,
        "total": total,
        "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "open_count": service.open_count(),
    })


@router.post("/{report_id}/reply")
async def reply_symptom(
    request: Request, report_id: int, user: dict = Depends(reply_allowed)
):
    form = await request.form()
    reply = str(form.get("reply") or "").strip()
    if not reply:
        return redirect("/symptoms", "กรุณากรอกข้อความตอบกลับ", "danger")
    with db() as conn:
        report = service.record_reply(conn, report_id, reply, user["username"])
    if report is None:
        return redirect("/symptoms", "ไม่พบรายงานอาการ", "danger")
    delivered = service.deliver_reply_to_patient(report, reply)
    message = (
        "ส่งคำตอบถึงผู้ป่วยทาง LINE เรียบร้อย" if delivered
        else "บันทึกคำตอบแล้ว (ผู้ป่วยยังไม่ได้เชื่อม LINE จึงยังไม่ได้ส่ง)"
    )
    return redirect("/symptoms", message, "success" if delivered else "warning")


@router.post("/{report_id}/resolve")
def resolve_symptom(
    request: Request, report_id: int, user: dict = Depends(require_user)
):
    with db() as conn:
        ok = service.resolve(conn, report_id, user["username"])
    if not ok:
        return redirect("/symptoms", "ไม่พบรายงานอาการ", "danger")
    return redirect("/symptoms", "ปิดเรื่องรายงานอาการเรียบร้อย")
