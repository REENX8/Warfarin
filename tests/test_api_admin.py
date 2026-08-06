"""JSON API responses and admin console pages."""
from warfarin.db import db
from warfarin.time_utils import now, today


def test_inr_data_includes_target_band(admin_client, patient):
    with db() as conn:
        conn.execute(
            "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
            "VALUES (?,'INR',?,?,1,?)",
            (patient["patient_id"], 2.5, today(), now()),
        )
    payload = admin_client.get(f"/api/patients/{patient['patient_id']}/inr-data").json()
    assert payload["target_min"] == 2.0
    assert payload["target_max"] == 3.0
    assert payload["points"][0]["value"] == 2.5


def test_adherence_data_clamps_day_range(admin_client, patient):
    response = admin_client.get(
        f"/api/patients/{patient['patient_id']}/adherence-data?days=99999"
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_heatmap_data_length_is_clamped(admin_client, patient):
    payload = admin_client.get(
        f"/api/patients/{patient['patient_id']}/heatmap-data?days=1"
    ).json()
    assert len(payload) == 30  # minimum window


def test_api_404_for_unknown_patient(admin_client):
    assert admin_client.get("/api/patients/999999/inr-data").status_code == 404


def test_dashboard_stats_shape(admin_client):
    payload = admin_client.get("/api/dashboard-stats").json()
    for key in ("today_taken", "today_missed", "today_pending", "active_patients"):
        assert key in payload


def test_interaction_check_endpoint(admin_client, patient):
    payload = admin_client.get(
        f"/api/patients/{patient['patient_id']}/interactions?q=taking ibuprofen daily"
    ).json()
    assert payload["hits"]


def test_patient_summary_endpoint(admin_client, patient):
    payload = admin_client.get(f"/api/patients/{patient['patient_id']}/summary").json()
    assert payload["full_name"] == patient["full_name"]
    assert "adherence_30d" in payload


def test_lookup_requires_two_characters(admin_client, patient):
    assert admin_client.get("/api/lookup?q=a").json() == []
    results = admin_client.get(f"/api/lookup?q={patient['full_name'][:4]}").json()
    assert any(row["patient_id"] == patient["patient_id"] for row in results)


def test_notification_health_endpoint(admin_client):
    payload = admin_client.get("/api/health/notifications").json()
    assert "percent" in payload


# --- admin console ----------------------------------------------------------
def test_system_page_reports_schema_and_line_state(admin_client):
    response = admin_client.get("/system")
    assert response.status_code == 200
    assert "สถานะระบบ" in response.text
    assert "/webhook" in response.text


def test_manual_job_run(admin_client):
    response = admin_client.post(
        "/system/run-job", data={"job": "housekeeping"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "kind=success" in response.headers["location"]


def test_manual_job_run_rejects_unknown_job(admin_client):
    response = admin_client.post(
        "/system/run-job", data={"job": "drop-database"}, follow_redirects=False
    )
    assert "kind=danger" in response.headers["location"]


def test_audit_filter_by_action(admin_client, patient):
    response = admin_client.get("/audit?action=create")
    assert response.status_code == 200
    assert "create" in response.text


def test_notification_filter_by_type(admin_client):
    assert admin_client.get("/notifications?message_type=reminder_1").status_code == 200


def test_account_page(admin_client):
    response = admin_client.get("/account")
    assert response.status_code == 200
    assert "บัญชีของฉัน" in response.text


def test_scan_page_renders(admin_client):
    response = admin_client.get("/scan")
    assert response.status_code == 200
    assert "สแกน QR" in response.text


def test_portal_qr_requires_login(anon_client, patient):
    response = anon_client.get(
        f"/qr/portal/{patient['access_token']}.png", follow_redirects=False
    )
    assert response.status_code == 303


def test_portal_qr_for_staff(admin_client, patient):
    response = admin_client.get(f"/qr/portal/{patient['access_token']}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
