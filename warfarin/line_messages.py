"""LINE message content: Flex bubbles, quick replies and the command router.

Every builder degrades to plain text when the SDK's Flex classes are missing,
so a patient always gets a usable answer.
"""
from __future__ import annotations

import logging
import re

from warfarin import line_service as ls
from warfarin.adherence import (
    compute_adherence,
    compute_gamification_score,
    compute_streak,
    compute_ttr,
)
from warfarin.clinical import DOSE_STATUS_LABELS, EDUCATION_TOPICS, education_text
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, fetch_one, read_db
from warfarin.time_utils import now, thai_date, today

logger = logging.getLogger(__name__)

STATUS_ICONS = {
    "taken": "✅", "late": "⏰", "missed": "❌", "planned": "🕐",
}


def _status_text(status: str) -> str:
    return f"{DOSE_STATUS_LABELS.get(status, status)} {STATUS_ICONS.get(status, '')}".strip()


def _base_url() -> str:
    return get_settings().base_url


# ---------------------------------------------------------------------------
# Flex helpers
# ---------------------------------------------------------------------------
def _alt_text(text: str, limit: int = 380) -> str:
    return text.replace("\n", " ").strip()[:limit]


def build_bubble(header: str, color: str, rows: list, footer_button=None):
    """Standard bubble: coloured header, label/value body, optional CTA."""
    if not ls.FLEX_AVAILABLE:
        return None
    try:
        header_box = ls.FlexBox(
            layout="vertical",
            padding_all="16px",
            background_color=color,
            contents=[
                ls.FlexText(text=header, color="#ffffff", weight="bold", size="lg", wrap=True)
            ],
        )
        body_contents = []
        for row in rows:
            if isinstance(row, tuple):
                label, value = row
                body_contents.append(ls.FlexBox(
                    layout="baseline", spacing="sm",
                    contents=[
                        ls.FlexText(text=str(label), color="#64748b", size="sm", flex=4),
                        ls.FlexText(
                            text=str(value), color="#0f172a", size="sm",
                            weight="bold", flex=6, wrap=True, align="end",
                        ),
                    ],
                ))
            elif row == "---":
                body_contents.append(ls.FlexSeparator(margin="sm"))
            else:
                body_contents.append(
                    ls.FlexText(text=str(row), color="#475569", size="sm", wrap=True)
                )
        kwargs = {
            "header": header_box,
            "body": ls.FlexBox(
                layout="vertical", spacing="md", padding_all="16px",
                contents=body_contents,
            ),
        }
        if footer_button is not None:
            kwargs["footer"] = ls.FlexBox(
                layout="vertical", padding_all="12px", contents=[footer_button]
            )
        return ls.FlexBubble(**kwargs)
    except Exception:
        logger.warning("Flex bubble build failed", exc_info=True)
        return None


def build_quick_reply(items: list[tuple[str, str]]):
    """items = [(label, message_text_or_url), ...]"""
    if not ls.FLEX_AVAILABLE:
        return None
    try:
        entries = []
        for label, action in items[:13]:
            if action.startswith(("http://", "https://")):
                act = ls.URIAction(label=label[:20], uri=action)
            else:
                act = ls.MessageAction(label=label[:20], text=action)
            entries.append(ls.QuickReplyItem(action=act))
        return ls.QuickReply(items=entries) if entries else None
    except Exception:
        logger.warning("Quick reply build failed", exc_info=True)
        return None


DEFAULT_QUICK_REPLY = [
    ("📅 สถานะ", "สถานะ"),
    ("💊 ยา", "ยา"),
    ("📊 ความสม่ำเสมอ", "ความสม่ำเสมอ"),
    ("🧪 ผลเลือด", "ผลเลือด"),
    ("📆 นัดหมาย", "นัด"),
    ("📚 ความรู้", "ความรู้"),
]


def as_messages(text: str, quick_reply_items: list | None = None) -> list:
    """Wrap plain text as a LINE message list, with the default menu attached."""
    body = text[:ls.MAX_TEXT]
    if ls.SDK_AVAILABLE:
        try:
            qr = build_quick_reply(quick_reply_items or DEFAULT_QUICK_REPLY)
            return [ls.TextMessage(text=body, quick_reply=qr)]
        except Exception:
            logger.warning("TextMessage build failed; using raw payload", exc_info=True)
    return [body]


def _flex_or_text(bubble, fallback_text: str, quick_reply_items=None) -> list:
    if bubble is not None and ls.FLEX_AVAILABLE:
        try:
            return [ls.FlexMessage(
                alt_text=_alt_text(fallback_text),
                contents=bubble,
                quick_reply=build_quick_reply(quick_reply_items or DEFAULT_QUICK_REPLY),
            )]
        except Exception:
            logger.warning("FlexMessage build failed; falling back to text", exc_info=True)
    return as_messages(fallback_text, quick_reply_items)


# ---------------------------------------------------------------------------
# Data lookups shared by the reply builders
# ---------------------------------------------------------------------------
def _today_doses(patient_id: int) -> list[dict]:
    with read_db() as conn:
        return fetch_all(
            conn,
            "SELECT mp.*, dt.token_id FROM medication_plan mp "
            "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? ORDER BY mp.scheduled_time",
            (patient_id, today()),
        )


def _next_appointment(patient_id: int) -> dict | None:
    with read_db() as conn:
        return fetch_one(
            conn,
            "SELECT * FROM appointments WHERE patient_id=? AND status='scheduled' "
            "AND appointment_date>=? ORDER BY appointment_date LIMIT 1",
            (patient_id, today()),
        )


# ---------------------------------------------------------------------------
# Reply builders
# ---------------------------------------------------------------------------
def status_reply(patient: dict) -> list:
    doses = _today_doses(patient["patient_id"])
    with read_db() as conn:
        adherence = compute_adherence(conn, patient["patient_id"], 7)
        streak = compute_streak(conn, patient["patient_id"])
    lines = [f"สวัสดีคุณ{patient['full_name']}", f"📅 สถานะยาวันที่ {thai_date(today())}"]
    rows: list = [("วันที่", thai_date(today()))]
    if not doses:
        lines.append("วันนี้ไม่มีแผนกินยาค่ะ")
        rows.append("วันนี้ไม่มีแผนกินยาค่ะ")
    else:
        for dose in doses:
            lines.append(
                f"• {dose['scheduled_time']} น. — {dose['warfarin_mg']} mg — "
                f"{_status_text(dose['status'])}"
            )
            rows.append((
                f"{dose['scheduled_time']} น.",
                f"{dose['warfarin_mg']} mg · {_status_text(dose['status'])}",
            ))
    lines.append(f"\n📊 ความสม่ำเสมอ 7 วัน: {adherence['percent']}%")
    lines.append(f"🔥 ต่อเนื่อง: {streak} วัน")
    rows.append("---")
    rows.append(("ความสม่ำเสมอ 7 วัน", f"{adherence['percent']}%"))
    rows.append(("🔥 ต่อเนื่อง", f"{streak} วัน"))

    pending = next(
        (d for d in doses if d["status"] == "planned" and d.get("token_id")), None
    )
    footer = None
    if pending and ls.FLEX_AVAILABLE:
        try:
            footer = ls.FlexButton(
                style="primary", color="#059669", height="sm",
                action=ls.URIAction(
                    label="กดยืนยันกินยา ✅",
                    uri=f"{_base_url()}/dose/{pending['token_id']}",
                ),
            )
        except Exception:
            footer = None
    if pending:
        lines.append(f"\n✅ กดยืนยันกินยา:\n{_base_url()}/dose/{pending['token_id']}")
    return _flex_or_text(
        build_bubble("📅 สถานะยาวันนี้", "#4f46e5", rows, footer), "\n".join(lines)
    )


def dose_reply(patient: dict) -> list:
    doses = _today_doses(patient["patient_id"])
    if not doses:
        return as_messages(
            "วันนี้ไม่มีแผนกินยาค่ะ\nหากคิดว่าไม่ถูกต้อง กรุณาติดต่อเภสัชกร"
        )
    dose = doses[0]
    rows = [
        ("ขนาดยา", f"{dose['warfarin_mg']} mg"),
        ("ลักษณะยา", dose.get("pill_description") or "ยาวาร์ฟาริน"),
        ("เวลา", f"{dose['scheduled_time']} น."),
        ("สถานะ", _status_text(dose["status"])),
    ]
    text = (
        f"💊 ยาวาร์ฟาริน {dose['warfarin_mg']} mg\n"
        f"📋 {dose.get('pill_description') or 'ยาวาร์ฟาริน'}\n"
        f"⏰ เวลา {dose['scheduled_time']} น.\n"
        f"📌 สถานะ: {_status_text(dose['status'])}"
    )
    footer = None
    if dose["status"] == "planned" and dose.get("token_id"):
        url = f"{_base_url()}/dose/{dose['token_id']}"
        text += f"\n\n✅ กดลิงก์ยืนยันหลังกินยา:\n{url}"
        if ls.FLEX_AVAILABLE:
            try:
                footer = ls.FlexButton(
                    style="primary", color="#059669", height="sm",
                    action=ls.URIAction(label="กดยืนยันกินยา ✅", uri=url),
                )
            except Exception:
                footer = None
    return _flex_or_text(build_bubble("💊 ยาวันนี้", "#059669", rows, footer), text)


def adherence_reply(patient: dict) -> list:
    pid = patient["patient_id"]
    with read_db() as conn:
        adh7 = compute_adherence(conn, pid, 7)
        adh30 = compute_adherence(conn, pid, 30)
        streak = compute_streak(conn, pid)
    score = compute_gamification_score(adh7["percent"], streak)
    rows = [
        ("7 วัน", f"{adh7['percent']}% ({adh7['taken'] + adh7['late']}/{adh7['total']})"),
        ("30 วัน", f"{adh30['percent']}% ({adh30['taken'] + adh30['late']}/{adh30['total']})"),
        ("🔥 ต่อเนื่อง", f"{streak} วัน"),
        ("🏆 คะแนนรวม", str(score)),
    ]
    if adh7["percent"] >= 90:
        rows.append("ยอดเยี่ยมมาก! รักษาความสม่ำเสมอไว้นะคะ 🌟")
    elif adh7["percent"] >= 70:
        rows.append("ทำได้ดีค่ะ อีกนิดเดียวจะถึงเป้าหมาย 💪")
    else:
        rows.append("ลองตั้งเตือนในมือถือช่วยนะคะ การกินยาสม่ำเสมอสำคัญมากค่ะ ❤️")
    text = (
        f"📊 ความสม่ำเสมอในการกินยา\n"
        f"คุณ{patient['full_name']}\n\n"
        f"🗓️ 7 วัน: {adh7['percent']}%\n"
        f"🗓️ 30 วัน: {adh30['percent']}%\n"
        f"🔥 ต่อเนื่อง: {streak} วัน\n"
        f"🏆 คะแนนรวม: {score}"
    )
    return _flex_or_text(build_bubble("📊 ความสม่ำเสมอ", "#0891b2", rows), text)


def inr_reply(patient: dict) -> list:
    pid = patient["patient_id"]
    with read_db() as conn:
        labs = fetch_all(
            conn,
            "SELECT value, test_date, in_range FROM lab_results "
            "WHERE patient_id=? AND lab_name='INR' ORDER BY test_date DESC LIMIT 3",
            (pid,),
        )
        ttr = compute_ttr(conn, pid)
    target = f"{patient['target_inr_min']}–{patient['target_inr_max']}"
    rows: list = [("เป้าหมาย INR", target)]
    lines = [
        "🧪 ผลตรวจ INR ล่าสุด",
        f"คุณ{patient['full_name']}",
        f"เป้าหมาย {target}",
        "",
    ]
    if not labs:
        rows.append("ยังไม่มีผลการตรวจ INR ค่ะ")
        lines.append("ยังไม่มีผลการตรวจ INR ค่ะ")
    else:
        rows.append("---")
        for lab in labs:
            mark = "✅" if lab["in_range"] else "⚠️"
            rows.append((thai_date(lab["test_date"]), f"{lab['value']} {mark}"))
            lines.append(f"{mark} {thai_date(lab['test_date'])}: {lab['value']}")
    if ttr is not None:
        rows.append(("TTR (อยู่ในเป้าหมาย)", f"{ttr}%"))
        lines.append(f"\n📈 อยู่ในช่วงเป้าหมาย {ttr}% ของเวลา")
    appointment = _next_appointment(pid)
    if appointment:
        rows.append(("นัดตรวจครั้งถัดไป", thai_date(appointment["appointment_date"])))
        lines.append(f"📆 นัดครั้งถัดไป: {thai_date(appointment['appointment_date'])}")
    return _flex_or_text(build_bubble("🧪 ผล INR", "#9333ea", rows), "\n".join(lines))


def streak_reply(patient: dict) -> list:
    with read_db() as conn:
        streak = compute_streak(conn, patient["patient_id"])
    if streak >= 30:
        emoji, praise, color = "🏆", "สุดยอด! คุณคือแชมป์ความสม่ำเสมอ", "#f59e0b"
    elif streak >= 14:
        emoji, praise, color = "🥇", "ยอดเยี่ยมมาก รักษาไว้นะคะ", "#eab308"
    elif streak >= 7:
        emoji, praise, color = "🥈", "ดีมาก! อีกนิดเดียวจะครบ 2 สัปดาห์", "#14b8a6"
    elif streak >= 1:
        emoji, praise, color = "🎯", "เริ่มต้นดีมาก พยายามต่อไปนะคะ", "#0ea5e9"
    else:
        emoji, praise, color = "💪", "มาเริ่มสร้างสถิติกันใหม่นะคะ", "#64748b"
    rows = [(f"{emoji} ต่อเนื่อง", f"{streak} วัน"), praise]
    return _flex_or_text(
        build_bubble("🔥 สถิติต่อเนื่อง", color, rows),
        f"{emoji} ต่อเนื่อง {streak} วัน\n{praise}",
    )


def appointment_reply(patient: dict) -> list:
    pid = patient["patient_id"]
    with read_db() as conn:
        appointments = fetch_all(
            conn,
            "SELECT * FROM appointments WHERE patient_id=? AND status='scheduled' "
            "AND appointment_date>=? ORDER BY appointment_date LIMIT 3",
            (pid, today()),
        )
    from warfarin.clinical import APPOINTMENT_TYPES

    if not appointments:
        return as_messages(
            "📆 ยังไม่มีนัดหมายในระบบค่ะ\n"
            "กรุณาสอบถามเจ้าหน้าที่เมื่อมารับยาครั้งถัดไป"
        )
    rows: list = []
    lines = ["📆 นัดหมายครั้งถัดไป", f"คุณ{patient['full_name']}", ""]
    for appointment in appointments:
        label = APPOINTMENT_TYPES.get(
            appointment.get("appointment_type", "inr"), "นัดหมาย"
        )
        when = thai_date(appointment["appointment_date"])
        if appointment.get("appointment_time"):
            when += f" เวลา {appointment['appointment_time']} น."
        rows.append((label, when))
        lines.append(f"• {label}: {when}")
        if appointment.get("location"):
            lines.append(f"  สถานที่: {appointment['location']}")
    lines.append("\nกรุณามาตามนัดเพื่อตรวจ INR นะคะ")
    return _flex_or_text(build_bubble("📆 นัดหมาย", "#0284c7", rows), "\n".join(lines))


def stock_reply(patient: dict) -> list:
    from warfarin.clinical import days_of_stock
    from warfarin.doses import current_weekly_mg

    pid = patient["patient_id"]
    with read_db() as conn:
        weekly = current_weekly_mg(conn, pid)
    inventory = int(patient.get("pill_inventory") or 0)
    remaining_days = days_of_stock(inventory, weekly)
    lines = [f"💊 ยาคงเหลือ: {inventory} เม็ด"]
    rows: list = [("ยาคงเหลือ", f"{inventory} เม็ด")]
    if weekly:
        rows.append(("ขนาดยาต่อสัปดาห์", f"{weekly} mg"))
        lines.append(f"ขนาดยารวมต่อสัปดาห์: {weekly} mg")
    if remaining_days is not None:
        rows.append(("ใช้ได้อีกประมาณ", f"{remaining_days} วัน"))
        lines.append(f"ใช้ได้อีกประมาณ {remaining_days} วัน")
        if remaining_days <= get_settings().low_stock_threshold:
            rows.append("⚠️ ยาใกล้หมด กรุณาติดต่อรับยาก่อนวันนัด")
            lines.append("\n⚠️ ยาใกล้หมด กรุณาติดต่อรับยาก่อนวันนัดนะคะ")
    else:
        lines.append("ยังไม่มีข้อมูลเพียงพอสำหรับประมาณวันที่ยาหมด")
    return _flex_or_text(build_bubble("💊 ยาคงเหลือ", "#0d9488", rows), "\n".join(lines))


def education_messages() -> list:
    if ls.FLEX_AVAILABLE:
        try:
            bubbles = []
            for topic in EDUCATION_TOPICS:
                footer = ls.FlexButton(
                    style="link", height="sm",
                    action=ls.URIAction(
                        label="อ่านเพิ่มเติม",
                        uri=f"{_base_url()}/education#{topic['anchor']}",
                    ),
                )
                bubble = build_bubble(
                    f"{topic['icon']} {topic['title']}",
                    topic["color"], list(topic["points"]), footer,
                )
                if bubble is not None:
                    bubbles.append(bubble)
            if bubbles:
                return [ls.FlexMessage(
                    alt_text="คู่มือผู้ป่วยวาร์ฟาริน",
                    contents=ls.FlexCarousel(contents=bubbles[:10]),
                    quick_reply=build_quick_reply(DEFAULT_QUICK_REPLY),
                )]
        except Exception:
            logger.warning("Education carousel build failed", exc_info=True)
    return as_messages(f"{education_text()}\n\nอ่านเพิ่มเติม: {_base_url()}/education")


HELP_TEXT = (
    "📋 เมนูคำสั่งที่ใช้ได้\n"
    "• สถานะ — สถานะยาวันนี้\n"
    "• ยา — รายละเอียดยา + ลิงก์ยืนยัน\n"
    "• ความสม่ำเสมอ — สรุป 7/30 วัน\n"
    "• ผลเลือด — ผล INR ล่าสุด\n"
    "• ต่อเนื่อง — จำนวนวันติดต่อกัน\n"
    "• นัด — นัดหมายครั้งถัดไป\n"
    "• ยาคงเหลือ — จำนวนยาที่เหลือ\n"
    "• อาการ — รายงานอาการไม่พึงประสงค์\n"
    "• ความรู้ — คู่มือผู้ป่วยวาร์ฟาริน\n"
    "• ลงทะเบียน <HN> — เชื่อมบัญชี LINE\n"
    "• help — เมนูนี้"
)

WELCOME_TEXT = (
    "สวัสดีค่ะ! ยินดีต้อนรับสู่ระบบติดตามยาวาร์ฟาริน 💊\n"
    "{hospital}\n\n"
    "📝 ขั้นตอนลงทะเบียน:\n"
    "พิมพ์  ลงทะเบียน <HN>  เช่น  ลงทะเบียน 12345\n"
    "หรือแจ้ง LINE ของคุณกับเภสัชกร\n\n" + HELP_TEXT
)


# ---------------------------------------------------------------------------
# Command router
# ---------------------------------------------------------------------------
_REGISTER_RE = re.compile(r"^(?:ลงทะเบียน|register)\s*[:：]?\s*(\S+)", re.IGNORECASE)

COMMANDS: dict[str, str] = {}
for keyword, action in {
    "สถานะ": "status", "status": "status",
    "ยา": "dose", "dose": "dose", "doses": "dose",
    "ความสม่ำเสมอ": "adherence", "adherence": "adherence",
    "ผลเลือด": "inr", "inr": "inr", "lab": "inr",
    "ต่อเนื่อง": "streak", "streak": "streak",
    "นัด": "appointment", "นัดหมาย": "appointment", "appointment": "appointment",
    "ยาคงเหลือ": "stock", "stock": "stock", "คงเหลือ": "stock",
    "อาการ": "symptom", "symptom": "symptom", "symptoms": "symptom",
    "ความรู้": "education", "คู่มือ": "education", "education": "education",
    "knowledge": "education", "edu": "education",
    "help": "help", "ช่วยเหลือ": "help", "เมนู": "help", "menu": "help",
}.items():
    COMMANDS[keyword] = action


def _find_patient_by_line_id(line_user_id: str) -> dict | None:
    with read_db() as conn:
        return fetch_one(
            conn,
            "SELECT * FROM patients WHERE line_user_id=? AND active=1",
            (line_user_id,),
        )


def _register_patient(line_user_id: str, hn: str) -> list:
    from warfarin.audit import log_audit

    with db() as conn:
        patient = fetch_one(
            conn, "SELECT * FROM patients WHERE hn=? AND active=1", (hn,)
        )
        if patient is None:
            return as_messages(
                f"ไม่พบผู้ป่วย HN: {hn}\n"
                "กรุณาตรวจสอบเลข HN อีกครั้ง หรือติดต่อเภสัชกรค่ะ"
            )
        existing = patient.get("line_user_id")
        if existing and existing != line_user_id:
            return as_messages(
                "HN นี้ถูกลงทะเบียนกับบัญชี LINE อื่นแล้ว\n"
                "หากเป็นของคุณจริง กรุณาติดต่อเภสัชกรเพื่อยืนยันตัวตนค่ะ"
            )
        if existing == line_user_id:
            return as_messages(
                f"บัญชีนี้ลงทะเบียนกับ HN {hn} อยู่แล้วค่ะ\n\n{HELP_TEXT}"
            )
        conn.execute(
            "UPDATE patients SET line_user_id=?, updated_at=? WHERE patient_id=?",
            (line_user_id, now(), patient["patient_id"]),
        )
        log_audit(
            conn, "line_register", "patient", patient["patient_id"],
            f"line:{line_user_id[:10]}", f"linked HN={hn}",
        )
    return as_messages(
        f"✅ ลงทะเบียนสำเร็จ\n"
        f"คุณ{patient['full_name']} (HN: {hn})\n\n"
        f"ต่อจากนี้ระบบจะแจ้งเตือนเวลากินยาให้ค่ะ\n\n{HELP_TEXT}"
    )


def route_command(line_user_id: str, raw_text: str) -> list:
    """Map an inbound patient message to a reply message list."""
    raw = (raw_text or "").strip()
    lowered = raw.lower()

    match = _REGISTER_RE.match(raw)
    if match:
        return _register_patient(line_user_id, match.group(1).strip())
    if lowered in ("ลงทะเบียน", "register"):
        return as_messages(
            "กรุณาพิมพ์เลข HN ต่อท้ายด้วยค่ะ\nตัวอย่าง: ลงทะเบียน 12345"
        )

    action = COMMANDS.get(raw) or COMMANDS.get(lowered)

    if action == "help":
        return as_messages(HELP_TEXT)
    if action == "education":
        return education_messages()

    patient = _find_patient_by_line_id(line_user_id)
    if patient is None:
        return as_messages(
            "⚠️ ยังไม่พบข้อมูลผู้ป่วยของบัญชีนี้\n"
            "กรุณาพิมพ์:  ลงทะเบียน <HN>\n"
            "ตัวอย่าง: ลงทะเบียน 12345\n"
            "หรือติดต่อเภสัชกรค่ะ",
            [("📚 ความรู้", "ความรู้"), ("❓ help", "help")],
        )

    if action == "status":
        return status_reply(patient)
    if action == "dose":
        return dose_reply(patient)
    if action == "adherence":
        return adherence_reply(patient)
    if action == "inr":
        return inr_reply(patient)
    if action == "streak":
        return streak_reply(patient)
    if action == "appointment":
        return appointment_reply(patient)
    if action == "stock":
        return stock_reply(patient)
    if action == "symptom":
        return as_messages(
            "📝 รายงานอาการไม่พึงประสงค์\n"
            f"กรุณากดลิงก์นี้เพื่อกรอกแบบฟอร์ม:\n"
            f"{_base_url()}/report/symptom/{patient['access_token']}\n\n"
            "🚨 หากมีเลือดออกไม่หยุด อาเจียนเป็นเลือด อุจจาระสีดำ "
            "หรือปวดศีรษะรุนแรง กรุณาไปโรงพยาบาลทันที ไม่ต้องรอการติดต่อกลับ"
        )

    return as_messages(
        "ไม่เข้าใจคำสั่งนี้ค่ะ 🤔\n"
        "ลองกดปุ่มด้านล่าง หรือพิมพ์ 'help' เพื่อดูเมนูทั้งหมด"
    )
