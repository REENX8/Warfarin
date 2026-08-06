"""Patient CRUD, validation and the detail page."""
import pytest

from warfarin.db import db, read_db
from warfarin.patients import (
    ValidationError,
    clean_patient_form,
    create_patient,
    get_patient_by_token,
    search_patients,
    update_patient,
)


def test_patient_list_renders(admin_client, patient):
    response = admin_client.get("/patients")
    assert response.status_code == 200
    assert patient["full_name"] in response.text


def test_patient_search_by_hn(admin_client, patient):
    response = admin_client.get(f"/patients?q={patient['hn']}")
    assert response.status_code == 200
    assert patient["full_name"] in response.text


def test_patient_detail_renders(admin_client, patient):
    response = admin_client.get(f"/patients/{patient['patient_id']}")
    assert response.status_code == 200
    assert patient["full_name"] in response.text


def test_unknown_patient_returns_404(admin_client):
    assert admin_client.get("/patients/999999").status_code == 404


def test_create_patient_via_form(admin_client):
    response = admin_client.post(
        "/patients/new",
        data={
            "full_name": "สมชาย ทดสอบ",
            "hn": "HN-CREATE-1",
            "age_years": "58",
            "target_inr_min": "2.0",
            "target_inr_max": "3.0",
            "indication": "af",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        rows = search_patients(conn, "HN-CREATE-1")
    assert len(rows) == 1
    assert rows[0]["access_token"]


def test_create_patient_rejects_blank_name(admin_client):
    response = admin_client.post("/patients/new", data={"full_name": "  "})
    assert response.status_code == 400
    assert "กรุณากรอกชื่อ" in response.text


# --- validation -------------------------------------------------------------
def test_clean_form_normalises_blank_optional_fields():
    data = clean_patient_form({"full_name": "ทดสอบ", "hn": "", "phone": ""})
    assert data["hn"] is None
    assert data["phone"] is None
    assert data["target_inr_min"] == 2.0


def test_clean_form_rejects_inverted_inr_range():
    with pytest.raises(ValidationError) as info:
        clean_patient_form(
            {"full_name": "ทดสอบ", "target_inr_min": "3.5", "target_inr_max": "2.0"}
        )
    assert "target_inr_max" in info.value.errors


def test_clean_form_rejects_bad_line_id():
    with pytest.raises(ValidationError) as info:
        clean_patient_form({"full_name": "ทดสอบ", "line_user_id": "not-a-line-id"})
    assert "line_user_id" in info.value.errors


def test_clean_form_rejects_non_numeric_weight():
    with pytest.raises(ValidationError) as info:
        clean_patient_form({"full_name": "ทดสอบ", "weight_kg": "หนักมาก"})
    assert "weight_kg" in info.value.errors


def test_duplicate_hn_is_rejected(patient):
    with pytest.raises(ValidationError) as info, db() as conn:
        create_patient(
            conn, clean_patient_form({"full_name": "ซ้ำ", "hn": patient["hn"]}), "pytest"
        )
    assert "hn" in info.value.errors


def test_duplicate_line_id_is_rejected(patient):
    line_id = "U" + "a" * 32
    with db() as conn:
        update_patient(
            conn,
            patient["patient_id"],
            clean_patient_form({
                "full_name": patient["full_name"],
                "hn": patient["hn"],
                "line_user_id": line_id,
            }),
            "pytest",
        )
    with pytest.raises(ValidationError) as info, db() as conn:
        create_patient(
            conn,
            clean_patient_form({"full_name": "อีกคน", "line_user_id": line_id}),
            "pytest",
        )
    assert "line_user_id" in info.value.errors


# --- lifecycle --------------------------------------------------------------
def test_deactivate_and_reactivate(admin_client, patient):
    pid = patient["patient_id"]
    admin_client.post(f"/patients/{pid}/deactivate", follow_redirects=False)
    with read_db() as conn:
        assert conn.execute(
            "SELECT active FROM patients WHERE patient_id=?", (pid,)
        ).fetchone()[0] == 0

    admin_client.post(f"/patients/{pid}/reactivate", follow_redirects=False)
    with read_db() as conn:
        assert conn.execute(
            "SELECT active FROM patients WHERE patient_id=?", (pid,)
        ).fetchone()[0] == 1


def test_access_token_is_unguessable_and_resolves(patient):
    with read_db() as conn:
        found = get_patient_by_token(conn, patient["access_token"])
    assert found["patient_id"] == patient["patient_id"]
    assert len(patient["access_token"]) >= 20
    with read_db() as conn:
        assert get_patient_by_token(conn, "not-a-real-token") is None


def test_inventory_update(admin_client, patient):
    pid = patient["patient_id"]
    admin_client.post(
        f"/patients/{pid}/inventory", data={"pill_inventory": "30"}, follow_redirects=False
    )
    with read_db() as conn:
        assert conn.execute(
            "SELECT pill_inventory FROM patients WHERE patient_id=?", (pid,)
        ).fetchone()[0] == 30


def test_inventory_rejects_non_numeric(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/inventory",
        data={"pill_inventory": "สามสิบ"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "danger" in response.headers["location"]
