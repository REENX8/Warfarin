"""Clinic dashboard and operational worklists."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from warfarin import symptoms
from warfarin.adherence import (
    compute_adherence_bulk,
    compute_at_risk,
    last_inr_bulk,
)
from warfarin.appointments import clinic_schedule
from warfarin.clinical import days_of_stock
from warfarin.config import get_settings
from warfarin.db import fetch_all, read_db, scalar
from warfarin.deps import csrf_protect, require_user
from warfarin.doses import current_weekly_mg
from warfarin.line_service import push_enabled
from warfarin.notifications import delivery_stats
from warfarin.patients import active_patients, overdue_inr
from warfarin.templating import render
from warfarin.time_utils import today

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"], dependencies=[Depends(csrf_protect)])


def _today_counts(conn) -> dict:
    row = conn.execute(
        "SELECT "
        " SUM(CASE WHEN status IN ('taken','late') THEN 1 ELSE 0 END) AS taken, "
        " SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed, "
        " SUM(CASE WHEN status='planned' THEN 1 ELSE 0 END) AS pending, "
        " COUNT(*) AS total "
        "FROM medication_plan WHERE scheduled_date=?",
        (today(),),
    ).fetchone()
    return {
        "taken": row["taken"] or 0,
        "missed": row["missed"] or 0,
        "pending": row["pending"] or 0,
        "total": row["total"] or 0,
    }


@router.get("/dashboard")
def dashboard(request: Request, user: dict = Depends(require_user)):
    settings = get_settings()
    with read_db() as conn:
        patients = active_patients(conn)
        patient_ids = [p["patient_id"] for p in patients]

        total_patients = int(scalar(conn, "SELECT COUNT(*) FROM patients"))
        counts = _today_counts(conn)
        adherence = compute_adherence_bulk(conn, patient_ids, 7)
        percentages = [
            adherence[pid]["percent"] for pid in patient_ids if adherence[pid]["total"]
        ]
        adherence_avg = (
            round(sum(percentages) / len(percentages), 1) if percentages else 0
        )
        at_risk = compute_at_risk(conn, patients, limit=20)

        recent = fetch_all(
            conn,
            "SELECT mp.*, p.full_name, p.hn FROM medication_plan mp "
            "JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.confirmed_at IS NOT NULL AND mp.status IN ('taken','late') "
            "ORDER BY mp.confirmed_at DESC LIMIT 10",
        )
        urgent_symptoms = symptoms.list_reports(status="open", limit=6)
        urgent_symptoms = [s for s in urgent_symptoms if (s.get("severity") or 0) >= 3]

        line_linked = int(
            scalar(
                conn,
                "SELECT COUNT(*) FROM patients WHERE active=1 "
                "AND line_user_id IS NOT NULL AND line_user_id<>''",
            )
        )
        inr_overdue = overdue_inr(conn)
        upcoming = clinic_schedule(conn, days=7)
        last_inr = last_inr_bulk(conn, patient_ids)

        low_stock = []
        for patient in patients:
            inventory = int(patient.get("pill_inventory") or 0)
            if inventory <= 0:
                continue
            days_left = days_of_stock(
                inventory, current_weekly_mg(conn, patient["patient_id"])
            )
            if days_left is not None and days_left <= settings.low_stock_threshold:
                low_stock.append({"patient": patient, "days_left": days_left})
        low_stock.sort(key=lambda item: item["days_left"])

    inr_out_of_range = sum(
        1 for value in last_inr.values() if value and not value["in_range"]
    )

    return render(request, "dashboard.html", {
        "user": user,
        "total_patients": total_patients,
        "active_patients": len(patients),
        "today_taken": counts["taken"],
        "today_missed": counts["missed"],
        "today_pending": counts["pending"],
        "today_total": counts["total"],
        "adherence_avg": adherence_avg,
        "at_risk_patients": at_risk,
        "recent_activity": recent,
        "urgent_symptoms": urgent_symptoms,
        "open_symptom_count": symptoms.open_count(),
        "line_linked": line_linked,
        "line_configured": push_enabled(),
        "inr_overdue": inr_overdue[:10],
        "inr_overdue_count": len(inr_overdue),
        "inr_out_of_range": inr_out_of_range,
        "upcoming_appointments": upcoming[:10],
        "low_stock": low_stock[:10],
        "delivery": delivery_stats(7),
    })


@router.get("/dashboard/at-risk")
def at_risk_page(request: Request, user: dict = Depends(require_user)):
    with read_db() as conn:
        patients = active_patients(conn)
        at_risk = compute_at_risk(conn, patients)
    return render(request, "at_risk.html", {"user": user, "at_risk": at_risk})


@router.get("/dashboard/missed-today")
def missed_today_page(request: Request, user: dict = Depends(require_user)):
    with read_db() as conn:
        rows = fetch_all(
            conn,
            "SELECT mp.*, p.full_name, p.hn, p.phone, p.line_user_id "
            "FROM medication_plan mp JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status IN ('planned','missed') AND p.active=1 "
            "ORDER BY mp.status DESC, p.full_name",
            (today(),),
        )
    return render(request, "missed_today.html", {"user": user, "rows": rows})


@router.get("/appointments")
def appointments_page(
    request: Request, days: int = 14, user: dict = Depends(require_user)
):
    days = min(max(days, 1), 90)
    with read_db() as conn:
        schedule = clinic_schedule(conn, days=days)
        overdue = overdue_inr(conn)
    grouped: dict[str, list] = {}
    for appointment in schedule:
        grouped.setdefault(appointment["appointment_date"], []).append(appointment)
    return render(request, "appointments.html", {
        "user": user,
        "grouped": sorted(grouped.items()),
        "days": days,
        "overdue": overdue,
    })
