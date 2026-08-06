"""Every page must render — a sweep that catches template regressions.

Jinja silently swallows undefined names, so these assertions check for
content that only appears when the route actually passed its data through.
"""
import pytest

from warfarin.appointments import add_lab_result, create_appointment
from warfarin.db import db, read_db
from warfarin.doses import create_plan
from warfarin.staff import create_staff, get_by_username
from warfarin.symptoms import create_report
from warfarin.time_utils import today


@pytest.fixture
def rich_patient(patient):
    """A patient with the full range of data every panel needs."""
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    pid = patient["patient_id"]
    start = now_dt().date()
    with db() as conn:
        create_plan(
            conn, pid, start.isoformat(), (start + timedelta(days=13)).isoformat(),
            dict.fromkeys(range(7), 3.0), performed_by="pytest",
        )
        add_lab_result(conn, patient, {"value": "2.6", "test_date": today()}, "pytest")
        create_appointment(
            conn, pid,
            {"appointment_date": (start + timedelta(days=21)).isoformat(),
             "appointment_type": "inr", "location": "คลินิกวาร์ฟาริน"},
            "pytest",
        )
        create_report(conn, patient, {"severity": "3", "bleeding": "1"})
        conn.execute(
            "UPDATE patients SET pill_inventory=20, chronic_conditions=? WHERE patient_id=?",
            ("ความดันสูง กิน ibuprofen เป็นบางครั้ง", pid),
        )
        conn.execute(
            "INSERT INTO test_scores (patient_id, test_type, score, max_score, taken_at) "
            "VALUES (?,'pre',12,20,?)",
            (pid, today()),
        )
    return patient


STAFF_PAGES = [
    ("/dashboard", "แดชบอร์ด"),
    ("/dashboard/at-risk", "ผู้ป่วยที่ต้องติดตาม"),
    ("/dashboard/missed-today", "ยังไม่ยืนยัน"),
    ("/patients", "ผู้ป่วย"),
    ("/patients/new", "เพิ่มผู้ป่วยใหม่"),
    ("/appointments", "ตารางนัดหมาย"),
    ("/symptoms", "รายงานอาการจากผู้ป่วย"),
    ("/reports", "รายงานผลลัพธ์"),
    ("/notifications", "ประวัติการแจ้งเตือน"),
    ("/audit", "บันทึกการใช้งานระบบ"),
    ("/staff", "บัญชีผู้ใช้งาน"),
    ("/staff/new", "เพิ่มบัญชีผู้ใช้"),
    ("/system", "สถานะระบบ"),
    ("/scan", "สแกน QR"),
    ("/account", "บัญชีของฉัน"),
    ("/account/password", "เปลี่ยนรหัสผ่าน"),
]


@pytest.mark.parametrize("path,marker", STAFF_PAGES)
def test_staff_pages_render(admin_client, rich_patient, path, marker):
    response = admin_client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert marker in response.text, f"{path} is missing expected content"


PATIENT_PAGE_SUFFIXES = ["", "/edit", "/qr-sheet", "/schedule", "/survey"]


@pytest.mark.parametrize("suffix", PATIENT_PAGE_SUFFIXES)
def test_patient_pages_render(admin_client, rich_patient, suffix):
    path = f"/patients/{rich_patient['patient_id']}{suffix}"
    response = admin_client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert rich_patient["full_name"] in response.text


def test_patient_detail_shows_every_panel(admin_client, rich_patient):
    response = admin_client.get(f"/patients/{rich_patient['patient_id']}")
    text = response.text
    for marker in (
        "แนวโน้มค่า INR",
        "บันทึกผลตรวจ INR",
        "ปฏิทินการกินยา 90 วัน",
        "สร้าง / ต่อแผนการกินยา",
        "นัดหมาย",
        "ประวัติการกินยา",
        "ยาคงเหลือ",
        "รายงานอาการล่าสุด",
        "ประวัติการปรับขนาดยา",
        "คะแนนแบบทดสอบความรู้",
        "แบบสอบถามความพึงพอใจ",
    ):
        assert marker in text, f"missing panel: {marker}"


def test_patient_detail_surfaces_interaction_warning(admin_client, rich_patient):
    """The notes mention ibuprofen, so the interaction banner must appear."""
    response = admin_client.get(f"/patients/{rich_patient['patient_id']}")
    assert "ปฏิกิริยากับวาร์ฟาริน" in response.text


def test_staff_edit_page_renders(admin_client):
    with db() as conn:
        create_staff(conn, "render-check", "render-password-1", "ทดสอบ", "nurse", "pytest")
        account = get_by_username(conn, "render-check")
    response = admin_client.get(f"/staff/{account['staff_id']}/edit")
    assert response.status_code == 200
    assert "render-check" in response.text
    assert "รีเซ็ตรหัสผ่าน" in response.text


PUBLIC_PAGES = ["/login", "/education", "/ping", "/healthz"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(anon_client, path):
    assert anon_client.get(path).status_code == 200


def test_patient_facing_pages_render(anon_client, rich_patient, dose_token):
    token = rich_patient["access_token"]
    for path in (f"/p/{token}", f"/report/symptom/{token}", f"/dose/{dose_token['token_id']}"):
        response = anon_client.get(path)
        assert response.status_code == 200, path
        assert rich_patient["full_name"] in response.text, path


def test_pages_do_not_leak_jinja_errors(admin_client, rich_patient):
    """A stray '{{' or 'Undefined' in the output means a broken template."""
    for path, _ in STAFF_PAGES:
        text = admin_client.get(path).text
        assert "{{" not in text, f"unrendered Jinja expression on {path}"
        assert "Undefined" not in text, f"undefined value rendered on {path}"


def test_csrf_token_is_present_in_every_form(admin_client, rich_patient):
    """A form without a token would break the moment CSRF is enabled."""
    import re

    for path, _ in STAFF_PAGES:
        text = admin_client.get(path).text
        for form in re.findall(r"<form\b.*?</form>", text, re.DOTALL):
            if 'method="POST"' not in form and "method=\"post\"" not in form.lower():
                continue
            assert 'name="csrf_token"' in form, f"form without CSRF token on {path}"


def test_public_forms_carry_csrf_token(anon_client, rich_patient, dose_token):
    import re

    for path in (
        "/login",
        f"/report/symptom/{rich_patient['access_token']}",
        f"/dose/{dose_token['token_id']}",
    ):
        text = anon_client.get(path).text
        for form in re.findall(r"<form\b.*?</form>", text, re.DOTALL):
            if "post" not in form.lower():
                continue
            assert 'name="csrf_token"' in form, f"form without CSRF token on {path}"
