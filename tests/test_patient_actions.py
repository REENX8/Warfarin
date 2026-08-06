"""Form handlers on the patient detail page."""
from datetime import timedelta

from warfarin.db import db, read_db
from warfarin.time_utils import now_dt, today


def _in_days(count: int) -> str:
    return (now_dt() + timedelta(days=count)).strftime("%Y-%m-%d")


def test_create_plan_over_http(admin_client, patient):
    pid = patient["patient_id"]
    response = admin_client.post(
        f"/patients/{pid}/doses",
        data={
            "start_date": today(), "end_date": _in_days(6),
            "scheduled_time": "18:00", "warfarin_mg": "3",
            "pill_description": "เม็ดสีชมพู",
            **{f"dose_day_{index}": "3" for index in range(7)},
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM medication_plan WHERE patient_id=?", (pid,)
        ).fetchone()[0]
        tokens = conn.execute(
            "SELECT COUNT(*) FROM dose_tokens dt JOIN medication_plan mp "
            "ON dt.dose_id=mp.dose_id WHERE mp.patient_id=?",
            (pid,),
        ).fetchone()[0]
    assert count == 7
    assert tokens == 7, "every dose needs a confirmation token"


def test_create_plan_reports_validation_error(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/doses",
        data={"start_date": _in_days(5), "end_date": today(), "warfarin_mg": "3"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "kind=danger" in response.headers["location"]


def test_plan_with_different_daily_doses(admin_client, patient):
    """Warfarin is commonly dosed as alternating strengths across the week."""
    pid = patient["patient_id"]
    doses = {0: "3", 1: "1.5", 2: "3", 3: "1.5", 4: "3", 5: "1.5", 6: "3"}
    admin_client.post(
        f"/patients/{pid}/doses",
        data={
            "start_date": today(), "end_date": _in_days(6), "warfarin_mg": "3",
            **{f"dose_day_{index}": value for index, value in doses.items()},
        },
        follow_redirects=False,
    )
    with read_db() as conn:
        total = conn.execute(
            "SELECT SUM(warfarin_mg) FROM medication_plan WHERE patient_id=?", (pid,)
        ).fetchone()[0]
    assert total == 16.5


def test_large_dose_change_records_an_adjustment(admin_client, patient):
    pid = patient["patient_id"]
    admin_client.post(
        f"/patients/{pid}/doses",
        data={
            "start_date": today(), "end_date": _in_days(6), "warfarin_mg": "3",
            **{f"dose_day_{index}": "3" for index in range(7)},
        },
        follow_redirects=False,
    )
    admin_client.post(
        f"/patients/{pid}/doses",
        data={
            "start_date": _in_days(7), "end_date": _in_days(13), "warfarin_mg": "4",
            "adjust_reason": "INR ต่ำกว่าเป้าหมาย",
            **{f"dose_day_{index}": "4" for index in range(7)},
        },
        follow_redirects=False,
    )
    with read_db() as conn:
        rows = conn.execute(
            "SELECT * FROM dose_adjustments WHERE patient_id=?", (pid,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reason"] == "INR ต่ำกว่าเป้าหมาย"


def test_dose_status_override_over_http(admin_client, patient, dose_token):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/doses/{dose_token['dose_id']}/status",
        data={"status": "taken"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        assert conn.execute(
            "SELECT status FROM medication_plan WHERE dose_id=?", (dose_token["dose_id"],)
        ).fetchone()[0] == "taken"


def test_dose_status_override_rejects_bad_status(admin_client, patient, dose_token):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/doses/{dose_token['dose_id']}/status",
        data={"status": "vaporised"},
        follow_redirects=False,
    )
    assert "kind=danger" in response.headers["location"]


def test_appointment_creation_over_http(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/appointments",
        data={"appointment_date": _in_days(14), "appointment_type": "inr",
              "location": "คลินิกวาร์ฟาริน"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        row = conn.execute(
            "SELECT next_inr_date FROM patients WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()
    assert row[0] == _in_days(14)


def test_appointment_status_update_over_http(admin_client, patient):
    admin_client.post(
        f"/patients/{patient['patient_id']}/appointments",
        data={"appointment_date": _in_days(3)}, follow_redirects=False,
    )
    with read_db() as conn:
        appointment_id = conn.execute(
            "SELECT appointment_id FROM appointments WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()[0]
    admin_client.post(
        f"/patients/{patient['patient_id']}/appointments/{appointment_id}/status",
        data={"status": "attended"}, follow_redirects=False,
    )
    with read_db() as conn:
        assert conn.execute(
            "SELECT status FROM appointments WHERE appointment_id=?", (appointment_id,)
        ).fetchone()[0] == "attended"


def test_test_score_is_recorded(admin_client, patient):
    admin_client.post(
        f"/patients/{patient['patient_id']}/test-score",
        data={"test_type": "pre", "score": "12", "max_score": "20"},
        follow_redirects=False,
    )
    with read_db() as conn:
        row = conn.execute(
            "SELECT test_type, score FROM test_scores WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()
    assert row["test_type"] == "pre"
    assert row["score"] == 12


def test_test_score_rejects_out_of_range(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/test-score",
        data={"score": "30", "max_score": "20"}, follow_redirects=False,
    )
    assert "kind=danger" in response.headers["location"]


def test_survey_page_and_submission(admin_client, patient):
    pid = patient["patient_id"]
    assert admin_client.get(f"/patients/{pid}/survey").status_code == 200
    admin_client.post(
        f"/patients/{pid}/survey",
        data={
            "survey_date": today(), "ease_of_use": "5",
            "line_satisfaction": "4", "reminder_helpful": "5",
            "comments": "ใช้งานง่ายดี",
        },
        follow_redirects=False,
    )
    with read_db() as conn:
        row = conn.execute(
            "SELECT ease_of_use, comments FROM satisfaction_surveys WHERE patient_id=?",
            (pid,),
        ).fetchone()
    assert row["ease_of_use"] == 5
    assert row["comments"] == "ใช้งานง่ายดี"


def test_survey_clamps_out_of_range_scores(admin_client, patient):
    admin_client.post(
        f"/patients/{patient['patient_id']}/survey",
        data={"ease_of_use": "99", "line_satisfaction": "-3", "reminder_helpful": "x"},
        follow_redirects=False,
    )
    with read_db() as conn:
        row = conn.execute(
            "SELECT ease_of_use, line_satisfaction, reminder_helpful "
            "FROM satisfaction_surveys WHERE patient_id=? ORDER BY survey_id DESC LIMIT 1",
            (patient["patient_id"],),
        ).fetchone()
    assert row["ease_of_use"] == 5
    assert row["line_satisfaction"] == 1
    assert row["reminder_helpful"] == 3


def test_patient_edit_saves_caregiver(admin_client, patient):
    pid = patient["patient_id"]
    admin_client.post(
        f"/patients/{pid}/edit",
        data={
            "full_name": patient["full_name"], "hn": patient["hn"],
            "target_inr_min": "2.0", "target_inr_max": "3.0",
            "caregiver_name": "ลูกสาว", "caregiver_relationship": "บุตร",
            "caregiver_phone": "0812345678", "caregiver_notify": "on",
        },
        follow_redirects=False,
    )
    with read_db() as conn:
        row = conn.execute(
            "SELECT name, notify_enabled FROM caregivers WHERE patient_id=?", (pid,)
        ).fetchone()
    assert row["name"] == "ลูกสาว"
    assert row["notify_enabled"] == 1


def test_clearing_caregiver_name_removes_the_record(admin_client, patient):
    pid = patient["patient_id"]
    base = {
        "full_name": patient["full_name"], "hn": patient["hn"],
        "target_inr_min": "2.0", "target_inr_max": "3.0",
    }
    admin_client.post(
        f"/patients/{pid}/edit", data={**base, "caregiver_name": "ลูกชาย"},
        follow_redirects=False,
    )
    admin_client.post(
        f"/patients/{pid}/edit", data={**base, "caregiver_name": ""},
        follow_redirects=False,
    )
    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM caregivers WHERE patient_id=?", (pid,)
        ).fetchone()[0]
    assert count == 0


def test_edit_rejects_invalid_data_and_redisplays_form(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/edit",
        data={"full_name": "", "hn": patient["hn"]},
    )
    assert response.status_code == 400
    assert "ข้อมูลไม่ถูกต้อง" in response.text
