"""Patient records: list, create, edit, and everything on the detail page."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from warfarin import appointments as appt
from warfarin import doses as dose_service
from warfarin import notifications, research, symptoms
from warfarin import patients as patient_service
from warfarin.adherence import (
    compute_adherence,
    compute_gamification_score,
    compute_streak,
    compute_ttr,
)
from warfarin.audit import log_audit
from warfarin.clinical import (
    assess_inr,
    days_of_stock,
    screen_medication_text,
    suggest_weekly_adjustment,
)
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, read_db
from warfarin.deps import csrf_protect, redirect, require_clinical, require_user
from warfarin.templating import render
from warfarin.time_utils import now, today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/patients", tags=["patients"], dependencies=[Depends(csrf_protect)])

PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# List / create
# ---------------------------------------------------------------------------
@router.get("")
def list_patients(
    request: Request,
    q: str = "",
    status: str = "active",
    page: int = 1,
    user: dict = Depends(require_user),
):
    query = q.strip()[:60]
    page = max(page, 1)
    offset = (page - 1) * PAGE_SIZE
    with read_db() as conn:
        rows = patient_service.search_patients(conn, query, status, PAGE_SIZE, offset)
        total = patient_service.count_patients(conn, query, status)
        ids = [row["patient_id"] for row in rows]
        from warfarin.adherence import compute_adherence_bulk, last_inr_bulk

        adherence = compute_adherence_bulk(conn, ids, 7)
        last_inr = last_inr_bulk(conn, ids)
    for row in rows:
        row["adherence"] = adherence.get(row["patient_id"])
        row["last_inr"] = last_inr.get(row["patient_id"])
    return render(request, "patients.html", {
        "user": user,
        "patients": rows,
        "q": query,
        "status": status,
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    })


@router.get("/new")
def new_patient_form(request: Request, user: dict = Depends(require_clinical)):
    return render(request, "patient_form.html", {
        "user": user, "patient": None, "caregiver": None, "errors": {}, "form": {},
    })


@router.post("/new")
async def create_patient(request: Request, user: dict = Depends(require_clinical)):
    form = dict(await request.form())
    try:
        data = patient_service.clean_patient_form(form)
    except patient_service.ValidationError as exc:
        return render(request, "patient_form.html", {
            "user": user, "patient": None, "caregiver": None,
            "errors": exc.errors, "form": form,
        }, status_code=400)
    try:
        with db() as conn:
            patient_id = patient_service.create_patient(conn, data, user["username"])
            patient_service.save_caregiver(conn, patient_id, form)
    except patient_service.ValidationError as exc:
        return render(request, "patient_form.html", {
            "user": user, "patient": None, "caregiver": None,
            "errors": exc.errors, "form": form,
        }, status_code=400)
    return redirect(f"/patients/{patient_id}", "เพิ่มผู้ป่วยเรียบร้อยแล้ว")


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@router.get("/{patient_id}")
def patient_detail(
    request: Request, patient_id: int, user: dict = Depends(require_user)
):
    settings = get_settings()
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
        if patient is None:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
        caregivers = patient_service.caregivers_for(conn, patient_id)
        dose_rows = fetch_all(
            conn,
            "SELECT mp.*, dt.token_id, dt.is_used FROM medication_plan mp "
            "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? ORDER BY mp.scheduled_date DESC, mp.scheduled_time DESC "
            "LIMIT 60",
            (patient_id,),
        )
        labs = appt.labs_for_patient(conn, patient_id)
        scores = fetch_all(
            conn,
            "SELECT * FROM test_scores WHERE patient_id=? ORDER BY taken_at DESC",
            (patient_id,),
        )
        surveys = fetch_all(
            conn,
            "SELECT * FROM satisfaction_surveys WHERE patient_id=? "
            "ORDER BY survey_date DESC LIMIT 5",
            (patient_id,),
        )
        adherence7 = compute_adherence(conn, patient_id, 7)
        adherence30 = compute_adherence(conn, patient_id, 30)
        adherence_all = compute_adherence(conn, patient_id, 365)
        streak = compute_streak(conn, patient_id)
        ttr = compute_ttr(conn, patient_id)
        weekly_mg = dose_service.current_weekly_mg(conn, patient_id)
        weekly_pattern = dose_service.weekly_pattern(conn, patient_id)
        plan_end = dose_service.plan_coverage_end(conn, patient_id)
        today_dose = next(
            (d for d in dose_rows if d["scheduled_date"] == today()), None
        )
        upcoming_appointments = appt.upcoming_for_patient(conn, patient_id)
        appointment_history = appt.history_for_patient(conn, patient_id)
        adjustments = appt.adjustments_for_patient(conn, patient_id)
        symptom_rows = symptoms.list_reports(patient_id=patient_id, limit=10)
        participant = research.get_by_patient(conn, patient_id)

    latest_lab = labs[0] if labs else None
    assessment = (
        assess_inr(
            latest_lab["value"], patient["target_inr_min"], patient["target_inr_max"]
        )
        if latest_lab else None
    )
    suggestion = (
        suggest_weekly_adjustment(
            weekly_mg, latest_lab["value"],
            patient["target_inr_min"], patient["target_inr_max"],
        )
        if latest_lab and weekly_mg else None
    )
    interaction_hits = screen_medication_text(
        " ".join(
            str(patient.get(field) or "")
            for field in ("chronic_conditions", "diagnosis", "notes", "allergies")
        )
    )
    stock_days = days_of_stock(int(patient.get("pill_inventory") or 0), weekly_mg)

    return render(request, "patient_detail.html", {
        "user": user,
        "patient": patient,
        "caregivers": caregivers,
        "caregiver": caregivers[0] if caregivers else None,
        "doses": dose_rows,
        "labs": labs,
        "scores": scores,
        "surveys": surveys,
        "symptoms": symptom_rows,
        "participant": participant,
        "adh7": adherence7,
        "adh30": adherence30,
        "adh_all": adherence_all,
        "streak": streak,
        "gamification_score": compute_gamification_score(adherence7["percent"], streak),
        "ttr": ttr,
        "weekly_mg": weekly_mg,
        "weekly_pattern": weekly_pattern,
        "plan_end": plan_end,
        "today_dose": today_dose,
        "latest_lab": latest_lab,
        "assessment": assessment,
        "suggestion": suggestion,
        "interaction_hits": interaction_hits,
        "stock_days": stock_days,
        "low_stock_threshold": settings.low_stock_threshold,
        "appointments": upcoming_appointments,
        "appointment_history": appointment_history,
        "adjustments": adjustments,
        "portal_url": f"{settings.base_url}/p/{patient.get('access_token') or ''}",
    })


@router.get("/{patient_id}/edit")
def edit_patient_form(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
        if patient is None:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
        caregivers = patient_service.caregivers_for(conn, patient_id)
    return render(request, "patient_form.html", {
        "user": user, "patient": patient,
        "caregiver": caregivers[0] if caregivers else None,
        "errors": {}, "form": patient,
    })


@router.post("/{patient_id}/edit")
async def update_patient(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = dict(await request.form())
    with read_db() as conn:
        existing = patient_service.get_patient(conn, patient_id)
        caregivers = patient_service.caregivers_for(conn, patient_id)
    if existing is None:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    try:
        data = patient_service.clean_patient_form(form, patient_id=patient_id)
        with db() as conn:
            patient_service.update_patient(conn, patient_id, data, user["username"])
            patient_service.save_caregiver(conn, patient_id, form)
    except patient_service.ValidationError as exc:
        return render(request, "patient_form.html", {
            "user": user, "patient": existing,
            "caregiver": caregivers[0] if caregivers else None,
            "errors": exc.errors, "form": form,
        }, status_code=400)
    return redirect(f"/patients/{patient_id}", "บันทึกข้อมูลผู้ป่วยเรียบร้อย")


@router.post("/{patient_id}/deactivate")
def deactivate_patient(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    with db() as conn:
        patient_service.set_active(conn, patient_id, False, user["username"])
    return redirect("/patients", "ปิดใช้งานผู้ป่วยเรียบร้อย", "info")


@router.post("/{patient_id}/reactivate")
def reactivate_patient(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    with db() as conn:
        patient_service.set_active(conn, patient_id, True, user["username"])
    return redirect(f"/patients/{patient_id}", "เปิดใช้งานผู้ป่วยเรียบร้อย")


# ---------------------------------------------------------------------------
# Medication plan
# ---------------------------------------------------------------------------
@router.post("/{patient_id}/doses")
async def create_doses(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = dict(await request.form())
    try:
        default_mg = float(form.get("warfarin_mg") or 0)
    except (TypeError, ValueError):
        default_mg = 0.0
    day_doses: dict[int, float] = {}
    for weekday in range(7):
        raw = form.get(f"dose_day_{weekday}")
        try:
            day_doses[weekday] = float(raw) if raw not in (None, "") else default_mg
        except (TypeError, ValueError):
            day_doses[weekday] = default_mg

    from warfarin.time_utils import parse_time

    try:
        with db() as conn:
            previous_weekly = dose_service.current_weekly_mg(conn, patient_id)
            created = dose_service.create_plan(
                conn,
                patient_id=patient_id,
                start_date=(form.get("start_date") or "").strip(),
                end_date=(form.get("end_date") or "").strip(),
                day_doses=day_doses,
                scheduled_time=parse_time(form.get("scheduled_time"), "18:00"),
                pill_description=(form.get("pill_description") or "").strip()[:120],
                performed_by=user["username"],
                replace_existing=form.get("replace_existing") in ("on", "1", "true"),
            )
            new_weekly = round(sum(day_doses.values()), 2)
            if previous_weekly and abs(new_weekly - previous_weekly) >= 0.5:
                appt.record_dose_adjustment(
                    conn, patient_id, previous_weekly, new_weekly,
                    None, (form.get("adjust_reason") or "ปรับแผนการกินยา")[:200],
                    user["username"],
                )
    except dose_service.DoseError as exc:
        return redirect(f"/patients/{patient_id}", str(exc), "danger")
    return redirect(f"/patients/{patient_id}", f"สร้างแผนยา {created} วันเรียบร้อย")


@router.post("/{patient_id}/doses/{dose_id}/status")
async def override_dose(
    request: Request,
    patient_id: int,
    dose_id: int,
    user: dict = Depends(require_clinical),
):
    form = await request.form()
    status = str(form.get("status") or "taken")
    try:
        with db() as conn:
            if not dose_service.override_status(conn, dose_id, status, user["username"]):
                raise HTTPException(404, "ไม่พบรายการยา")
    except dose_service.DoseError as exc:
        return redirect(f"/patients/{patient_id}", str(exc), "danger")
    return redirect(f"/patients/{patient_id}", "ปรับสถานะการกินยาเรียบร้อย")


# ---------------------------------------------------------------------------
# Labs, appointments, inventory, scores, survey
# ---------------------------------------------------------------------------
@router.post("/{patient_id}/labs")
async def add_lab(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = dict(await request.form())
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
    if patient is None:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    try:
        with db() as conn:
            result = appt.add_lab_result(conn, patient, form, user["username"])
    except appt.AppointmentError as exc:
        return redirect(f"/patients/{patient_id}", str(exc), "danger")

    if form.get("notify_patient") in ("on", "1", "true") and patient.get("line_user_id"):
        notifications.send_inr_result(patient, result["value"], result["assessment"])
    message = f"บันทึกผล INR {result['value']} ({result['assessment'].label}) เรียบร้อย"
    kind = "success" if result["assessment"].urgency == 0 else "warning"
    return redirect(f"/patients/{patient_id}", message, kind)


@router.post("/{patient_id}/appointments")
async def add_appointment(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = dict(await request.form())
    try:
        with db() as conn:
            appt.create_appointment(conn, patient_id, form, user["username"])
    except appt.AppointmentError as exc:
        return redirect(f"/patients/{patient_id}", str(exc), "danger")
    return redirect(f"/patients/{patient_id}", "บันทึกนัดหมายเรียบร้อย")


@router.post("/{patient_id}/appointments/{appointment_id}/status")
async def update_appointment(
    request: Request,
    patient_id: int,
    appointment_id: int,
    user: dict = Depends(require_clinical),
):
    form = await request.form()
    try:
        with db() as conn:
            appt.set_appointment_status(
                conn, appointment_id, str(form.get("status") or ""), user["username"]
            )
    except appt.AppointmentError as exc:
        return redirect(f"/patients/{patient_id}", str(exc), "danger")
    return redirect(f"/patients/{patient_id}", "อัปเดตสถานะนัดหมายเรียบร้อย")


@router.post("/{patient_id}/inventory")
async def update_inventory(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = await request.form()
    try:
        count = max(0, int(float(form.get("pill_inventory") or 0)))
    except (TypeError, ValueError):
        return redirect(f"/patients/{patient_id}", "จำนวนยาต้องเป็นตัวเลข", "danger")
    with db() as conn:
        conn.execute(
            "UPDATE patients SET pill_inventory=?, updated_at=? WHERE patient_id=?",
            (count, now(), patient_id),
        )
        log_audit(
            conn, "update_inventory", "patient", patient_id, user["username"],
            f"count={count}",
        )
    return redirect(f"/patients/{patient_id}", "บันทึกจำนวนยาคงเหลือเรียบร้อย")


@router.post("/{patient_id}/test-score")
async def add_test_score(
    request: Request, patient_id: int, user: dict = Depends(require_clinical)
):
    form = await request.form()
    try:
        score = float(form.get("score") or 0)
        max_score = float(form.get("max_score") or 100)
    except (TypeError, ValueError):
        return redirect(f"/patients/{patient_id}", "คะแนนต้องเป็นตัวเลข", "danger")
    if max_score <= 0 or not 0 <= score <= max_score:
        return redirect(f"/patients/{patient_id}", "คะแนนไม่อยู่ในช่วงที่ถูกต้อง", "danger")
    test_type = str(form.get("test_type") or "pre")
    if test_type not in ("pre", "post"):
        test_type = "pre"
    with db() as conn:
        conn.execute(
            "INSERT INTO test_scores (patient_id, test_type, score, max_score, taken_at) "
            "VALUES (?,?,?,?,?)",
            (patient_id, test_type, score, max_score, now()),
        )
        log_audit(
            conn, "add_score", "test_scores", patient_id, user["username"],
            f"{test_type}={score}/{max_score}",
        )
    return redirect(f"/patients/{patient_id}", "บันทึกคะแนนแบบทดสอบเรียบร้อย")


@router.get("/{patient_id}/survey")
def survey_form(
    request: Request, patient_id: int, user: dict = Depends(require_user)
):
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
    if patient is None:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    return render(request, "survey_form.html", {
        "user": user, "patient": patient, "today": today(),
    })


@router.post("/{patient_id}/survey")
async def survey_submit(
    request: Request, patient_id: int, user: dict = Depends(require_user)
):
    form = await request.form()

    def score(field: str) -> int:
        try:
            return min(max(int(form.get(field) or 3), 1), 5)
        except (TypeError, ValueError):
            return 3

    with db() as conn:
        conn.execute(
            "INSERT INTO satisfaction_surveys (patient_id, survey_date, ease_of_use, "
            "line_satisfaction, reminder_helpful, comments, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                patient_id, str(form.get("survey_date") or today()),
                score("ease_of_use"), score("line_satisfaction"),
                score("reminder_helpful"),
                str(form.get("comments") or "")[:1000], now(),
            ),
        )
        log_audit(
            conn, "add_survey", "satisfaction_surveys", patient_id, user["username"], ""
        )
    return redirect(f"/patients/{patient_id}", "บันทึกแบบสอบถามเรียบร้อย")


# ---------------------------------------------------------------------------
# Printables
# ---------------------------------------------------------------------------
@router.get("/{patient_id}/qr-sheet")
def qr_sheet(request: Request, patient_id: int, user: dict = Depends(require_user)):
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
        if patient is None:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
        doses = dose_service.upcoming_doses(conn, patient_id, limit=31)
    return render(request, "qr_sheet.html", {
        "user": user, "patient": patient, "doses": doses,
    })


@router.get("/{patient_id}/schedule")
def print_schedule(
    request: Request, patient_id: int, days: int = 30, user: dict = Depends(require_user)
):
    days = min(max(days, 1), 120)
    with read_db() as conn:
        patient = patient_service.get_patient(conn, patient_id)
        if patient is None:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
        doses = dose_service.upcoming_doses(conn, patient_id, limit=days)
        weekly_mg = dose_service.current_weekly_mg(conn, patient_id)
        appointments = appt.upcoming_for_patient(conn, patient_id, limit=3)
    return render(request, "print_schedule.html", {
        "user": user, "patient": patient, "doses": doses,
        "weekly_mg": weekly_mg, "appointments": appointments, "days": days,
    })
