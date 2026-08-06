"""Symptom intake, triage, ticket codes and pharmacist replies."""
import pytest

from warfarin import symptoms as service
from warfarin.db import db, read_db


def _create(patient, **overrides):
    form = {"severity": "2", "bruising": "1"}
    form.update(overrides)
    with db() as conn:
        return service.create_report(conn, patient, form)


def test_symptom_form_requires_valid_token(anon_client):
    assert anon_client.get("/report/symptom/not-a-token").status_code == 404


def test_symptom_form_renders_for_valid_token(anon_client, patient):
    response = anon_client.get(f"/report/symptom/{patient['access_token']}")
    assert response.status_code == 200
    assert patient["full_name"] in response.text


def test_symptom_submission_creates_report(anon_client, patient):
    response = anon_client.post(
        f"/report/symptom/{patient['access_token']}",
        data={"severity": "4", "bleeding": "1", "other": "เลือดออกตามไรฟัน"},
    )
    assert response.status_code == 200
    assert "ส่งรายงานเรียบร้อย" in response.text
    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM symptom_reports WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()[0]
    assert count == 1


def test_urgent_report_shows_emergency_guidance(anon_client, patient):
    response = anon_client.post(
        f"/report/symptom/{patient['access_token']}",
        data={"severity": "5", "bleeding": "1"},
    )
    assert "ทันที" in response.text


def test_ticket_codes_are_unique_across_open_reports(patient):
    first = _create(patient)
    second = _create(patient)
    assert first["ticket_code"] != second["ticket_code"]


def test_ticket_code_is_recycled_after_resolution(patient):
    first = _create(patient)
    with db() as conn:
        service.resolve(conn, first["report_id"], "pytest")
    second = _create(patient)
    assert second["ticket_code"] == first["ticket_code"]


def test_find_open_by_ticket_ignores_resolved(patient):
    created = _create(patient)
    with read_db() as conn:
        assert service.find_open_by_ticket(conn, created["ticket_code"]) is not None
    with db() as conn:
        service.resolve(conn, created["report_id"], "pytest")
        assert service.find_open_by_ticket(conn, created["ticket_code"]) is None


def test_find_open_by_ticket_is_case_insensitive(patient):
    created = _create(patient)
    with read_db() as conn:
        found = service.find_open_by_ticket(conn, created["ticket_code"].lower())
    assert found is not None


def test_reply_marks_report_replied(patient):
    created = _create(patient)
    with db() as conn:
        report = service.record_reply(conn, created["report_id"], "พักผ่อนมาก ๆ นะคะ", "ภก.สมหญิง")
    assert report is not None
    with read_db() as conn:
        row = conn.execute(
            "SELECT status, pharmacist_reply, replied_by FROM symptom_reports WHERE report_id=?",
            (created["report_id"],),
        ).fetchone()
    assert row[0] == "replied"
    assert row[1] == "พักผ่อนมาก ๆ นะคะ"
    assert row[2] == "ภก.สมหญิง"


def test_reply_to_missing_report_returns_none():
    with db() as conn:
        assert service.record_reply(conn, 999999, "hello", "staff") is None


def test_open_count_tracks_new_reports(patient):
    before = service.open_count()
    _create(patient)
    assert service.open_count() == before + 1


# --- staff pages ------------------------------------------------------------
def test_symptom_list_requires_login(anon_client):
    response = anon_client.get("/symptoms", follow_redirects=False)
    assert response.status_code == 303


def test_symptom_list_shows_report(admin_client, patient):
    created = _create(patient)
    response = admin_client.get("/symptoms")
    assert response.status_code == 200
    assert created["ticket_code"] in response.text


def test_staff_reply_over_http(admin_client, patient):
    created = _create(patient)
    response = admin_client.post(
        f"/symptoms/{created['report_id']}/reply",
        data={"reply": "ให้มาพบเภสัชกรพรุ่งนี้"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        status = conn.execute(
            "SELECT status FROM symptom_reports WHERE report_id=?", (created["report_id"],)
        ).fetchone()[0]
    assert status == "replied"


def test_staff_reply_rejects_empty_text(admin_client, patient):
    created = _create(patient)
    response = admin_client.post(
        f"/symptoms/{created['report_id']}/reply", data={"reply": "   "},
        follow_redirects=False,
    )
    assert "danger" in response.headers["location"]


def test_resolve_over_http(admin_client, patient):
    created = _create(patient)
    admin_client.post(f"/symptoms/{created['report_id']}/resolve", follow_redirects=False)
    with read_db() as conn:
        status = conn.execute(
            "SELECT status FROM symptom_reports WHERE report_id=?", (created["report_id"],)
        ).fetchone()[0]
    assert status == "resolved"


# --- LINE recipients --------------------------------------------------------
def test_recipient_registration_is_idempotent():
    line_id = "U" + "9" * 32
    service.register_recipient(line_id)
    service.register_recipient(line_id)
    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM line_recipients WHERE line_user_id=?", (line_id,)
        ).fetchone()[0]
    assert count == 1


def test_recipient_can_be_deactivated_and_reactivated():
    line_id = "U" + "b" * 32
    service.register_recipient(line_id)
    assert service.deactivate_recipient(line_id) is True
    assert service.active_recipient(line_id) is None
    service.register_recipient(line_id)
    assert service.active_recipient(line_id) is not None
