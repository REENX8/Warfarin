"""LINE webhook and staff broadcast.

The webhook serves two audiences on one channel:
  * patients — the command menu (status, dose, INR, appointments, ...)
  * pharmacists — registration with a shared code, then `A01+text` replies
    to answer a symptom report straight from LINE.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from warfarin import line_messages, notifications, symptoms
from warfarin import line_service as ls
from warfarin.adherence import compute_adherence_bulk, last_inr_bulk
from warfarin.audit import log_audit_standalone
from warfarin.config import get_settings
from warfarin.db import db, read_db
from warfarin.deps import client_ip, csrf_protect, redirect, require_user
from warfarin.patients import linked_patients
from warfarin.security import is_rate_limited
from warfarin.time_utils import days_ago

logger = logging.getLogger(__name__)

router = APIRouter(tags=["line"])
staff_router = APIRouter(prefix="/line", tags=["line"], dependencies=[Depends(csrf_protect)])

# Ticket code (A01) optionally followed by '+' or whitespace, then the reply.
TICKET_RE = re.compile(r"^([A-Za-z]\d{2})\s*\+?\s*(.*)$", re.DOTALL)
UNREGISTER_WORDS = {"ยกเลิกการลงทะเบียน", "ยกเลิกรับแจ้ง", "unregister", "stop"}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@router.post("/webhook")
async def line_webhook(request: Request):
    """Always answers 200 except on a bad signature, so LINE stops retrying."""
    body = await request.body()
    if not body:
        # LINE Developers Console "Verify" sends an empty body.
        return JSONResponse({"status": "ok"})

    settings = get_settings()
    if not settings.line_webhook_enabled:
        return JSONResponse({"status": "line not configured"})

    signature = request.headers.get("X-Line-Signature", "")
    if not ls.verify_signature(body, signature):
        logger.warning("Rejected LINE webhook with bad signature from %s", client_ip(request))
        return JSONResponse({"status": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"status": "bad payload"}, status_code=400)

    for event in payload.get("events", []):
        try:
            handle_event(event)
        except Exception:
            logger.exception("LINE event handling failed")
    return JSONResponse({"status": "ok"})


def handle_event(event: dict) -> None:
    event_type = event.get("type")
    source = event.get("source") or {}
    user_id = source.get("userId")
    reply_token = event.get("replyToken")
    if not user_id:
        return

    if event_type == "follow":
        log_audit_standalone("line_follow", "line", user_id[:16], "system", "")
        ls.reply_messages(
            reply_token,
            line_messages.as_messages(
                line_messages.WELCOME_TEXT.format(
                    hospital=get_settings().hospital_name
                )
            ),
        )
        return

    if event_type == "unfollow":
        log_audit_standalone("line_unfollow", "line", user_id[:16], "system", "")
        return

    if event_type != "message":
        return
    message = event.get("message") or {}
    if message.get("type") != "text":
        ls.reply_text(
            reply_token,
            "ระบบรับเฉพาะข้อความตัวอักษรค่ะ พิมพ์ 'help' เพื่อดูเมนู",
        )
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    # A LINE user can trigger DB work, so cap how fast one account can do it.
    if is_rate_limited(f"line:{user_id}", 30, 60):
        logger.info("Rate limited LINE user %s", user_id[:10])
        return

    if _handle_staff_message(user_id, reply_token, text):
        return

    messages = line_messages.route_command(user_id, text)
    if messages:
        ls.reply_messages(reply_token, messages)


def _handle_staff_message(user_id: str, reply_token: str, text: str) -> bool:
    """Handle pharmacist registration and ticket replies. True when consumed."""
    settings = get_settings()
    code = settings.line_staff_register_code

    if code and text == code:
        recipient = symptoms.register_recipient(user_id)
        ls.reply_text(
            reply_token,
            "ลงทะเบียนรับแจ้งอาการสำเร็จ ✅\n"
            f"ชื่อ: {recipient.get('display_name') or 'เจ้าหน้าที่'}\n\n"
            "ระบบจะส่งการแจ้งอาการของผู้ป่วยมาที่นี่\n"
            'ตอบกลับผู้ป่วยโดยพิมพ์ "เลขรับคำตอบ+ข้อความ" เช่น A01+กินยาพร้อมอาหารนะคะ',
        )
        return True

    if text in UNREGISTER_WORDS:
        changed = symptoms.deactivate_recipient(user_id)
        if changed:
            ls.reply_text(reply_token, "ยกเลิกการรับแจ้งอาการแล้วค่ะ")
            return True
        return False

    match = TICKET_RE.match(text)
    if not match:
        return False
    # Only a registered pharmacist may use ticket replies; for anyone else the
    # message falls through to the patient command router.
    recipient = symptoms.active_recipient(user_id)
    if recipient is None:
        return False

    ticket = match.group(1).upper()
    reply = match.group(2).strip()
    if not reply:
        ls.reply_text(
            reply_token, f"กรุณาพิมพ์ข้อความหลังรหัส เช่น {ticket}+ข้อความถึงผู้ป่วย"
        )
        return True

    with db() as conn:
        report = symptoms.find_open_by_ticket(conn, ticket)
        if report is None:
            ls.reply_text(reply_token, f"ไม่พบเลขรับคำตอบ {ticket} ที่กำลังรอตอบอยู่")
            return True
        replied_by = f"{recipient.get('display_name') or 'เภสัชกร'} (LINE)"
        symptoms.record_reply(conn, report["report_id"], reply, replied_by)

    delivered = symptoms.deliver_reply_to_patient(report, reply)
    ls.reply_text(
        reply_token,
        f"บันทึกคำตอบเลข {ticket} เรียบร้อย ✅\n"
        + (
            f"ส่งถึงคุณ{report['full_name']} ทาง LINE แล้ว"
            if delivered
            else "แต่ผู้ป่วยยังไม่ได้เชื่อม LINE — กรุณาติดต่อทางโทรศัพท์"
        ),
    )
    return True


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------
BROADCAST_TARGETS = {
    "all": "ผู้ป่วยที่เชื่อม LINE ทั้งหมด",
    "low_adherence": "ความสม่ำเสมอ < 50% (30 วัน)",
    "missed_yesterday": "ขาดยาเมื่อวาน",
    "inr_out_of_range": "INR ล่าสุดนอกเป้าหมาย",
    "inr_due": "ถึงกำหนดตรวจ INR",
    "high_risk": "กลุ่มเสี่ยงสูง (ขาดยาหรือ INR ผิดปกติ)",
}


def filter_broadcast_targets(
    conn: sqlite3.Connection, patients: list[dict], target: str
) -> list[dict]:
    """Narrow a patient list to a broadcast audience, using bulk queries."""
    if target == "all" or not patients:
        return patients
    ids = [p["patient_id"] for p in patients]

    if target == "missed_yesterday":
        yesterday = days_ago(1)
        placeholders = ",".join("?" * len(ids))
        missed = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT patient_id FROM medication_plan "
                f"WHERE patient_id IN ({placeholders}) AND scheduled_date=? "
                f"AND status IN ('missed','planned')",
                (*ids, yesterday),
            ).fetchall()
        }
        return [p for p in patients if p["patient_id"] in missed]

    if target == "inr_due":
        from warfarin.time_utils import today

        return [
            p for p in patients
            if p.get("next_inr_date") and p["next_inr_date"] <= today()
        ]

    adherence = compute_adherence_bulk(conn, ids, 30)
    last_inr = last_inr_bulk(conn, ids)

    if target == "low_adherence":
        return [
            p for p in patients
            if adherence[p["patient_id"]]["total"]
            and adherence[p["patient_id"]]["percent"] < 50
        ]
    if target == "inr_out_of_range":
        return [
            p for p in patients
            if last_inr.get(p["patient_id"])
            and not last_inr[p["patient_id"]]["in_range"]
        ]
    if target == "high_risk":
        selected = []
        for patient in patients:
            pid = patient["patient_id"]
            stats = adherence[pid]
            inr = last_inr.get(pid)
            if (stats["total"] and stats["percent"] < 50) or (inr and not inr["in_range"]):
                selected.append(patient)
        return selected
    return patients


@staff_router.post("/broadcast")
async def broadcast(request: Request, user: dict = Depends(require_user)):
    form = await request.form()
    message = str(form.get("message") or "").strip()
    target = str(form.get("target") or "all")
    if target not in BROADCAST_TARGETS:
        target = "all"
    if not message:
        return redirect("/dashboard", "กรุณากรอกข้อความที่จะส่ง", "danger")
    if not ls.push_enabled():
        return redirect("/dashboard", "ยังไม่ได้ตั้งค่า LINE Access Token", "danger")
    message = message[:1500]

    with read_db() as conn:
        patients = linked_patients(conn)
        selected = filter_broadcast_targets(conn, patients, target)
    if not selected:
        return redirect("/dashboard", "ไม่มีผู้ป่วยตรงตามเงื่อนไขที่เลือก", "warning")

    result = notifications.broadcast(selected, message, user["username"])
    return redirect(
        "/dashboard",
        f"ส่งข้อความสำเร็จ {result['sent']} ราย "
        f"(ไม่สำเร็จ {result['failed']} ราย) — {BROADCAST_TARGETS[target]}",
        "success" if result["sent"] else "warning",
    )
