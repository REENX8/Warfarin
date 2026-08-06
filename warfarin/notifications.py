"""Outbound patient/caregiver notifications and their audit log."""
from __future__ import annotations

import logging
import sqlite3

from warfarin import line_service as ls
from warfarin.adherence import compute_streak
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, fetch_one, read_db, scalar
from warfarin.patients import notifiable_caregivers
from warfarin.time_utils import now, thai_date, today

logger = logging.getLogger(__name__)

MESSAGE_TYPES = {
    "reminder_1": "เตือนกินยาครั้งที่ 1",
    "reminder_2": "เตือนกินยาครั้งที่ 2",
    "missed": "แจ้งลืมกินยา",
    "missed_caregiver": "แจ้งผู้ดูแล",
    "confirmed": "ยืนยันการกินยา",
    "broadcast": "ประกาศถึงผู้ป่วย",
    "appointment": "แจ้งเตือนนัดหมาย",
    "low_stock": "แจ้งยาใกล้หมด",
    "symptom_ack": "ตอบรับรายงานอาการ",
    "symptom_reply": "คำตอบจากเภสัชกร",
    "inr_result": "แจ้งผล INR",
}


def log_notification(
    conn: sqlite3.Connection,
    patient_id: int | None,
    dose_id: int | None,
    message_type: str,
    text: str,
    delivered: bool,
    channel: str = "line",
) -> None:
    try:
        conn.execute(
            "INSERT INTO notification_log "
            "(patient_id, dose_id, channel, message_type, message_text, sent_at, delivered) "
            "VALUES (?,?,?,?,?,?,?)",
            (patient_id, dose_id, channel, message_type, (text or "")[:1000], now(),
             1 if delivered else 0),
        )
    except sqlite3.Error:
        logger.exception("Notification log write failed (type=%s)", message_type)


def notify_patient(
    patient: dict,
    text: str,
    message_type: str,
    dose_id: int | None = None,
    messages: list | None = None,
) -> bool:
    """Push to a patient's LINE and record the attempt. Never raises."""
    line_user_id = (patient or {}).get("line_user_id") or ""
    if not line_user_id:
        return False
    try:
        if messages:
            delivered = ls.push_messages(line_user_id, messages)
        else:
            delivered = ls.push_text(line_user_id, text)
    except Exception:
        logger.exception("LINE push failed for patient %s", patient.get("patient_id"))
        delivered = False
    try:
        with db() as conn:
            log_notification(
                conn, patient.get("patient_id"), dose_id, message_type, text, delivered
            )
    except Exception:
        logger.exception("Could not record notification")
    return delivered


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------
def _dose_link(token_id: str) -> str:
    return f"{get_settings().base_url}/dose/{token_id}"


def reminder_text(patient: dict, dose: dict | None, attempt: int) -> str:
    hospital = get_settings().hospital_name
    if dose is None:
        return (
            f"💊 ถึงเวลากินยาวาร์ฟารินแล้วค่ะ คุณ{patient.get('full_name','')}\n"
            "กรุณากินยาตามที่เภสัชกรกำหนด และยืนยันในระบบด้วยนะคะ"
        )
    link = _dose_link(dose["token_id"]) if dose.get("token_id") else ""
    pill = dose.get("pill_description") or "ยาวาร์ฟาริน"
    if attempt <= 1:
        body = (
            f"💊 ถึงเวลากินยาแล้วค่ะ\n"
            f"คุณ{patient.get('full_name','')}\n"
            f"• ขนาด: {dose['warfarin_mg']} mg\n"
            f"• ลักษณะยา: {pill}\n"
            f"• เวลา: {dose['scheduled_time']} น."
        )
    else:
        body = (
            f"⏰ แจ้งเตือนซ้ำ (ครั้งที่ {attempt})\n"
            f"ยังไม่พบการยืนยันกินยา {dose['warfarin_mg']} mg ของวันนี้\n"
            f"กรุณาอย่าลืมกินยานะคะ"
        )
    if link:
        body += f"\n\n✅ กดยืนยันหลังกินยา:\n{link}"
    body += "\n\nหากมีอาการผิดปกติ พิมพ์ 'อาการ' เพื่อรายงานค่ะ"
    if attempt > 1:
        body += f"\n— {hospital}"
    return body


def pending_dose_for(conn: sqlite3.Connection, patient_id: int) -> dict | None:
    return fetch_one(
        conn,
        "SELECT mp.dose_id, mp.warfarin_mg, mp.pill_description, mp.scheduled_time, "
        "dt.token_id FROM medication_plan mp "
        "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
        "WHERE mp.patient_id=? AND mp.scheduled_date=? AND mp.status='planned' "
        "ORDER BY mp.scheduled_time LIMIT 1",
        (patient_id, today()),
    )


def send_dose_reminder(patient: dict, attempt: int = 1) -> bool:
    with read_db() as conn:
        dose = pending_dose_for(conn, patient["patient_id"])
    if dose is None:
        return False
    text = reminder_text(patient, dose, attempt)
    delivered = notify_patient(
        patient, text, f"reminder_{min(attempt, 2)}", dose_id=dose["dose_id"]
    )
    with db() as conn:
        conn.execute(
            "UPDATE dose_tokens SET reminder_count=COALESCE(reminder_count,0)+1 "
            "WHERE dose_id=?",
            (dose["dose_id"],),
        )
    return delivered


def send_confirmation(patient: dict, dose: dict, streak: int) -> bool:
    if streak >= 30:
        trophy = "🏆"
    elif streak >= 14:
        trophy = "🥇"
    elif streak >= 7:
        trophy = "🥈"
    else:
        trophy = "🎯"
    text = (
        f"บันทึกการกินยาเรียบร้อย! ✅\n"
        f"ยาวาร์ฟาริน {dose.get('warfarin_mg', '')} mg\n"
        f"{trophy} กินยาต่อเนื่อง {streak} วัน\n\n"
        f"รักษาความสม่ำเสมอไว้นะคะ 💪"
    )
    return notify_patient(patient, text, "confirmed", dose_id=dose.get("dose_id"))


def send_missed_alert(patient: dict, dose_id: int | None = None) -> bool:
    settings = get_settings()
    contact = f"\n📞 {settings.hospital_phone}" if settings.hospital_phone else ""
    text = (
        f"⚠️ คุณ{patient['full_name']}\n"
        f"ยังไม่พบการยืนยันกินยาวาร์ฟารินของวันนี้\n\n"
        f"• หากยังไม่ได้กินและนึกได้ภายใน 12 ชั่วโมง ให้กินทันที\n"
        f"• หากเกิน 12 ชั่วโมง ให้ข้ามมื้อนี้ ห้ามกินซ้อนสองมื้อ\n"
        f"• หากไม่แน่ใจ กรุณาติดต่อเภสัชกรก่อน\n"
        f"— {settings.hospital_name}{contact}"
    )
    delivered = notify_patient(patient, text, "missed", dose_id=dose_id)

    with read_db() as conn:
        caregivers = notifiable_caregivers(conn, patient["patient_id"])
    for caregiver in caregivers:
        caregiver_text = (
            f"⚠️ แจ้งผู้ดูแล\n"
            f"ผู้ป่วย {patient['full_name']} ยังไม่ได้ยืนยันการกินยาวาร์ฟารินวันนี้\n"
            f"รบกวนช่วยเตือนด้วยนะคะ"
        )
        sent = ls.push_text(caregiver["line_user_id"], caregiver_text)
        with db() as conn:
            log_notification(
                conn, patient["patient_id"], dose_id, "missed_caregiver",
                caregiver_text, sent,
            )
    return delivered


def send_appointment_reminder(patient: dict, appointment: dict) -> bool:
    from warfarin.clinical import APPOINTMENT_TYPES

    label = APPOINTMENT_TYPES.get(appointment.get("appointment_type", "inr"), "นัดหมาย")
    when = thai_date(appointment["appointment_date"])
    if appointment.get("appointment_time"):
        when += f" เวลา {appointment['appointment_time']} น."
    location = f"\n📍 {appointment['location']}" if appointment.get("location") else ""
    text = (
        f"📆 แจ้งเตือนนัดหมาย\n"
        f"คุณ{patient['full_name']}\n\n"
        f"• {label}\n"
        f"• วันที่ {when}{location}\n\n"
        f"กรุณามาตามนัดเพื่อตรวจติดตามระดับยานะคะ\n"
        f"หากมาไม่ได้ กรุณาแจ้งเจ้าหน้าที่ล่วงหน้า"
    )
    return notify_patient(patient, text, "appointment")


def send_low_stock_alert(patient: dict, days_left: int) -> bool:
    text = (
        f"💊 แจ้งเตือนยาใกล้หมด\n"
        f"คุณ{patient['full_name']}\n\n"
        f"ยาวาร์ฟารินที่เหลืออยู่พอใช้อีกประมาณ {days_left} วัน\n"
        f"กรุณาติดต่อรับยาก่อนยาหมดนะคะ เพื่อไม่ให้ขาดยา"
    )
    return notify_patient(patient, text, "low_stock")


def send_inr_result(patient: dict, value: float, assessment) -> bool:
    mark = "✅" if assessment.band == "in_range" else "⚠️"
    text = (
        f"🧪 ผลตรวจ INR ของคุณ{patient['full_name']}\n\n"
        f"{mark} ค่า INR: {value} ({assessment.label})\n"
        f"เป้าหมาย: {patient['target_inr_min']}–{patient['target_inr_max']}\n\n"
        f"{assessment.advice}"
    )
    if assessment.urgency >= 2:
        text += "\n\n📞 กรุณาติดต่อโรงพยาบาลโดยเร็วที่สุด"
    return notify_patient(patient, text, "inr_result")


def broadcast(patients: list[dict], message: str, performed_by: str) -> dict:
    """Multicast to a filtered patient group and log one row per patient."""
    recipients = [p for p in patients if p.get("line_user_id")]
    if not recipients:
        return {"sent": 0, "failed": 0, "total": 0}
    delivered, failed = ls.multicast_text(
        [p["line_user_id"] for p in recipients], message
    )
    with db() as conn:
        for index, patient in enumerate(recipients):
            log_notification(
                conn, patient["patient_id"], None, "broadcast", message,
                index < delivered,
            )
        from warfarin.audit import log_audit

        log_audit(
            conn, "broadcast", "line", "patients", performed_by,
            f"sent={delivered} failed={failed} total={len(recipients)}",
        )
    return {"sent": delivered, "failed": failed, "total": len(recipients)}


def recent_notifications(limit: int = 100, offset: int = 0, message_type: str = "") -> list[dict]:
    sql = (
        "SELECT nl.*, p.full_name, p.hn FROM notification_log nl "
        "LEFT JOIN patients p ON nl.patient_id=p.patient_id WHERE 1=1"
    )
    params: list = []
    if message_type:
        sql += " AND nl.message_type=?"
        params.append(message_type)
    sql += " ORDER BY nl.sent_at DESC, nl.log_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with read_db() as conn:
        return fetch_all(conn, sql, tuple(params))


def count_notifications(message_type: str = "") -> int:
    sql = "SELECT COUNT(*) FROM notification_log WHERE 1=1"
    params: list = []
    if message_type:
        sql += " AND message_type=?"
        params.append(message_type)
    with read_db() as conn:
        return int(scalar(conn, sql, tuple(params)))


def delivery_stats(days: int = 7) -> dict:
    from warfarin.time_utils import days_ago

    since = days_ago(days)
    with read_db() as conn:
        rows = fetch_all(
            conn,
            "SELECT message_type, COUNT(*) AS total, SUM(delivered) AS delivered "
            "FROM notification_log WHERE sent_at>=? GROUP BY message_type",
            (since,),
        )
    total = sum(r["total"] for r in rows)
    delivered = sum(r["delivered"] or 0 for r in rows)
    return {
        "rows": rows,
        "total": total,
        "delivered": delivered,
        "percent": round(delivered / total * 100, 1) if total else 0.0,
    }


def streak_for(patient_id: int) -> int:
    with read_db() as conn:
        return compute_streak(conn, patient_id)
