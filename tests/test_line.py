"""LINE webhook, command routing and broadcast targeting."""
import base64
import hashlib
import hmac
import json

import pytest

from warfarin import line_messages, line_service
from warfarin.config import get_settings, reset_settings_cache
from warfarin.db import db, read_db
from warfarin.routers.line_bp import TICKET_RE, filter_broadcast_targets
from warfarin.time_utils import days_ago, now, today


def _signature(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


# --- signature verification -------------------------------------------------
def test_signature_roundtrip():
    body = b'{"events":[]}'
    assert line_service.verify_signature(body, _signature("s3cret", body), "s3cret")


def test_signature_rejects_tampered_body():
    secret = "s3cret"
    signature = _signature(secret, b'{"events":[]}')
    assert not line_service.verify_signature(b'{"events":[1]}', signature, secret)


def test_signature_rejects_empty_inputs():
    assert not line_service.verify_signature(b"x", "", "secret")
    assert not line_service.verify_signature(b"x", "sig", "")


# --- webhook endpoint -------------------------------------------------------
def test_webhook_accepts_empty_verification_request(anon_client):
    response = anon_client.post("/webhook", content=b"")
    assert response.status_code == 200


def test_webhook_reports_unconfigured_when_no_secret(anon_client):
    """With no channel secret the endpoint must not pretend to work."""
    assert not get_settings().line_webhook_enabled
    response = anon_client.post("/webhook", content=b'{"events":[]}')
    assert response.status_code == 200
    assert "not configured" in response.json()["status"]


def test_webhook_rejects_bad_signature(anon_client, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "webhook-secret")
    reset_settings_cache()
    try:
        response = anon_client.post(
            "/webhook", content=b'{"events":[]}',
            headers={"X-Line-Signature": "obviously-wrong"},
        )
        assert response.status_code == 401
    finally:
        monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
        reset_settings_cache()


def test_webhook_accepts_valid_signature(anon_client, monkeypatch):
    secret = "webhook-secret"
    monkeypatch.setenv("LINE_CHANNEL_SECRET", secret)
    reset_settings_cache()
    try:
        body = json.dumps({"events": []}).encode()
        response = anon_client.post(
            "/webhook", content=body,
            headers={"X-Line-Signature": _signature(secret, body)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
        reset_settings_cache()


# --- command routing --------------------------------------------------------
def _text(messages) -> str:
    parts = []
    for message in messages:
        if isinstance(message, str):
            parts.append(message)
        else:
            parts.append(getattr(message, "text", "") or getattr(message, "alt_text", ""))
    return "\n".join(parts)


def test_help_command_lists_menu():
    assert "เมนูคำสั่ง" in _text(line_messages.route_command("Uunknown", "help"))


def test_education_command_works_without_registration():
    assert "วาร์ฟาริน" in _text(line_messages.route_command("Uunknown", "ความรู้"))


def test_unregistered_user_is_prompted_to_register():
    assert "ลงทะเบียน" in _text(line_messages.route_command("Uunknown", "สถานะ"))


def test_registration_links_line_account(patient):
    line_id = "U" + "1" * 32
    reply = _text(line_messages.route_command(line_id, f"ลงทะเบียน {patient['hn']}"))
    assert "ลงทะเบียนสำเร็จ" in reply
    with read_db() as conn:
        stored = conn.execute(
            "SELECT line_user_id FROM patients WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()[0]
    assert stored == line_id


def test_registration_rejects_unknown_hn():
    reply = _text(line_messages.route_command("U" + "2" * 32, "ลงทะเบียน NOPE-9999"))
    assert "ไม่พบผู้ป่วย" in reply


def test_registration_refuses_hn_owned_by_another_account(patient):
    first = "U" + "3" * 32
    line_messages.route_command(first, f"ลงทะเบียน {patient['hn']}")
    reply = _text(line_messages.route_command("U" + "4" * 32, f"ลงทะเบียน {patient['hn']}"))
    assert "ลงทะเบียนกับบัญชี LINE อื่น" in reply


def test_registration_without_hn_asks_for_it():
    assert "ตัวอย่าง" in _text(line_messages.route_command("U" + "5" * 32, "ลงทะเบียน"))


def test_registered_patient_gets_status(patient):
    line_id = "U" + "6" * 32
    line_messages.route_command(line_id, f"ลงทะเบียน {patient['hn']}")
    assert "สถานะยา" in _text(line_messages.route_command(line_id, "สถานะ"))


def test_unknown_command_gets_help_hint(patient):
    line_id = "U" + "7" * 32
    line_messages.route_command(line_id, f"ลงทะเบียน {patient['hn']}")
    assert "ไม่เข้าใจคำสั่ง" in _text(line_messages.route_command(line_id, "อยากกินข้าว"))


def test_symptom_command_uses_unguessable_token(patient):
    line_id = "U" + "8" * 32
    line_messages.route_command(line_id, f"ลงทะเบียน {patient['hn']}")
    reply = _text(line_messages.route_command(line_id, "อาการ"))
    assert patient["access_token"] in reply
    assert f"/report/symptom/{patient['patient_id']}" not in reply


# --- ticket replies ---------------------------------------------------------
@pytest.mark.parametrize(
    "text,code,body",
    [
        ("A01+กินยาพร้อมอาหาร", "A01", "กินยาพร้อมอาหาร"),
        ("b12 ให้มาพบเภสัชกร", "b12", "ให้มาพบเภสัชกร"),
        ("Z99+", "Z99", ""),
    ],
)
def test_ticket_pattern_parses(text, code, body):
    match = TICKET_RE.match(text)
    assert match is not None
    assert match.group(1) == code
    assert match.group(2).strip() == body


def test_ticket_pattern_ignores_plain_text():
    assert TICKET_RE.match("สถานะ") is None


# --- broadcast targeting ----------------------------------------------------
def test_broadcast_all_returns_everyone(patient):
    with read_db() as conn:
        assert filter_broadcast_targets(conn, [patient], "all") == [patient]


def test_broadcast_missed_yesterday(patient):
    with db() as conn:
        conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
            "warfarin_mg, status, created_at) VALUES (?,?,?,?,'missed',?)",
            (patient["patient_id"], days_ago(1), "18:00", 3.0, now()),
        )
    with read_db() as conn:
        selected = filter_broadcast_targets(conn, [patient], "missed_yesterday")
    assert len(selected) == 1


def test_broadcast_inr_out_of_range(patient):
    with db() as conn:
        conn.execute(
            "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
            "VALUES (?,'INR',?,?,0,?)",
            (patient["patient_id"], 5.2, today(), now()),
        )
    with read_db() as conn:
        selected = filter_broadcast_targets(conn, [patient], "inr_out_of_range")
    assert len(selected) == 1


def test_broadcast_inr_due(patient):
    with db() as conn:
        conn.execute(
            "UPDATE patients SET next_inr_date=? WHERE patient_id=?",
            (days_ago(2), patient["patient_id"]),
        )
    patient["next_inr_date"] = days_ago(2)
    with read_db() as conn:
        assert len(filter_broadcast_targets(conn, [patient], "inr_due")) == 1


def test_broadcast_preview_requires_login(anon_client):
    assert anon_client.get("/api/broadcast-preview").status_code in (303, 401, 403)


def test_broadcast_preview_for_staff(admin_client):
    response = admin_client.get("/api/broadcast-preview?target=all")
    assert response.status_code == 200
    assert "count" in response.json()
