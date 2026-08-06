"""Symptom reports: intake, triage, ticket codes and pharmacist replies.

The ticket-code workflow is ported from the TB tracker: each open report gets
a short code (A01) that a pharmacist can quote from LINE to reply, without
needing to open the web app.
"""
from __future__ import annotations

import logging
import sqlite3
import string

from warfarin import line_service as ls
from warfarin.audit import log_audit
from warfarin.clinical import SYMPTOM_STATUS_LABELS, symptom_labels, triage_symptom
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, fetch_one, insert_returning_id, read_db, scalar
from warfarin.notifications import log_notification, notify_patient
from warfarin.time_utils import now, thai_date, today

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("new", "replied")


# ---------------------------------------------------------------------------
# Ticket codes
# ---------------------------------------------------------------------------
def _all_codes():
    for letter in string.ascii_uppercase:
        for number in range(1, 100):
            yield f"{letter}{number:02d}"


def allocate_ticket_code(conn: sqlite3.Connection) -> str:
    """First short code not held by an unresolved report.

    A code stays reserved until its report is resolved, so a reply can only
    ever route to one open report.
    """
    used = {
        row[0]
        for row in conn.execute(
            "SELECT ticket_code FROM symptom_reports "
            "WHERE status<>'resolved' AND ticket_code IS NOT NULL"
        ).fetchall()
        if row[0]
    }
    for code in _all_codes():
        if code not in used:
            return code
    total = scalar(conn, "SELECT COUNT(*) FROM symptom_reports")
    return f"Z{int(total) % 100:02d}"


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------
def create_report(
    conn: sqlite3.Connection, patient: dict, form: dict, source: str = "patient"
) -> dict:
    """Store a symptom report, allocate its ticket and derive the auto-reply."""
    report = {
        "bleeding": 1 if form.get("bleeding") else 0,
        "bruising": 1 if form.get("bruising") else 0,
        "headache": 1 if form.get("headache") else 0,
        "dizziness": 1 if form.get("dizziness") else 0,
        "nausea": 1 if form.get("nausea") else 0,
        "other": (form.get("other") or "").strip()[:500],
    }
    try:
        severity = int(form.get("severity") or 1)
    except (TypeError, ValueError):
        severity = 1
    report["severity"] = min(max(severity, 1), 5)

    triage = triage_symptom(report)
    ticket = allocate_ticket_code(conn)
    report_date = (form.get("report_date") or "").strip() or today()

    report_id = insert_returning_id(
        conn,
        "INSERT INTO symptom_reports "
        "(patient_id, report_date, bleeding, bruising, headache, dizziness, nausea, "
        " other, severity, source, created_at, status, ticket_code, auto_response) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
        (
            patient["patient_id"], report_date,
            report["bleeding"], report["bruising"], report["headache"],
            report["dizziness"], report["nausea"], report["other"],
            report["severity"], source, now(), ticket, triage["auto_response"],
        ),
    )
    log_audit(
        conn, "symptom_report", "symptom_reports", report_id, source,
        f"severity={report['severity']} ticket={ticket} urgent={triage['urgent']}",
    )
    return {
        "report_id": report_id,
        "ticket_code": ticket,
        "severity": report["severity"],
        "urgent": triage["urgent"],
        "labels": triage["labels"],
        "auto_response": triage["auto_response"],
    }


def acknowledge_patient(patient: dict, result: dict) -> None:
    """Send the automatic reply back to the patient over LINE."""
    if not patient.get("line_user_id"):
        return
    notify_patient(patient, result["auto_response"], "symptom_ack")


def notify_staff(patient: dict, result: dict) -> None:
    """Push a new-symptom alert to every registered pharmacist LINE account."""
    if not ls.push_enabled():
        return
    with read_db() as conn:
        recipients = [
            row["line_user_id"]
            for row in fetch_all(
                conn,
                "SELECT line_user_id FROM line_recipients WHERE is_active=1",
            )
        ]
    if not recipients:
        return
    severity_mark = " ⚠️ (รุนแรง)" if result["urgent"] else ""
    lines = [
        "🔔 มีการแจ้งอาการใหม่จากผู้ป่วย",
        f"ชื่อ: {patient['full_name']}",
        f"HN: {patient.get('hn') or '-'}",
        f"ระดับความรุนแรง: {result['severity']}/5{severity_mark}",
        f"อาการ: {', '.join(result['labels']) or '-'}",
        "",
        f"เลขรับคำตอบ: {result['ticket_code']}",
        f"ตอบกลับโดยพิมพ์: {result['ticket_code']}+ข้อความถึงผู้ป่วย",
        f"{get_settings().base_url}/symptoms",
    ]
    delivered, _ = ls.multicast_text(recipients, "\n".join(lines))
    with db() as conn:
        log_notification(
            conn, patient["patient_id"], None, "symptom_ack",
            f"alert staff ticket={result['ticket_code']}", delivered > 0,
        )


# ---------------------------------------------------------------------------
# Replies and resolution
# ---------------------------------------------------------------------------
def find_open_by_ticket(conn: sqlite3.Connection, code: str) -> dict | None:
    return fetch_one(
        conn,
        "SELECT sr.*, p.full_name, p.hn, p.line_user_id FROM symptom_reports sr "
        "JOIN patients p ON sr.patient_id=p.patient_id "
        "WHERE sr.ticket_code=? AND sr.status<>'resolved' "
        "ORDER BY sr.created_at DESC LIMIT 1",
        (code.upper(),),
    )


def record_reply(
    conn: sqlite3.Connection, report_id: int, reply: str, replied_by: str
) -> dict | None:
    report = fetch_one(
        conn,
        "SELECT sr.*, p.full_name, p.line_user_id FROM symptom_reports sr "
        "JOIN patients p ON sr.patient_id=p.patient_id WHERE sr.report_id=?",
        (report_id,),
    )
    if report is None:
        return None
    conn.execute(
        "UPDATE symptom_reports SET pharmacist_reply=?, replied_by=?, replied_at=?, "
        "status='replied' WHERE report_id=?",
        (reply[:2000], replied_by, now(), report_id),
    )
    log_audit(
        conn, "reply_symptom", "symptom_reports", report_id, replied_by,
        f"ticket={report.get('ticket_code')}",
    )
    return report


def deliver_reply_to_patient(report: dict, reply: str) -> bool:
    if not report.get("line_user_id"):
        return False
    text = (
        f"💬 คำตอบจากเภสัชกร\n"
        f"เรื่องอาการที่คุณแจ้งเมื่อ {thai_date(report.get('report_date'))}\n\n"
        f"{reply}\n\n"
        f"หากอาการแย่ลง กรุณาติดต่อโรงพยาบาลทันทีนะคะ"
    )
    return notify_patient(
        {"patient_id": report["patient_id"], "line_user_id": report["line_user_id"]},
        text, "symptom_reply",
    )


def resolve(conn: sqlite3.Connection, report_id: int, performed_by: str) -> bool:
    cursor = conn.execute(
        "UPDATE symptom_reports SET status='resolved' WHERE report_id=?", (report_id,)
    )
    if not cursor.rowcount:
        return False
    log_audit(conn, "resolve_symptom", "symptom_reports", report_id, performed_by, "")
    return True


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def list_reports(
    status: str = "", limit: int = 50, offset: int = 0, patient_id: int | None = None
) -> list[dict]:
    sql = (
        "SELECT sr.*, p.full_name, p.hn FROM symptom_reports sr "
        "JOIN patients p ON sr.patient_id=p.patient_id WHERE 1=1"
    )
    params: list = []
    if status in SYMPTOM_STATUS_LABELS:
        sql += " AND sr.status=?"
        params.append(status)
    elif status == "open":
        sql += " AND sr.status IN ('new','replied')"
    if patient_id:
        sql += " AND sr.patient_id=?"
        params.append(patient_id)
    sql += " ORDER BY sr.severity DESC, sr.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with read_db() as conn:
        rows = fetch_all(conn, sql, tuple(params))
    for row in rows:
        row["labels"] = symptom_labels(row)
    return rows


def count_reports(status: str = "", patient_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) FROM symptom_reports WHERE 1=1"
    params: list = []
    if status in SYMPTOM_STATUS_LABELS:
        sql += " AND status=?"
        params.append(status)
    elif status == "open":
        sql += " AND status IN ('new','replied')"
    if patient_id:
        sql += " AND patient_id=?"
        params.append(patient_id)
    with read_db() as conn:
        return int(scalar(conn, sql, tuple(params)))


def open_count() -> int:
    with read_db() as conn:
        return int(
            scalar(conn, "SELECT COUNT(*) FROM symptom_reports WHERE status='new'")
        )


# ---------------------------------------------------------------------------
# Staff LINE recipients
# ---------------------------------------------------------------------------
def register_recipient(line_user_id: str) -> dict:
    display_name = ls.fetch_display_name(line_user_id)
    with db() as conn:
        existing = fetch_one(
            conn, "SELECT * FROM line_recipients WHERE line_user_id=?", (line_user_id,)
        )
        if existing:
            conn.execute(
                "UPDATE line_recipients SET is_active=1, display_name=COALESCE(?, display_name) "
                "WHERE recipient_id=?",
                (display_name, existing["recipient_id"]),
            )
            existing["is_active"] = 1
            existing["display_name"] = display_name or existing.get("display_name")
            return existing
        recipient_id = insert_returning_id(
            conn,
            "INSERT INTO line_recipients (line_user_id, display_name, is_active, registered_at) "
            "VALUES (?,?,1,?)",
            (line_user_id, display_name, now()),
        )
        log_audit(
            conn, "line_staff_register", "line_recipients", recipient_id,
            display_name or line_user_id[:10], "",
        )
    return {
        "recipient_id": recipient_id,
        "line_user_id": line_user_id,
        "display_name": display_name,
        "is_active": 1,
    }


def deactivate_recipient(line_user_id: str) -> bool:
    with db() as conn:
        cursor = conn.execute(
            "UPDATE line_recipients SET is_active=0 WHERE line_user_id=? AND is_active=1",
            (line_user_id,),
        )
        changed = bool(cursor.rowcount)
        if changed:
            log_audit(
                conn, "line_staff_unregister", "line_recipients", line_user_id[:16],
                "line", "",
            )
    return changed


def active_recipient(line_user_id: str) -> dict | None:
    with read_db() as conn:
        return fetch_one(
            conn,
            "SELECT * FROM line_recipients WHERE line_user_id=? AND is_active=1",
            (line_user_id,),
        )


def list_recipients() -> list[dict]:
    with read_db() as conn:
        return fetch_all(
            conn, "SELECT * FROM line_recipients ORDER BY is_active DESC, registered_at DESC"
        )
