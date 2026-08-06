"""INR / clinic appointments and lab results.

Warfarin management is driven by the INR recheck interval, so appointments
are first-class here rather than a note on the patient record.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from warfarin.audit import log_audit
from warfarin.clinical import APPOINTMENT_STATUSES, APPOINTMENT_TYPES, assess_inr
from warfarin.db import fetch_all, fetch_one, insert_returning_id
from warfarin.time_utils import DATE_FMT, now, now_dt, parse_date, parse_time, today

logger = logging.getLogger(__name__)


class AppointmentError(Exception):
    """Raised when appointment input cannot be accepted."""


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
def create_appointment(
    conn: sqlite3.Connection, patient_id: int, form: dict, performed_by: str
) -> int:
    date_str = (form.get("appointment_date") or "").strip()
    if parse_date(date_str) is None:
        raise AppointmentError("กรุณาระบุวันที่นัดให้ถูกต้อง")
    appointment_type = (form.get("appointment_type") or "inr").strip()
    if appointment_type not in APPOINTMENT_TYPES:
        appointment_type = "inr"
    time_str = (form.get("appointment_time") or "").strip()
    time_value = parse_time(time_str, "") if time_str else None

    appointment_id = insert_returning_id(
        conn,
        "INSERT INTO appointments (patient_id, appointment_date, appointment_time, "
        "appointment_type, location, notes, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,'scheduled',?,?)",
        (
            patient_id, date_str, time_value or None, appointment_type,
            (form.get("location") or "").strip()[:200] or None,
            (form.get("notes") or "").strip()[:500] or None,
            performed_by, now(),
        ),
    )
    if appointment_type == "inr":
        conn.execute(
            "UPDATE patients SET next_inr_date=?, updated_at=? WHERE patient_id=?",
            (date_str, now(), patient_id),
        )
    log_audit(
        conn, "create_appointment", "appointments", appointment_id, performed_by,
        f"patient={patient_id} {appointment_type} {date_str}",
    )
    return appointment_id


def set_appointment_status(
    conn: sqlite3.Connection, appointment_id: int, status: str, performed_by: str
) -> bool:
    if status not in APPOINTMENT_STATUSES:
        raise AppointmentError("สถานะนัดหมายไม่ถูกต้อง")
    cursor = conn.execute(
        "UPDATE appointments SET status=? WHERE appointment_id=?", (status, appointment_id)
    )
    if not cursor.rowcount:
        return False
    log_audit(
        conn, "update_appointment", "appointments", appointment_id, performed_by,
        f"status={status}",
    )
    return True


def upcoming_for_patient(
    conn: sqlite3.Connection, patient_id: int, limit: int = 10
) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM appointments WHERE patient_id=? AND appointment_date>=? "
        "AND status='scheduled' ORDER BY appointment_date LIMIT ?",
        (patient_id, today(), limit),
    )


def history_for_patient(
    conn: sqlite3.Connection, patient_id: int, limit: int = 20
) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM appointments WHERE patient_id=? ORDER BY appointment_date DESC LIMIT ?",
        (patient_id, limit),
    )


def due_within(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Scheduled appointments landing in the next `days` days, not yet reminded."""
    horizon = (now_dt() + timedelta(days=days)).strftime(DATE_FMT)
    return fetch_all(
        conn,
        "SELECT a.*, p.full_name, p.line_user_id, p.hn FROM appointments a "
        "JOIN patients p ON a.patient_id=p.patient_id "
        "WHERE a.status='scheduled' AND a.appointment_date>=? AND a.appointment_date<=? "
        "AND p.active=1 AND (a.reminded_at IS NULL OR a.reminded_at='') "
        "ORDER BY a.appointment_date",
        (today(), horizon),
    )


def mark_reminded(conn: sqlite3.Connection, appointment_id: int) -> None:
    conn.execute(
        "UPDATE appointments SET reminded_at=? WHERE appointment_id=?",
        (now(), appointment_id),
    )


def mark_past_appointments_missed(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE appointments SET status='missed' "
        "WHERE status='scheduled' AND appointment_date < ?",
        (today(),),
    )
    return cursor.rowcount or 0


def clinic_schedule(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    horizon = (now_dt() + timedelta(days=days)).strftime(DATE_FMT)
    return fetch_all(
        conn,
        "SELECT a.*, p.full_name, p.hn, p.phone FROM appointments a "
        "JOIN patients p ON a.patient_id=p.patient_id "
        "WHERE a.status='scheduled' AND a.appointment_date>=? AND a.appointment_date<=? "
        "ORDER BY a.appointment_date, a.appointment_time",
        (today(), horizon),
    )


# ---------------------------------------------------------------------------
# Lab results
# ---------------------------------------------------------------------------
def add_lab_result(
    conn: sqlite3.Connection, patient: dict, form: dict, performed_by: str
) -> dict:
    """Record an INR result, auto-schedule the recheck and return the assessment."""
    raw_value = (form.get("value") or "").strip()
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise AppointmentError("กรุณากรอกค่า INR เป็นตัวเลข")
    if not 0.5 <= value <= 20:
        raise AppointmentError("ค่า INR ต้องอยู่ระหว่าง 0.5 ถึง 20")

    test_date = (form.get("test_date") or "").strip() or today()
    if parse_date(test_date) is None:
        raise AppointmentError("รูปแบบวันที่ตรวจไม่ถูกต้อง")

    lo = patient.get("target_inr_min") or 2.0
    hi = patient.get("target_inr_max") or 3.0
    assessment = assess_inr(value, lo, hi)
    in_range = 1 if assessment.band == "in_range" else 0

    result_id = insert_returning_id(
        conn,
        "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, "
        "notes, created_at, recorded_by, action_taken) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            patient["patient_id"], (form.get("lab_name") or "INR").strip()[:40],
            value, test_date, in_range,
            (form.get("notes") or "").strip()[:500] or None,
            now(), performed_by,
            (form.get("action_taken") or "").strip()[:500] or None,
        ),
    )

    next_date = (form.get("next_inr_date") or "").strip()
    if next_date and parse_date(next_date):
        conn.execute(
            "UPDATE patients SET next_inr_date=?, updated_at=? WHERE patient_id=?",
            (next_date, now(), patient["patient_id"]),
        )
    log_audit(
        conn, "add_lab", "lab_results", result_id, performed_by,
        f"patient={patient['patient_id']} INR={value} band={assessment.band}",
    )
    return {"result_id": result_id, "value": value, "assessment": assessment}


def labs_for_patient(
    conn: sqlite3.Connection, patient_id: int, limit: int = 100
) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM lab_results WHERE patient_id=? ORDER BY test_date DESC, result_id DESC "
        "LIMIT ?",
        (patient_id, limit),
    )


def latest_lab(conn: sqlite3.Connection, patient_id: int) -> dict | None:
    return fetch_one(
        conn,
        "SELECT * FROM lab_results WHERE patient_id=? AND lab_name='INR' "
        "ORDER BY test_date DESC, result_id DESC LIMIT 1",
        (patient_id,),
    )


# ---------------------------------------------------------------------------
# Dose adjustments
# ---------------------------------------------------------------------------
def record_dose_adjustment(
    conn: sqlite3.Connection,
    patient_id: int,
    previous_weekly_mg: float,
    new_weekly_mg: float,
    inr_value: float | None,
    reason: str,
    performed_by: str,
) -> int:
    adjustment_id = insert_returning_id(
        conn,
        "INSERT INTO dose_adjustments (patient_id, effective_date, previous_weekly_mg, "
        "new_weekly_mg, inr_value, reason, adjusted_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            patient_id, today(), previous_weekly_mg, new_weekly_mg, inr_value,
            (reason or "")[:500], performed_by, now(),
        ),
    )
    log_audit(
        conn, "dose_adjustment", "dose_adjustments", adjustment_id, performed_by,
        f"patient={patient_id} {previous_weekly_mg} → {new_weekly_mg} mg/สัปดาห์",
    )
    return adjustment_id


def adjustments_for_patient(
    conn: sqlite3.Connection, patient_id: int, limit: int = 20
) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM dose_adjustments WHERE patient_id=? "
        "ORDER BY effective_date DESC, adjustment_id DESC LIMIT ?",
        (patient_id, limit),
    )
