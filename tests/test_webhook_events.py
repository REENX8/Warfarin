"""LINE webhook event handling, including the pharmacist ticket workflow."""
import pytest

from warfarin import line_service
from warfarin import symptoms as symptom_service
from warfarin.config import reset_settings_cache
from warfarin.db import db, read_db
from warfarin.routers.line_bp import _handle_staff_message, handle_event


@pytest.fixture
def captured(monkeypatch):
    """Capture outbound LINE calls instead of hitting the network."""
    sent = {"reply": [], "push": [], "multicast": []}
    monkeypatch.setattr(
        line_service, "reply_text",
        lambda token, text: sent["reply"].append(text) or True,
    )
    monkeypatch.setattr(
        line_service, "reply_messages",
        lambda token, messages: sent["reply"].append(messages) or True,
    )
    monkeypatch.setattr(
        line_service, "push_text",
        lambda user_id, text: sent["push"].append((user_id, text)) or True,
    )
    monkeypatch.setattr(
        line_service, "multicast_text",
        lambda ids, text: (sent["multicast"].append((list(ids), text)), (len(list(ids)), 0))[1],
    )
    monkeypatch.setattr(line_service, "fetch_display_name", lambda user_id: "ภก.ทดสอบ")
    return sent


def _message_event(user_id: str, text: str) -> dict:
    return {
        "type": "message",
        "source": {"userId": user_id},
        "replyToken": "reply-token",
        "message": {"type": "text", "text": text},
    }


def test_follow_event_sends_welcome(captured):
    handle_event({"type": "follow", "source": {"userId": "U" + "c" * 32},
                  "replyToken": "reply-token"})
    assert captured["reply"], "a follow must be answered with the welcome message"


def test_unfollow_event_is_logged_without_reply(captured):
    handle_event({"type": "unfollow", "source": {"userId": "U" + "d" * 32}})
    assert captured["reply"] == []


def test_event_without_user_id_is_ignored(captured):
    handle_event({"type": "message", "source": {}, "message": {"type": "text", "text": "hi"}})
    assert captured["reply"] == []


def test_non_text_message_gets_guidance(captured):
    handle_event({
        "type": "message", "source": {"userId": "U" + "e" * 32},
        "replyToken": "reply-token", "message": {"type": "image"},
    })
    assert any("help" in str(item) for item in captured["reply"])


def test_patient_command_is_routed(captured, patient):
    from warfarin import line_messages

    line_id = "U" + "f" * 32
    line_messages.route_command(line_id, f"ลงทะเบียน {patient['hn']}")
    captured["reply"].clear()
    handle_event(_message_event(line_id, "สถานะ"))
    assert captured["reply"]


# --- pharmacist registration and replies -----------------------------------
@pytest.fixture
def staff_code(monkeypatch):
    monkeypatch.setenv("LINE_STAFF_REGISTER_CODE", "PHARM-2026")
    reset_settings_cache()
    yield "PHARM-2026"
    monkeypatch.delenv("LINE_STAFF_REGISTER_CODE", raising=False)
    reset_settings_cache()


def test_staff_registers_with_code(captured, staff_code):
    line_id = "U" + "1a" * 16
    assert _handle_staff_message(line_id, "reply-token", staff_code) is True
    assert symptom_service.active_recipient(line_id) is not None
    assert any("ลงทะเบียนรับแจ้งอาการสำเร็จ" in str(item) for item in captured["reply"])


def test_ticket_reply_from_unregistered_user_falls_through(captured, staff_code):
    """A patient typing something like 'A01 ...' must not become a reply."""
    assert _handle_staff_message("U" + "2a" * 16, "reply-token", "A01+hello") is False


def test_registered_staff_can_answer_by_ticket(captured, staff_code, patient):
    line_id = "U" + "3a" * 16
    symptom_service.register_recipient(line_id)
    with db() as conn:
        report = symptom_service.create_report(conn, patient, {"severity": "3", "bleeding": "1"})

    consumed = _handle_staff_message(
        line_id, "reply-token", f"{report['ticket_code']}+ให้มาพบเภสัชกรพรุ่งนี้ค่ะ"
    )
    assert consumed is True
    with read_db() as conn:
        row = conn.execute(
            "SELECT status, pharmacist_reply, replied_by FROM symptom_reports WHERE report_id=?",
            (report["report_id"],),
        ).fetchone()
    assert row["status"] == "replied"
    assert row["pharmacist_reply"] == "ให้มาพบเภสัชกรพรุ่งนี้ค่ะ"
    assert "LINE" in row["replied_by"]


def test_ticket_reply_without_text_asks_for_it(captured, staff_code, patient):
    line_id = "U" + "4a" * 16
    symptom_service.register_recipient(line_id)
    with db() as conn:
        report = symptom_service.create_report(conn, patient, {"severity": "2"})
    _handle_staff_message(line_id, "reply-token", f"{report['ticket_code']}+")
    assert any("กรุณาพิมพ์ข้อความหลังรหัส" in str(item) for item in captured["reply"])


def test_unknown_ticket_is_reported(captured, staff_code):
    line_id = "U" + "5a" * 16
    symptom_service.register_recipient(line_id)
    _handle_staff_message(line_id, "reply-token", "Z99+ข้อความ")
    assert any("ไม่พบเลขรับคำตอบ" in str(item) for item in captured["reply"])


def test_staff_can_unregister(captured, staff_code):
    line_id = "U" + "6a" * 16
    symptom_service.register_recipient(line_id)
    assert _handle_staff_message(line_id, "reply-token", "ยกเลิกการลงทะเบียน") is True
    assert symptom_service.active_recipient(line_id) is None


def test_unregister_from_non_recipient_falls_through(captured, staff_code):
    assert _handle_staff_message("U" + "7a" * 16, "reply-token", "ยกเลิก") is False


def test_new_symptom_alerts_registered_staff(captured, staff_code, patient):
    line_id = "U" + "8a" * 16
    symptom_service.register_recipient(line_id)
    with db() as conn:
        report = symptom_service.create_report(conn, patient, {"severity": "5", "bleeding": "1"})

    import warfarin.symptoms as service

    # notify_staff short-circuits when no access token is configured.
    monkeypatched = service.ls.push_enabled
    service.ls.push_enabled = lambda: True
    try:
        service.notify_staff(patient, report)
    finally:
        service.ls.push_enabled = monkeypatched

    assert captured["multicast"], "registered pharmacists must receive the alert"
    recipients, text = captured["multicast"][-1]
    assert line_id in recipients
    assert report["ticket_code"] in text
