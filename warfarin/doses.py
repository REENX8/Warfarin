"""Medication plan services: generating schedules and confirming doses."""
from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta

from warfarin.audit import log_audit
from warfarin.db import fetch_all, fetch_one, insert_returning_id
from warfarin.time_utils import DATE_FMT, now, now_dt, parse_dt, today

logger = logging.getLogger(__name__)

LATE_THRESHOLD_MINUTES = 120
MAX_PLAN_DAYS = 400
TOKEN_GRACE_DAYS = 1
WEEKDAY_LABELS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

CONFIRM_SOURCES = ("patient", "caregiver", "staff")
DOSE_STATUSES = ("planned", "taken", "late", "missed")


class DoseError(Exception):
    """Raised for user-correctable problems while building a plan."""


def new_dose_token() -> str:
    return secrets.token_urlsafe(18)


def create_plan(
    conn: sqlite3.Connection,
    patient_id: int,
    start_date: str,
    end_date: str,
    day_doses: dict[int, float],
    scheduled_time: str = "18:00",
    pill_description: str = "",
    performed_by: str = "system",
    replace_existing: bool = False,
) -> int:
    """Create one dose row (plus its confirmation token) per day in the range.

    `day_doses` maps Python weekday (0=Monday) to milligrams; a day mapped to
    0 mg is a deliberate rest day and gets no dose row at all.
    """
    try:
        start = datetime.strptime(start_date, DATE_FMT).date()
        end = datetime.strptime(end_date, DATE_FMT).date()
    except (ValueError, TypeError):
        raise DoseError("รูปแบบวันที่ไม่ถูกต้อง")
    if end < start:
        raise DoseError("วันสิ้นสุดต้องไม่ก่อนวันเริ่มต้น")
    if (end - start).days + 1 > MAX_PLAN_DAYS:
        raise DoseError(f"สร้างแผนได้สูงสุด {MAX_PLAN_DAYS} วันต่อครั้ง")
    if not any(float(v or 0) > 0 for v in day_doses.values()):
        raise DoseError("กรุณาระบุขนาดยาอย่างน้อยหนึ่งวัน")

    if replace_existing:
        conn.execute(
            "DELETE FROM dose_tokens WHERE dose_id IN "
            "(SELECT dose_id FROM medication_plan WHERE patient_id=? "
            " AND scheduled_date>=? AND scheduled_date<=? AND status='planned')",
            (patient_id, start_date, end_date),
        )
        conn.execute(
            "DELETE FROM medication_plan WHERE patient_id=? AND scheduled_date>=? "
            "AND scheduled_date<=? AND status='planned'",
            (patient_id, start_date, end_date),
        )

    existing = {
        row["scheduled_date"]
        for row in conn.execute(
            "SELECT scheduled_date FROM medication_plan "
            "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
            (patient_id, start_date, end_date),
        ).fetchall()
    }

    created = 0
    current = start
    timestamp = now()
    while current <= end:
        date_str = current.strftime(DATE_FMT)
        milligrams = float(day_doses.get(current.weekday(), 0) or 0)
        if milligrams <= 0 or date_str in existing:
            current += timedelta(days=1)
            continue
        dose_id = insert_returning_id(
            conn,
            "INSERT INTO medication_plan "
            "(patient_id, scheduled_date, scheduled_time, warfarin_mg, pill_description, "
            " status, created_at) VALUES (?,?,?,?,?,'planned',?)",
            (patient_id, date_str, scheduled_time, milligrams, pill_description, timestamp),
        )
        expires = datetime.combine(
            current + timedelta(days=TOKEN_GRACE_DAYS),
            datetime.max.time(),
        ).replace(microsecond=0)
        conn.execute(
            "INSERT INTO dose_tokens (token_id, dose_id, created_at, expires_at, reminder_count) "
            "VALUES (?,?,?,?,0)",
            (new_dose_token(), dose_id, timestamp, expires.isoformat()),
        )
        created += 1
        current += timedelta(days=1)

    log_audit(
        conn, "create_doses", "medication_plan", patient_id, performed_by,
        f"{start_date}..{end_date} created={created} time={scheduled_time}",
    )
    return created


def lookup_token(conn: sqlite3.Connection, token_id: str) -> dict | None:
    return fetch_one(
        conn,
        "SELECT dt.token_id, dt.dose_id, dt.is_used, dt.expires_at, dt.reminder_count, "
        "mp.scheduled_date, mp.scheduled_time, mp.warfarin_mg, mp.pill_description, "
        "mp.status, p.patient_id AS pid, p.full_name, p.hn, p.line_user_id, "
        "p.access_token, p.pill_inventory "
        "FROM dose_tokens dt "
        "JOIN medication_plan mp ON dt.dose_id=mp.dose_id "
        "JOIN patients p ON mp.patient_id=p.patient_id "
        "WHERE dt.token_id=?",
        (token_id,),
    )


def token_expired(token: dict) -> bool:
    expires = parse_dt(token.get("expires_at"))
    return bool(expires and now_dt() > expires)


def confirm_dose(
    conn: sqlite3.Connection, token: dict, confirm_source: str = "patient"
) -> dict:
    """Mark a dose confirmed. Returns {'ok': bool, 'status'|'reason': ...}.

    The UPDATE is guarded by `is_used=0` so two taps on the same link (or a
    patient and a caregiver confirming at once) can only ever succeed once —
    which matters because the same statement decrements the pill inventory.
    """
    if confirm_source not in CONFIRM_SOURCES:
        confirm_source = "patient"
    if token_expired(token):
        return {"ok": False, "reason": "expired"}

    current = now_dt()
    try:
        scheduled = datetime.strptime(
            f"{token['scheduled_date']} {token['scheduled_time']}", "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        scheduled = current
    late_minutes = max(0, int((current - scheduled).total_seconds() / 60))
    status = "late" if late_minutes > LATE_THRESHOLD_MINUTES else "taken"

    cursor = conn.execute(
        "UPDATE dose_tokens SET is_used=1, used_at=? WHERE token_id=? AND is_used=0",
        (current.isoformat(timespec="seconds"), token["token_id"]),
    )
    if not cursor.rowcount:
        return {"ok": False, "reason": "already_used"}

    conn.execute(
        "UPDATE medication_plan SET status=?, confirmed_at=?, confirmed_by=?, "
        "confirm_source=?, late_minutes=? WHERE dose_id=?",
        (
            status,
            current.isoformat(timespec="seconds"),
            confirm_source,
            confirm_source,
            late_minutes,
            token["dose_id"],
        ),
    )
    conn.execute(
        "UPDATE patients SET pill_inventory=MAX(COALESCE(pill_inventory,0)-1, 0) "
        "WHERE patient_id=? AND COALESCE(pill_inventory,0)>0",
        (token["pid"],),
    )
    log_audit(
        conn, "confirm_dose", "medication_plan", token["dose_id"], confirm_source,
        f"status={status} late={late_minutes}m",
    )
    return {"ok": True, "status": status, "late_minutes": late_minutes}


def override_status(
    conn: sqlite3.Connection, dose_id: int, status: str, performed_by: str
) -> bool:
    """Staff manual correction of a dose status."""
    if status not in DOSE_STATUSES:
        raise DoseError("สถานะไม่ถูกต้อง")
    row = fetch_one(
        conn, "SELECT patient_id, status FROM medication_plan WHERE dose_id=?", (dose_id,)
    )
    if row is None:
        return False
    if status in ("taken", "late"):
        conn.execute(
            "UPDATE medication_plan SET status=?, confirmed_at=?, confirmed_by=?, "
            "confirm_source='staff' WHERE dose_id=?",
            (status, now(), performed_by, dose_id),
        )
        conn.execute(
            "UPDATE dose_tokens SET is_used=1, used_at=? WHERE dose_id=?",
            (now(), dose_id),
        )
    else:
        conn.execute(
            "UPDATE medication_plan SET status=?, confirmed_at=NULL, confirmed_by=NULL, "
            "late_minutes=0 WHERE dose_id=?",
            (status, dose_id),
        )
        conn.execute(
            "UPDATE dose_tokens SET is_used=0, used_at=NULL WHERE dose_id=?", (dose_id,)
        )
    log_audit(
        conn, "override_dose", "medication_plan", dose_id, performed_by,
        f"{row['status']} → {status}",
    )
    return True


def mark_overdue_missed(conn: sqlite3.Connection) -> int:
    """Flip past-due 'planned' doses to 'missed'. Returns rows changed."""
    cursor = conn.execute(
        "UPDATE medication_plan SET status='missed' "
        "WHERE status='planned' AND scheduled_date < ?",
        (today(),),
    )
    return cursor.rowcount or 0


def current_weekly_mg(conn: sqlite3.Connection, patient_id: int) -> float:
    """Total milligrams scheduled over the next 7 days (the weekly dose)."""
    end = (now_dt() + timedelta(days=6)).strftime(DATE_FMT)
    row = conn.execute(
        "SELECT COALESCE(SUM(warfarin_mg), 0) FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
        (patient_id, today(), end),
    ).fetchone()
    total = float(row[0] or 0)
    if total:
        return round(total, 2)
    # No forward plan: fall back to the most recent completed week.
    start = (now_dt() - timedelta(days=7)).strftime(DATE_FMT)
    row = conn.execute(
        "SELECT COALESCE(SUM(warfarin_mg), 0) FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<?",
        (patient_id, start, today()),
    ).fetchone()
    return round(float(row[0] or 0), 2)


def weekly_pattern(conn: sqlite3.Connection, patient_id: int) -> dict[int, float]:
    """Most recent milligram value seen for each weekday, for form prefill."""
    rows = fetch_all(
        conn,
        "SELECT scheduled_date, warfarin_mg FROM medication_plan "
        "WHERE patient_id=? ORDER BY scheduled_date DESC LIMIT 60",
        (patient_id,),
    )
    pattern: dict[int, float] = {}
    for row in rows:
        try:
            weekday = datetime.strptime(row["scheduled_date"], DATE_FMT).weekday()
        except (ValueError, TypeError):
            continue
        pattern.setdefault(weekday, float(row["warfarin_mg"] or 0))
    return pattern


def upcoming_doses(conn: sqlite3.Connection, patient_id: int, limit: int = 31) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT mp.*, dt.token_id FROM medication_plan mp "
        "JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
        "WHERE mp.patient_id=? AND mp.scheduled_date>=? "
        "ORDER BY mp.scheduled_date, mp.scheduled_time LIMIT ?",
        (patient_id, today(), limit),
    )


def plan_coverage_end(conn: sqlite3.Connection, patient_id: int) -> str | None:
    """Last scheduled date, used to warn when a plan is about to run out."""
    row = conn.execute(
        "SELECT MAX(scheduled_date) FROM medication_plan WHERE patient_id=?",
        (patient_id,),
    ).fetchone()
    return row[0] if row and row[0] else None
