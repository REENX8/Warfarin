"""Patient record services: validation, persistence and lookups."""
from __future__ import annotations

import logging
import re
import sqlite3

from warfarin.audit import log_audit
from warfarin.clinical import INDICATIONS
from warfarin.db import fetch_all, fetch_one, insert_returning_id, scalar
from warfarin.security import new_access_token
from warfarin.time_utils import DATE_FMT, now, parse_date, today

logger = logging.getLogger(__name__)

HN_RE = re.compile(r"^[A-Za-z0-9\-/.]{1,32}$")
PHONE_RE = re.compile(r"^[0-9\-+ ]{6,20}$")
LINE_ID_RE = re.compile(r"^U[0-9a-f]{32}$")

PATIENT_FIELDS = (
    "hn", "full_name", "birth_date", "age_years", "weight_kg", "phone",
    "line_user_id", "chronic_conditions", "diagnosis", "target_inr_min",
    "target_inr_max", "sex", "indication", "warfarin_start_date", "allergies",
    "notes", "next_inr_date", "inr_interval_days",
)


class ValidationError(Exception):
    """Collects field-level validation messages for the form to render."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _to_float(value, field: str, errors: dict, minimum=None, maximum=None, default=None):
    raw = (value or "").strip() if isinstance(value, str) else value
    if raw in (None, ""):
        return default
    try:
        number = float(raw)
    except (TypeError, ValueError):
        errors[field] = "ต้องเป็นตัวเลข"
        return default
    if minimum is not None and number < minimum:
        errors[field] = f"ต้องไม่น้อยกว่า {minimum}"
        return default
    if maximum is not None and number > maximum:
        errors[field] = f"ต้องไม่เกิน {maximum}"
        return default
    return number


def _to_int(value, field: str, errors: dict, minimum=None, maximum=None, default=None):
    number = _to_float(value, field, errors, minimum, maximum, None)
    return default if number is None else int(number)


def clean_patient_form(form: dict, *, patient_id: int | None = None) -> dict:
    """Validate and normalise a patient form. Raises ValidationError."""
    errors: dict[str, str] = {}
    data: dict = {}

    full_name = (form.get("full_name") or "").strip()
    if not full_name:
        errors["full_name"] = "กรุณากรอกชื่อ-นามสกุล"
    elif len(full_name) > 120:
        errors["full_name"] = "ชื่อยาวเกินไป"
    data["full_name"] = full_name

    hn = (form.get("hn") or "").strip()
    if hn and not HN_RE.match(hn):
        errors["hn"] = "HN ใช้ได้เฉพาะตัวอักษร ตัวเลข และ - / ."
    data["hn"] = hn or None

    phone = (form.get("phone") or "").strip()
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "รูปแบบเบอร์โทรไม่ถูกต้อง"
    data["phone"] = phone or None

    line_user_id = (form.get("line_user_id") or "").strip()
    if line_user_id and not LINE_ID_RE.match(line_user_id):
        errors["line_user_id"] = "LINE User ID ต้องขึ้นต้นด้วย U ตามด้วยตัวอักษร 32 ตัว"
    data["line_user_id"] = line_user_id or None

    birth_date = (form.get("birth_date") or "").strip()
    if birth_date and parse_date(birth_date) is None:
        errors["birth_date"] = "รูปแบบวันเกิดไม่ถูกต้อง (ปปปป-ดด-วว)"
    data["birth_date"] = birth_date or None

    data["age_years"] = _to_int(form.get("age_years"), "age_years", errors, 0, 130)
    data["weight_kg"] = _to_float(form.get("weight_kg"), "weight_kg", errors, 1, 400)

    indication = (form.get("indication") or "").strip()
    if indication and indication not in INDICATIONS:
        errors["indication"] = "ข้อบ่งใช้ไม่ถูกต้อง"
    data["indication"] = indication or None

    inr_min = _to_float(form.get("target_inr_min"), "target_inr_min", errors, 0.8, 6.0, 2.0)
    inr_max = _to_float(form.get("target_inr_max"), "target_inr_max", errors, 0.8, 6.0, 3.0)
    if inr_min is not None and inr_max is not None and inr_min >= inr_max:
        errors["target_inr_max"] = "ค่าสูงสุดต้องมากกว่าค่าต่ำสุด"
    data["target_inr_min"] = inr_min
    data["target_inr_max"] = inr_max

    sex = (form.get("sex") or "").strip()
    data["sex"] = sex if sex in ("male", "female") else None

    warfarin_start = (form.get("warfarin_start_date") or "").strip()
    if warfarin_start and parse_date(warfarin_start) is None:
        errors["warfarin_start_date"] = "รูปแบบวันที่ไม่ถูกต้อง"
    data["warfarin_start_date"] = warfarin_start or None

    next_inr = (form.get("next_inr_date") or "").strip()
    if next_inr and parse_date(next_inr) is None:
        errors["next_inr_date"] = "รูปแบบวันที่ไม่ถูกต้อง"
    data["next_inr_date"] = next_inr or None

    data["inr_interval_days"] = _to_int(
        form.get("inr_interval_days"), "inr_interval_days", errors, 1, 180, 28
    )

    for field, limit in (
        ("chronic_conditions", 500), ("diagnosis", 500),
        ("allergies", 500), ("notes", 2000),
    ):
        value = (form.get(field) or "").strip()
        if len(value) > limit:
            errors[field] = f"ข้อความยาวเกิน {limit} ตัวอักษร"
        data[field] = value or None

    if errors:
        raise ValidationError(errors)
    return data


def hn_taken(conn: sqlite3.Connection, hn: str | None, exclude_id: int | None = None) -> bool:
    if not hn:
        return False
    sql = "SELECT COUNT(*) FROM patients WHERE hn=?"
    params: list = [hn]
    if exclude_id:
        sql += " AND patient_id<>?"
        params.append(exclude_id)
    return scalar(conn, sql, tuple(params)) > 0


def line_id_taken(
    conn: sqlite3.Connection, line_user_id: str | None, exclude_id: int | None = None
) -> bool:
    if not line_user_id:
        return False
    sql = "SELECT COUNT(*) FROM patients WHERE line_user_id=?"
    params: list = [line_user_id]
    if exclude_id:
        sql += " AND patient_id<>?"
        params.append(exclude_id)
    return scalar(conn, sql, tuple(params)) > 0


def create_patient(conn: sqlite3.Connection, data: dict, performed_by: str) -> int:
    if hn_taken(conn, data.get("hn")):
        raise ValidationError({"hn": "HN นี้มีอยู่ในระบบแล้ว"})
    if line_id_taken(conn, data.get("line_user_id")):
        raise ValidationError({"line_user_id": "LINE User ID นี้ถูกใช้กับผู้ป่วยรายอื่นแล้ว"})
    timestamp = now()
    columns = list(PATIENT_FIELDS) + ["active", "created_at", "updated_at", "access_token"]
    values = [data.get(field) for field in PATIENT_FIELDS] + [
        1, timestamp, timestamp, new_access_token()
    ]
    placeholders = ",".join("?" * len(columns))
    patient_id = insert_returning_id(
        conn,
        f"INSERT INTO patients ({','.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )
    log_audit(
        conn, "create", "patient", patient_id, performed_by,
        f"เพิ่มผู้ป่วย {data.get('full_name')} HN={data.get('hn') or '-'}",
    )
    return patient_id


def update_patient(
    conn: sqlite3.Connection, patient_id: int, data: dict, performed_by: str
) -> None:
    if hn_taken(conn, data.get("hn"), exclude_id=patient_id):
        raise ValidationError({"hn": "HN นี้มีอยู่ในระบบแล้ว"})
    if line_id_taken(conn, data.get("line_user_id"), exclude_id=patient_id):
        raise ValidationError({"line_user_id": "LINE User ID นี้ถูกใช้กับผู้ป่วยรายอื่นแล้ว"})
    assignments = ",".join(f"{field}=?" for field in PATIENT_FIELDS)
    values = [data.get(field) for field in PATIENT_FIELDS] + [now(), patient_id]
    conn.execute(
        f"UPDATE patients SET {assignments}, updated_at=? WHERE patient_id=?",
        tuple(values),
    )
    ensure_access_token(conn, patient_id)
    log_audit(conn, "update", "patient", patient_id, performed_by, "แก้ไขข้อมูลผู้ป่วย")


def ensure_access_token(conn: sqlite3.Connection, patient_id: int) -> str:
    """Guarantee the patient has a portal token (older rows predate the column)."""
    row = fetch_one(
        conn, "SELECT access_token FROM patients WHERE patient_id=?", (patient_id,)
    )
    if row and row.get("access_token"):
        return row["access_token"]
    token = new_access_token()
    conn.execute(
        "UPDATE patients SET access_token=? WHERE patient_id=?", (token, patient_id)
    )
    return token


def backfill_access_tokens(conn: sqlite3.Connection) -> int:
    """Give every legacy patient row a portal token. Returns rows updated."""
    rows = conn.execute(
        "SELECT patient_id FROM patients WHERE access_token IS NULL OR access_token=''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE patients SET access_token=? WHERE patient_id=?",
            (new_access_token(), row[0]),
        )
    return len(rows)


def get_patient(conn: sqlite3.Connection, patient_id: int) -> dict | None:
    return fetch_one(conn, "SELECT * FROM patients WHERE patient_id=?", (patient_id,))


def get_patient_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    return fetch_one(
        conn, "SELECT * FROM patients WHERE access_token=? AND active=1", (token,)
    )


def search_patients(
    conn: sqlite3.Connection,
    query: str = "",
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM patients WHERE 1=1"
    params: list = []
    if status == "active":
        sql += " AND active=1"
    elif status == "inactive":
        sql += " AND active=0"
    if query:
        sql += " AND (full_name LIKE ? OR hn LIKE ? OR phone LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like, like])
    sql += " ORDER BY active DESC, full_name LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return fetch_all(conn, sql, tuple(params))


def count_patients(conn: sqlite3.Connection, query: str = "", status: str = "active") -> int:
    sql = "SELECT COUNT(*) FROM patients WHERE 1=1"
    params: list = []
    if status == "active":
        sql += " AND active=1"
    elif status == "inactive":
        sql += " AND active=0"
    if query:
        sql += " AND (full_name LIKE ? OR hn LIKE ? OR phone LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like, like])
    return int(scalar(conn, sql, tuple(params)))


def active_patients(conn: sqlite3.Connection) -> list[dict]:
    return fetch_all(
        conn, "SELECT * FROM patients WHERE active=1 ORDER BY full_name"
    )


def linked_patients(conn: sqlite3.Connection) -> list[dict]:
    """Active patients with a usable LINE account."""
    return fetch_all(
        conn,
        "SELECT * FROM patients WHERE active=1 AND line_user_id IS NOT NULL "
        "AND line_user_id<>'' ORDER BY full_name",
    )


def set_active(
    conn: sqlite3.Connection, patient_id: int, active: bool, performed_by: str
) -> None:
    conn.execute(
        "UPDATE patients SET active=?, updated_at=? WHERE patient_id=?",
        (1 if active else 0, now(), patient_id),
    )
    log_audit(
        conn,
        "reactivate" if active else "deactivate",
        "patient", patient_id, performed_by,
        "เปิดใช้งานผู้ป่วย" if active else "ปิดใช้งานผู้ป่วย (soft delete)",
    )


def save_caregiver(conn: sqlite3.Connection, patient_id: int, form: dict) -> None:
    """Upsert the primary caregiver; blank name removes them."""
    name = (form.get("caregiver_name") or "").strip()
    existing = fetch_one(
        conn,
        "SELECT caregiver_id FROM caregivers WHERE patient_id=? ORDER BY caregiver_id LIMIT 1",
        (patient_id,),
    )
    if not name:
        if existing:
            conn.execute(
                "DELETE FROM caregivers WHERE caregiver_id=?", (existing["caregiver_id"],)
            )
        return
    line_id = (form.get("caregiver_line") or "").strip() or None
    notify = 1 if form.get("caregiver_notify") in ("on", "1", "true", True) else 0
    values = (
        name,
        (form.get("caregiver_phone") or "").strip() or None,
        line_id,
        (form.get("caregiver_relationship") or "").strip() or None,
        notify,
    )
    if existing:
        conn.execute(
            "UPDATE caregivers SET name=?, phone=?, line_user_id=?, relationship=?, "
            "notify_enabled=? WHERE caregiver_id=?",
            (*values, existing["caregiver_id"]),
        )
    else:
        conn.execute(
            "INSERT INTO caregivers (patient_id, name, phone, line_user_id, relationship, "
            "notify_enabled, created_at) VALUES (?,?,?,?,?,?,?)",
            (patient_id, *values, now()),
        )


def caregivers_for(conn: sqlite3.Connection, patient_id: int) -> list[dict]:
    return fetch_all(
        conn, "SELECT * FROM caregivers WHERE patient_id=? ORDER BY caregiver_id", (patient_id,)
    )


def notifiable_caregivers(conn: sqlite3.Connection, patient_id: int) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM caregivers WHERE patient_id=? AND notify_enabled=1 "
        "AND line_user_id IS NOT NULL AND line_user_id<>''",
        (patient_id,),
    )


def patients_due_for_inr(conn: sqlite3.Connection, within_days: int) -> list[dict]:
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    horizon = (now_dt() + timedelta(days=within_days)).strftime(DATE_FMT)
    return fetch_all(
        conn,
        "SELECT * FROM patients WHERE active=1 AND next_inr_date IS NOT NULL "
        "AND next_inr_date<>'' AND next_inr_date<=? ORDER BY next_inr_date",
        (horizon,),
    )


def overdue_inr(conn: sqlite3.Connection) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT * FROM patients WHERE active=1 AND next_inr_date IS NOT NULL "
        "AND next_inr_date<>'' AND next_inr_date < ? ORDER BY next_inr_date",
        (today(),),
    )
