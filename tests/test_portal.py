"""Patient self-service portal and public pages."""
from warfarin.db import db
from warfarin.time_utils import now, today


def test_portal_requires_valid_token(anon_client):
    assert anon_client.get("/p/not-a-real-token").status_code == 404


def test_portal_renders_for_patient(anon_client, patient):
    response = anon_client.get(f"/p/{patient['access_token']}")
    assert response.status_code == 200
    assert patient["full_name"] in response.text
    assert "ความสม่ำเสมอในการกินยา" in response.text


def test_portal_shows_todays_dose(anon_client, patient, dose_token):
    response = anon_client.get(f"/p/{patient['access_token']}")
    assert "ยาของวันนี้" in response.text
    assert dose_token["token_id"] in response.text


def test_portal_shows_inr_result(anon_client, patient):
    with db() as conn:
        conn.execute(
            "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
            "VALUES (?,'INR',?,?,1,?)",
            (patient["patient_id"], 2.6, today(), now()),
        )
    response = anon_client.get(f"/p/{patient['access_token']}")
    assert "2.6" in response.text


def test_portal_hides_deactivated_patients(anon_client, patient):
    with db() as conn:
        conn.execute(
            "UPDATE patients SET active=0 WHERE patient_id=?", (patient["patient_id"],)
        )
    assert anon_client.get(f"/p/{patient['access_token']}").status_code == 404


def test_education_page_is_public(anon_client):
    response = anon_client.get("/education")
    assert response.status_code == 200
    for expected in ("วิตามินเค", "สัญญาณอันตราย", "Rifampicin", "ลืมกินยา"):
        assert expected in response.text, expected


def test_education_lists_interaction_table(anon_client):
    response = anon_client.get("/education")
    assert "Amiodarone" in response.text
    assert "เพิ่ม INR" in response.text


def test_error_page_renders_for_unknown_route(anon_client):
    response = anon_client.get("/definitely-not-a-page")
    assert response.status_code == 404


def test_healthz_reports_schema_version(anon_client):
    payload = anon_client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["schema_version"] == payload["schema_latest"]


def test_ping_is_plain_text(anon_client):
    response = anon_client.get("/ping")
    assert response.status_code == 200
    assert response.text == "ok"
