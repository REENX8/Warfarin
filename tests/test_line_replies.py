"""Content of the LINE replies patients receive."""
import pytest

from warfarin import line_messages
from warfarin.appointments import create_appointment
from warfarin.db import db
from warfarin.time_utils import days_ago, now, today


def _flatten(messages) -> str:
    parts = []
    for message in messages:
        if isinstance(message, str):
            parts.append(message)
            continue
        parts.append(getattr(message, "text", "") or "")
        parts.append(getattr(message, "alt_text", "") or "")
    return "\n".join(part for part in parts if part)


@pytest.fixture
def linked(patient):
    line_id = "U" + "5e" * 16
    with db() as conn:
        conn.execute(
            "UPDATE patients SET line_user_id=? WHERE patient_id=?",
            (line_id, patient["patient_id"]),
        )
    patient["line_user_id"] = line_id
    return patient


def test_status_reply_without_plan(linked):
    assert "ไม่มีแผนกินยา" in _flatten(line_messages.status_reply(linked))


def test_status_reply_with_todays_dose(linked, dose_token):
    text = _flatten(line_messages.status_reply(linked))
    assert "3.0" in text or "3" in text
    assert "สถานะยา" in text


def test_dose_reply_includes_confirmation_link(linked, dose_token):
    text = _flatten(line_messages.dose_reply(linked))
    assert dose_token["token_id"] in text


def test_dose_reply_without_plan(linked):
    assert "ไม่มีแผนกินยา" in _flatten(line_messages.dose_reply(linked))


def test_adherence_reply_reports_both_windows(linked):
    with db() as conn:
        for offset in range(1, 5):
            conn.execute(
                "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
                "warfarin_mg, status, created_at) VALUES (?,?,?,?,'taken',?)",
                (linked["patient_id"], days_ago(offset), "18:00", 3.0, now()),
            )
    text = _flatten(line_messages.adherence_reply(linked))
    assert "7 วัน" in text
    assert "30 วัน" in text


def test_inr_reply_without_results(linked):
    assert "ยังไม่มีผลการตรวจ" in _flatten(line_messages.inr_reply(linked))


def test_inr_reply_lists_recent_results(linked):
    with db() as conn:
        conn.execute(
            "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
            "VALUES (?,'INR',?,?,1,?)",
            (linked["patient_id"], 2.7, today(), now()),
        )
    text = _flatten(line_messages.inr_reply(linked))
    assert "2.7" in text


@pytest.mark.parametrize(
    "days,expected",
    [(0, "💪"), (3, "🎯"), (9, "🥈"), (16, "🥇"), (32, "🏆")],
)
def test_streak_reply_tiers(linked, days, expected):
    with db() as conn:
        for offset in range(1, days + 1):
            conn.execute(
                "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
                "warfarin_mg, status, created_at) VALUES (?,?,?,?,'taken',?)",
                (linked["patient_id"], days_ago(offset), "18:00", 3.0, now()),
            )
    assert expected in _flatten(line_messages.streak_reply(linked))


def test_appointment_reply_without_bookings(linked):
    assert "ยังไม่มีนัดหมาย" in _flatten(line_messages.appointment_reply(linked))


def test_appointment_reply_lists_next_visit(linked):
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    when = (now_dt() + timedelta(days=5)).strftime("%Y-%m-%d")
    with db() as conn:
        create_appointment(
            conn, linked["patient_id"],
            {"appointment_date": when, "appointment_type": "inr",
             "location": "คลินิกวาร์ฟาริน"},
            "pytest",
        )
    text = _flatten(line_messages.appointment_reply(linked))
    assert "ตรวจ INR" in text


def test_stock_reply_warns_when_low(linked):
    from datetime import timedelta

    from warfarin.doses import create_plan
    from warfarin.time_utils import now_dt

    start = now_dt().date()
    with db() as conn:
        create_plan(
            conn, linked["patient_id"], start.isoformat(),
            (start + timedelta(days=6)).isoformat(),
            {index: 3.0 for index in range(7)}, performed_by="pytest",
        )
        conn.execute(
            "UPDATE patients SET pill_inventory=3 WHERE patient_id=?",
            (linked["patient_id"],),
        )
    linked["pill_inventory"] = 3
    text = _flatten(line_messages.stock_reply(linked))
    assert "ยาใกล้หมด" in text


def test_stock_reply_without_data(linked):
    linked["pill_inventory"] = 0
    assert "ยาคงเหลือ" in _flatten(line_messages.stock_reply(linked))


def test_education_messages_cover_key_topics():
    text = _flatten(line_messages.education_messages())
    assert "วาร์ฟาริน" in text


def test_help_text_lists_all_commands():
    for command in ("สถานะ", "ยา", "ความสม่ำเสมอ", "ผลเลือด", "นัด", "อาการ", "ความรู้"):
        assert command in line_messages.HELP_TEXT, command


def test_welcome_text_formats_hospital_name():
    text = line_messages.WELCOME_TEXT.format(hospital="รพ.ทดสอบ")
    assert "รพ.ทดสอบ" in text
    assert "ลงทะเบียน" in text


def test_as_messages_truncates_over_limit():
    from warfarin.line_service import MAX_TEXT

    messages = line_messages.as_messages("x" * (MAX_TEXT + 500))
    body = _flatten(messages)
    assert len(body) <= MAX_TEXT + 10
