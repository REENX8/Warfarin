"""JSON endpoints for charts and AJAX.

These return patient data, so they require a staff session — the pre-2.0
versions were public, which let anyone enumerate INR history by patient id.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from warfarin import notifications
from warfarin import patients as patient_service
from warfarin.adherence import (
    compute_adherence_bulk,
    daily_adherence_series,
    heatmap_series,
)
from warfarin.clinical import screen_medication_text
from warfarin.db import fetch_all, read_db
from warfarin.deps import csrf_protect, enforce_rate_limit, require_user
from warfarin.doses import current_weekly_mg
from warfarin.time_utils import today

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(csrf_protect)])


def _require_patient(conn, patient_id: int) -> dict:
    patient = patient_service.get_patient(conn, patient_id)
    if patient is None:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    return patient


@router.get("/patients/{patient_id}/inr-data")
def inr_data(patient_id: int, user: dict = Depends(require_user)):
    with read_db() as conn:
        patient = _require_patient(conn, patient_id)
        rows = fetch_all(
            conn,
            "SELECT test_date AS date, value, in_range FROM lab_results "
            "WHERE patient_id=? AND lab_name='INR' ORDER BY test_date",
            (patient_id,),
        )
    return {
        "target_min": patient.get("target_inr_min"),
        "target_max": patient.get("target_inr_max"),
        "points": rows,
    }


@router.get("/patients/{patient_id}/adherence-data")
def adherence_data(patient_id: int, days: int = 30, user: dict = Depends(require_user)):
    days = min(max(days, 7), 365)
    with read_db() as conn:
        _require_patient(conn, patient_id)
        return daily_adherence_series(conn, patient_id, days)


@router.get("/patients/{patient_id}/heatmap-data")
def heatmap_data(patient_id: int, days: int = 90, user: dict = Depends(require_user)):
    days = min(max(days, 30), 365)
    with read_db() as conn:
        _require_patient(conn, patient_id)
        return heatmap_series(conn, patient_id, days)


@router.get("/dashboard-stats")
def dashboard_stats(user: dict = Depends(require_user)):
    with read_db() as conn:
        row = conn.execute(
            "SELECT "
            " SUM(CASE WHEN status IN ('taken','late') THEN 1 ELSE 0 END) AS taken, "
            " SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed, "
            " SUM(CASE WHEN status='planned' THEN 1 ELSE 0 END) AS pending "
            "FROM medication_plan WHERE scheduled_date=?",
            (today(),),
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM patients WHERE active=1"
        ).fetchone()[0]
    return {
        "today_taken": row["taken"] or 0,
        "today_missed": row["missed"] or 0,
        "today_pending": row["pending"] or 0,
        "active_patients": active,
    }


@router.get("/patients/{patient_id}/interactions")
def interaction_check(
    patient_id: int, q: str = "", user: dict = Depends(require_user)
):
    """Screen free text (or the patient's own notes) for known interactions."""
    with read_db() as conn:
        patient = _require_patient(conn, patient_id)
    haystack = q.strip() or " ".join(
        str(patient.get(field) or "")
        for field in ("chronic_conditions", "diagnosis", "notes", "allergies")
    )
    return {"query": haystack[:500], "hits": screen_medication_text(haystack)}


@router.get("/broadcast-preview")
def broadcast_preview(
    request: Request, target: str = "all", user: dict = Depends(require_user)
):
    from warfarin.routers.line_bp import filter_broadcast_targets

    with read_db() as conn:
        patients = patient_service.linked_patients(conn)
        selected = filter_broadcast_targets(conn, patients, target)
    return {"target": target, "count": len(selected), "eligible": len(patients)}


@router.get("/patients/{patient_id}/summary")
def patient_summary(patient_id: int, user: dict = Depends(require_user)):
    with read_db() as conn:
        patient = _require_patient(conn, patient_id)
        adherence = compute_adherence_bulk(conn, [patient_id], 30)[patient_id]
        weekly = current_weekly_mg(conn, patient_id)
    return {
        "patient_id": patient_id,
        "full_name": patient["full_name"],
        "hn": patient.get("hn"),
        "adherence_30d": adherence,
        "weekly_mg": weekly,
        "next_inr_date": patient.get("next_inr_date"),
    }


@router.get("/health/notifications")
def notification_health(days: int = 7, user: dict = Depends(require_user)):
    days = min(max(days, 1), 90)
    return notifications.delivery_stats(days)


@router.get("/lookup")
def quick_lookup(request: Request, q: str = "", user: dict = Depends(require_user)):
    """Type-ahead patient search used by the top navigation."""
    enforce_rate_limit(request, "lookup", 120, 60)
    query = q.strip()[:40]
    if len(query) < 2:
        return []
    with read_db() as conn:
        rows = patient_service.search_patients(conn, query, "all", limit=10)
    return [
        {
            "patient_id": row["patient_id"],
            "full_name": row["full_name"],
            "hn": row.get("hn"),
            "active": row.get("active"),
        }
        for row in rows
    ]
