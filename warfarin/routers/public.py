"""Patient-facing pages — no staff login, reached from LINE links or QR codes.

Every route here is addressed by an unguessable token rather than a numeric
patient id, so patient data cannot be enumerated, and each is rate limited.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from warfarin import appointments as appt
from warfarin import doses as dose_service
from warfarin import notifications, symptoms
from warfarin import patients as patient_service
from warfarin.adherence import (
    compute_adherence,
    compute_streak,
    compute_ttr,
    heatmap_series,
)
from warfarin.clinical import (
    BLEEDING_RED_FLAGS,
    DRUG_INTERACTIONS,
    EDUCATION_TOPICS,
    VITAMIN_K_FOODS,
    assess_inr,
)
from warfarin.db import db, read_db
from warfarin.deps import csrf_protect, enforce_rate_limit
from warfarin.templating import render
from warfarin.time_utils import today

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public"], dependencies=[Depends(csrf_protect)])


# ---------------------------------------------------------------------------
# Education (fully public)
# ---------------------------------------------------------------------------
@router.get("/education")
def education_page(request: Request):
    return render(request, "education.html", {
        "topics": EDUCATION_TOPICS,
        "interactions": DRUG_INTERACTIONS,
        "vitamin_k_foods": VITAMIN_K_FOODS,
        "red_flags": BLEEDING_RED_FLAGS,
    })


# ---------------------------------------------------------------------------
# Dose confirmation
# ---------------------------------------------------------------------------
@router.get("/dose/{token_id}")
def dose_confirm_page(request: Request, token_id: str):
    enforce_rate_limit(request, "dose_view", 60, 60)
    with read_db() as conn:
        token = dose_service.lookup_token(conn, token_id)
        if token is None:
            raise HTTPException(404, "ไม่พบข้อมูลยา หรือลิงก์ไม่ถูกต้อง")
        adherence = compute_adherence(conn, token["pid"], 7)
        streak = compute_streak(conn, token["pid"])

    if adherence["percent"] >= 90:
        encouragement = "ยอดเยี่ยมมาก! คุณกินยาได้สม่ำเสมอมากค่ะ 🌟"
    elif adherence["percent"] >= 70:
        encouragement = "ดีมากค่ะ พยายามกินยาให้สม่ำเสมอต่อไปนะคะ 💪"
    else:
        encouragement = "อย่าลืมกินยาทุกวันนะคะ สุขภาพสำคัญค่ะ ❤️"

    return render(request, "dose_confirm.html", {
        "token": token,
        "already": token["is_used"] == 1,
        "expired": dose_service.token_expired(token),
        "adherence": adherence,
        "streak": streak,
        "adh_msg": encouragement,
    })


@router.post("/dose/{token_id}/confirm")
async def dose_confirm(request: Request, token_id: str):
    enforce_rate_limit(request, "dose_confirm", 20, 60)
    form = await request.form()
    confirm_source = str(form.get("confirm_source") or "patient")

    with db() as conn:
        token = dose_service.lookup_token(conn, token_id)
        if token is None:
            raise HTTPException(404, "ไม่พบข้อมูลยา หรือลิงก์ไม่ถูกต้อง")
        result = dose_service.confirm_dose(conn, token, confirm_source)
        streak = compute_streak(conn, token["pid"]) if result["ok"] else 0

    if not result["ok"]:
        messages = {
            "already_used": "โดสนี้ถูกยืนยันไปแล้วก่อนหน้านี้ — ไม่สามารถยืนยันซ้ำได้",
            "expired": "ลิงก์ยืนยันนี้หมดอายุแล้ว กรุณาติดต่อเจ้าหน้าที่",
        }
        return render(request, "dose_result.html", {
            "success": False,
            "message": messages.get(result["reason"], "ไม่สามารถยืนยันได้"),
            "dose": token,
        })

    notifications.send_confirmation(
        {
            "patient_id": token["pid"],
            "full_name": token["full_name"],
            "line_user_id": token["line_user_id"],
        },
        {"warfarin_mg": token["warfarin_mg"], "dose_id": token["dose_id"]},
        streak,
    )
    late_note = " (กินยาช้ากว่ากำหนด)" if result["status"] == "late" else ""
    return render(request, "dose_result.html", {
        "success": True,
        "dose": token,
        "streak": streak,
        "status": result["status"],
        "message": f"บันทึกเรียบร้อย! กินยาต่อเนื่อง {streak} วัน{late_note}",
        "portal_token": token.get("access_token"),
    })


# ---------------------------------------------------------------------------
# Symptom reporting
# ---------------------------------------------------------------------------
def _patient_by_token_or_404(token: str) -> dict:
    with read_db() as conn:
        patient = patient_service.get_patient_by_token(conn, token)
    if patient is None:
        raise HTTPException(404, "ไม่พบข้อมูลผู้ป่วย หรือลิงก์ไม่ถูกต้อง")
    return patient


@router.get("/report/symptom/{token}")
def symptom_form(request: Request, token: str):
    enforce_rate_limit(request, "symptom_view", 40, 60)
    patient = _patient_by_token_or_404(token)
    return render(request, "symptom_form.html", {
        "patient": patient, "token": token, "today": today(),
        "red_flags": BLEEDING_RED_FLAGS,
    })


@router.post("/report/symptom/{token}")
async def symptom_submit(request: Request, token: str):
    enforce_rate_limit(request, "symptom_submit", 10, 300)
    patient = _patient_by_token_or_404(token)
    form = dict(await request.form())
    with db() as conn:
        result = symptoms.create_report(conn, patient, form, source="patient")

    symptoms.acknowledge_patient(patient, result)
    symptoms.notify_staff(patient, result)

    return render(request, "symptom_result.html", {
        "patient": patient,
        "result": result,
        "severity": result["severity"],
        "red_flags": BLEEDING_RED_FLAGS,
        "portal_token": patient.get("access_token"),
    })


# ---------------------------------------------------------------------------
# Patient self-service portal
# ---------------------------------------------------------------------------
@router.get("/p/{token}")
def patient_portal(request: Request, token: str):
    enforce_rate_limit(request, "portal", 60, 60)
    patient = _patient_by_token_or_404(token)
    pid = patient["patient_id"]
    with read_db() as conn:
        adherence7 = compute_adherence(conn, pid, 7)
        adherence30 = compute_adherence(conn, pid, 30)
        streak = compute_streak(conn, pid)
        ttr = compute_ttr(conn, pid)
        labs = appt.labs_for_patient(conn, pid, limit=10)
        upcoming = dose_service.upcoming_doses(conn, pid, limit=14)
        appointments = appt.upcoming_for_patient(conn, pid, limit=3)
        heatmap = heatmap_series(conn, pid, 90)
        weekly_mg = dose_service.current_weekly_mg(conn, pid)
        today_dose = next(
            (d for d in upcoming if d["scheduled_date"] == today()), None
        )
    latest = labs[0] if labs else None
    assessment = (
        assess_inr(latest["value"], patient["target_inr_min"], patient["target_inr_max"])
        if latest else None
    )
    return render(request, "portal.html", {
        "patient": patient,
        "token": token,
        "adh7": adherence7,
        "adh30": adherence30,
        "streak": streak,
        "ttr": ttr,
        "labs": labs,
        "latest_lab": latest,
        "assessment": assessment,
        "upcoming": upcoming,
        "appointments": appointments,
        "heatmap": heatmap,
        "weekly_mg": weekly_mg,
        "today_dose": today_dose,
        "topics": EDUCATION_TOPICS,
    })
