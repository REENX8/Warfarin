"""Outbound notification composition and logging."""
import pytest

from warfarin import line_service, notifications
from warfarin.db import db, read_db
from warfarin.time_utils import now, today


@pytest.fixture
def captured(monkeypatch):
    sent = {"push": [], "multicast": []}
    monkeypatch.setattr(
        line_service, "push_text",
        lambda user_id, text: sent["push"].append((user_id, text)) or True,
    )
    monkeypatch.setattr(
        line_service, "multicast_text",
        lambda ids, text: (sent["multicast"].append((list(ids), text)), (len(list(ids)), 0))[1],
    )
    return sent


@pytest.fixture
def linked_patient(patient):
    line_id = "U" + "9c" * 16
    with db() as conn:
        conn.execute(
            "UPDATE patients SET line_user_id=? WHERE patient_id=?",
            (line_id, patient["patient_id"]),
        )
    patient["line_user_id"] = line_id
    return patient


def test_notify_patient_without_line_is_a_no_op(patient, captured):
    assert notifications.notify_patient(patient, "hello", "reminder_1") is False
    assert captured["push"] == []


def test_notify_patient_logs_delivery(linked_patient, captured):
    assert notifications.notify_patient(linked_patient, "ทดสอบ", "reminder_1") is True
    with read_db() as conn:
        row = conn.execute(
            "SELECT message_type, delivered FROM notification_log "
            "WHERE patient_id=? ORDER BY log_id DESC LIMIT 1",
            (linked_patient["patient_id"],),
        ).fetchone()
    assert row["message_type"] == "reminder_1"
    assert row["delivered"] == 1


def test_failed_push_is_logged_as_undelivered(linked_patient, monkeypatch):
    monkeypatch.setattr(line_service, "push_text", lambda user_id, text: False)
    assert notifications.notify_patient(linked_patient, "ทดสอบ", "reminder_1") is False
    with read_db() as conn:
        delivered = conn.execute(
            "SELECT delivered FROM notification_log WHERE patient_id=? "
            "ORDER BY log_id DESC LIMIT 1",
            (linked_patient["patient_id"],),
        ).fetchone()[0]
    assert delivered == 0


def test_push_exception_never_propagates(linked_patient, monkeypatch):
    def explode(user_id, text):
        raise RuntimeError("LINE is down")

    monkeypatch.setattr(line_service, "push_text", explode)
    assert notifications.notify_patient(linked_patient, "ทดสอบ", "reminder_1") is False


def test_reminder_text_contains_dose_and_link(linked_patient, dose_token):
    with read_db() as conn:
        dose = notifications.pending_dose_for(conn, linked_patient["patient_id"])
    text = notifications.reminder_text(linked_patient, dose, attempt=1)
    assert "3.0 mg" in text
    assert dose_token["token_id"] in text
    assert "อาการ" in text


def test_second_reminder_text_differs(linked_patient, dose_token):
    with read_db() as conn:
        dose = notifications.pending_dose_for(conn, linked_patient["patient_id"])
    assert "แจ้งเตือนซ้ำ" in notifications.reminder_text(linked_patient, dose, attempt=2)


def test_send_dose_reminder_increments_counter(linked_patient, dose_token, captured):
    notifications.send_dose_reminder(linked_patient, attempt=1)
    with read_db() as conn:
        count = conn.execute(
            "SELECT reminder_count FROM dose_tokens WHERE dose_id=?",
            (dose_token["dose_id"],),
        ).fetchone()[0]
    assert count == 1


def test_send_dose_reminder_without_pending_dose(linked_patient, captured):
    assert notifications.send_dose_reminder(linked_patient, attempt=1) is False


def test_missed_alert_also_notifies_caregiver(linked_patient, captured):
    caregiver_line = "U" + "7d" * 16
    with db() as conn:
        conn.execute(
            "INSERT INTO caregivers (patient_id, name, line_user_id, notify_enabled, created_at) "
            "VALUES (?,?,?,1,?)",
            (linked_patient["patient_id"], "ลูกสาว", caregiver_line, now()),
        )
    notifications.send_missed_alert(linked_patient)
    recipients = [user_id for user_id, _ in captured["push"]]
    assert linked_patient["line_user_id"] in recipients
    assert caregiver_line in recipients


def test_missed_alert_includes_missed_dose_guidance(linked_patient, captured):
    notifications.send_missed_alert(linked_patient)
    _, text = captured["push"][0]
    assert "12 ชั่วโมง" in text
    assert "ห้ามกินซ้อน" in text


def test_confirmation_message_scales_with_streak(linked_patient, captured):
    notifications.send_confirmation(linked_patient, {"warfarin_mg": 3.0}, 30)
    _, text = captured["push"][-1]
    assert "🏆" in text
    assert "30 วัน" in text


def test_inr_result_message_carries_advice(linked_patient, captured):
    from warfarin.clinical import assess_inr

    assessment = assess_inr(5.5, 2.0, 3.0)
    notifications.send_inr_result(linked_patient, 5.5, assessment)
    _, text = captured["push"][-1]
    assert "5.5" in text
    assert "ติดต่อโรงพยาบาล" in text


def test_low_stock_message(linked_patient, captured):
    notifications.send_low_stock_alert(linked_patient, 3)
    _, text = captured["push"][-1]
    assert "3 วัน" in text


def test_appointment_reminder_message(linked_patient, captured):
    appointment = {
        "appointment_date": today(), "appointment_time": "09:00",
        "appointment_type": "inr", "location": "คลินิกวาร์ฟาริน",
    }
    notifications.send_appointment_reminder(linked_patient, appointment)
    _, text = captured["push"][-1]
    assert "คลินิกวาร์ฟาริน" in text
    assert "09:00" in text


def test_broadcast_logs_one_row_per_patient(linked_patient, captured):
    result = notifications.broadcast([linked_patient], "ประกาศทดสอบ", "admin")
    assert result["total"] == 1
    assert result["sent"] == 1
    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM notification_log WHERE message_type='broadcast' "
            "AND patient_id=?",
            (linked_patient["patient_id"],),
        ).fetchone()[0]
    assert count == 1


def test_broadcast_skips_patients_without_line(patient, captured):
    result = notifications.broadcast([patient], "ประกาศ", "admin")
    assert result == {"sent": 0, "failed": 0, "total": 0}


def test_delivery_stats_summarises_last_week(linked_patient, captured):
    notifications.notify_patient(linked_patient, "x", "reminder_1")
    stats = notifications.delivery_stats(7)
    assert stats["total"] >= 1
    assert 0 <= stats["percent"] <= 100
