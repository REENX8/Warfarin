"""Clinical decision-support logic."""
import pytest

from warfarin.clinical import (
    INDICATIONS,
    assess_inr,
    days_of_stock,
    education_text,
    next_inr_interval_days,
    screen_medication_text,
    suggest_weekly_adjustment,
    symptom_labels,
    triage_symptom,
    weekly_total_mg,
)


@pytest.mark.parametrize(
    "value,expected_band",
    [
        (1.2, "critical_low"),
        (1.8, "low"),
        (2.5, "in_range"),
        (3.4, "high"),
        (4.5, "high"),
        (6.0, "critical_high"),
        (12.0, "critical_high"),
    ],
)
def test_inr_bands_for_standard_target(value, expected_band):
    assert assess_inr(value, 2.0, 3.0).band == expected_band


def test_inr_band_respects_patient_specific_target():
    """3.2 is high on a 2–3 target but in range on a mechanical mitral valve."""
    assert assess_inr(3.2, 2.0, 3.0).band == "high"
    assert assess_inr(3.2, 2.5, 3.5).band == "in_range"


def test_missing_inr_is_handled():
    assessment = assess_inr(None, 2.0, 3.0)
    assert assessment.band == "unknown"
    assert assessment.urgency == 0


def test_critical_values_are_same_day_urgency():
    assert assess_inr(1.1, 2.0, 3.0).urgency == 2
    assert assess_inr(9.5, 2.0, 3.0).urgency == 2
    assert assess_inr(2.5, 2.0, 3.0).urgency == 0


def test_every_indication_has_a_sane_target():
    for key, info in INDICATIONS.items():
        assert info["inr_min"] < info["inr_max"], key
        assert 1.0 <= info["inr_min"] <= 4.0, key


# --- dose helpers -----------------------------------------------------------
def test_weekly_total():
    assert weekly_total_mg(dict.fromkeys(range(7), 3.0)) == 21.0
    assert weekly_total_mg({0: 3.0, 1: 1.5, 2: None}) == 4.5


def test_suggestion_keeps_dose_when_in_range():
    result = suggest_weekly_adjustment(21.0, 2.5, 2.0, 3.0)
    assert result["suggested_range"] is None
    assert result["percent_range"] == (0, 0)


def test_suggestion_lowers_dose_when_inr_high():
    result = suggest_weekly_adjustment(21.0, 4.2, 2.0, 3.0)
    low, high = result["suggested_range"]
    assert low < high < 21.0


def test_suggestion_raises_dose_when_inr_low():
    result = suggest_weekly_adjustment(21.0, 1.6, 2.0, 3.0)
    low, high = result["suggested_range"]
    assert 21.0 < low < high


def test_suggestion_handles_unknown_current_dose():
    assert suggest_weekly_adjustment(0, 4.2, 2.0, 3.0)["suggested_range"] is None


def test_recheck_interval_shortens_when_out_of_range():
    assert next_inr_interval_days("critical_high") == 3
    assert next_inr_interval_days("high") == 7
    assert next_inr_interval_days("in_range", 28) == 28


def test_days_of_stock():
    # 21 mg/week at 3 mg tablets = 7 tablets/week, so 14 tablets ≈ 14 days.
    assert days_of_stock(14, 21.0, tablet_mg=3.0) == 14
    assert days_of_stock(0, 21.0) is None
    assert days_of_stock(14, 0) is None


# --- interaction screening --------------------------------------------------
def test_screen_finds_nsaid():
    hits = screen_medication_text("ผู้ป่วยกิน ibuprofen สำหรับปวดเข่า")
    assert any("NSAID" in hit["name"] for hit in hits)


def test_screen_finds_enzyme_inducer():
    hits = screen_medication_text("on rifampicin for TB treatment")
    assert any(hit["effect"] == "decrease" for hit in hits)


def test_screen_is_quiet_on_unrelated_text():
    assert screen_medication_text("ความดันโลหิตสูง เบาหวาน") == []
    assert screen_medication_text("") == []


# --- symptom triage ---------------------------------------------------------
def test_high_severity_is_urgent():
    result = triage_symptom({"severity": 5, "bleeding": 1})
    assert result["urgent"] is True
    assert "ทันที" in result["auto_response"]


def test_moderate_bleeding_is_urgent():
    assert triage_symptom({"severity": 3, "bleeding": 1})["urgent"] is True


def test_mild_symptom_is_not_urgent():
    result = triage_symptom({"severity": 1, "dizziness": 1})
    assert result["urgent"] is False
    assert result["labels"] == ["เวียนศีรษะ / หน้ามืด"]


def test_symptom_labels_include_free_text():
    labels = symptom_labels({"bruising": 1, "other": "ปวดท้อง"})
    assert "จ้ำเลือด / ช้ำง่าย" in labels
    assert "ปวดท้อง" in labels


def test_education_text_is_not_empty():
    text = education_text()
    assert "วาร์ฟาริน" in text
    assert len(text) > 200
