"""Reporting pages and exports."""
import io

from warfarin.db import db
from warfarin.time_utils import days_ago, now, today


def _seed_history(patient_id):
    with db() as conn:
        for offset in range(1, 11):
            conn.execute(
                "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
                "warfarin_mg, status, created_at) VALUES (?,?,?,?,?,?)",
                (patient_id, days_ago(offset), "18:00", 3.0,
                 "taken" if offset % 4 else "missed", now()),
            )
        for date_string, value in (("2026-01-01", 2.4), ("2026-01-21", 2.8)):
            conn.execute(
                "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
                "VALUES (?,'INR',?,?,1,?)",
                (patient_id, value, date_string, now()),
            )


def test_reports_page_renders(admin_client, patient):
    _seed_history(patient["patient_id"])
    response = admin_client.get("/reports")
    assert response.status_code == 200
    assert patient["full_name"] in response.text
    assert "TTR" in response.text


def test_reports_requires_login(anon_client):
    assert anon_client.get("/reports", follow_redirects=False).status_code == 303


def test_csv_export_has_bom_and_headers(admin_client, patient):
    _seed_history(patient["patient_id"])
    response = admin_client.get("/reports/export.csv")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf"), "Excel needs a UTF-8 BOM"
    text = response.content.decode("utf-8-sig")
    assert "adherence_30d_%" in text
    assert patient["full_name"] in text


def test_csv_export_filename_is_dated(admin_client):
    response = admin_client.get("/reports/export.csv")
    assert today() in response.headers["content-disposition"]


def test_xlsx_export_is_a_workbook(admin_client, patient):
    _seed_history(patient["patient_id"])
    response = admin_client.get("/reports/export.xlsx")
    if response.status_code == 501:
        import pytest

        pytest.skip("openpyxl is not installed in this environment")
    assert response.status_code == 200
    assert response.content[:2] == b"PK", "xlsx files are zip archives"

    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    assert "ชื่อ-นามสกุล" in headers
    assert sheet.max_row >= 2


def test_dashboard_worklists_render(admin_client, patient):
    _seed_history(patient["patient_id"])
    for path in ("/dashboard/at-risk", "/dashboard/missed-today", "/appointments"):
        assert admin_client.get(path).status_code == 200, path


def test_print_schedule_renders(admin_client, patient):
    response = admin_client.get(f"/patients/{patient['patient_id']}/schedule")
    assert response.status_code == 200
    assert "ตารางการกินยาวาร์ฟาริน" in response.text


def test_qr_sheet_renders(admin_client, patient, dose_token):
    response = admin_client.get(f"/patients/{patient['patient_id']}/qr-sheet")
    assert response.status_code == 200
    assert dose_token["token_id"] in response.text


def test_qr_png_is_served(anon_client, dose_token):
    response = anon_client.get(f"/qr/{dose_token['token_id']}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_qr_png_unknown_token_404(anon_client):
    assert anon_client.get("/qr/not-a-token.png").status_code == 404


def test_notification_and_audit_pages(admin_client):
    assert admin_client.get("/notifications").status_code == 200
    assert admin_client.get("/audit").status_code == 200
