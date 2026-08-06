"""Warfarin clinical reference data and decision support.

Everything here is *decision support*: it summarises published guidance so the
pharmacist sees the relevant rule next to the patient's numbers. It never
changes a dose by itself — every suggestion is advisory and must be confirmed
by the responsible clinician.

Sources followed: ACCP Antithrombotic Therapy (CHEST) guidance on warfarin
management, and the Thai national warfarin clinic practice guidance used by
hospital anticoagulation clinics.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Indications and their usual INR targets
# ---------------------------------------------------------------------------
INDICATIONS: dict[str, dict] = {
    "af": {"label": "ภาวะหัวใจห้องบนสั่นพลิ้ว (AF)", "inr_min": 2.0, "inr_max": 3.0},
    "dvt_pe": {"label": "ลิ่มเลือดอุดตันหลอดเลือดดำ (DVT/PE)", "inr_min": 2.0, "inr_max": 3.0},
    "mech_mitral": {"label": "ลิ้นหัวใจเทียมโลหะ ตำแหน่งไมทรัล", "inr_min": 2.5, "inr_max": 3.5},
    "mech_aortic": {"label": "ลิ้นหัวใจเทียมโลหะ ตำแหน่งเอออร์ติก", "inr_min": 2.0, "inr_max": 3.0},
    "bioprosthetic": {"label": "ลิ้นหัวใจเทียมชีวภาพ", "inr_min": 2.0, "inr_max": 3.0},
    "rheumatic": {"label": "โรคลิ้นหัวใจรูมาติก", "inr_min": 2.0, "inr_max": 3.0},
    "stroke": {"label": "ป้องกันโรคหลอดเลือดสมองซ้ำ", "inr_min": 2.0, "inr_max": 3.0},
    "other": {"label": "อื่น ๆ", "inr_min": 2.0, "inr_max": 3.0},
}

SEX_LABELS = {"male": "ชาย", "female": "หญิง", "": "-"}

APPOINTMENT_TYPES = {
    "inr": "ตรวจ INR",
    "doctor": "พบแพทย์",
    "pharmacist": "พบเภสัชกร",
    "refill": "รับยาต่อเนื่อง",
    "other": "อื่น ๆ",
}

APPOINTMENT_STATUSES = {
    "scheduled": "นัดแล้ว",
    "attended": "มาตามนัด",
    "missed": "ไม่มาตามนัด",
    "cancelled": "ยกเลิก",
}

SYMPTOM_STATUS_LABELS = {
    "new": "ใหม่ (รอตอบ)",
    "replied": "ตอบแล้ว",
    "resolved": "ปิดเรื่องแล้ว",
}

DOSE_STATUS_LABELS = {
    "taken": "กินแล้ว",
    "late": "กินแล้ว (ช้า)",
    "missed": "ไม่ได้กิน",
    "planned": "รอกิน",
}


# ---------------------------------------------------------------------------
# INR interpretation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class INRAssessment:
    band: str             # critical_low | low | in_range | high | critical_high
    label: str
    color: str            # tailwind-ish token used by templates
    urgency: int          # 0 routine, 1 review soon, 2 same-day action
    advice: str
    suggested_change: str  # weekly-dose guidance, advisory only


def assess_inr(value: float | None, target_min: float, target_max: float) -> INRAssessment:
    """Classify an INR result against the patient's own target range."""
    if value is None:
        return INRAssessment(
            "unknown", "ไม่มีผล", "default", 0,
            "ยังไม่มีผลตรวจ INR", "-",
        )
    lo = target_min if target_min is not None else 2.0
    hi = target_max if target_max is not None else 3.0

    if value < 1.5:
        return INRAssessment(
            "critical_low", "ต่ำมาก", "danger", 2,
            "INR ต่ำมาก เสี่ยงเกิดลิ่มเลือด ควรปรึกษาแพทย์/เภสัชกรวันนี้ "
            "และตรวจซ้ำภายใน 1 สัปดาห์",
            "พิจารณาเพิ่มขนาดยารวมต่อสัปดาห์ 10–20% และนัดตรวจซ้ำเร็วขึ้น",
        )
    if value < lo:
        return INRAssessment(
            "low", "ต่ำกว่าเป้าหมาย", "warning", 1,
            "INR ต่ำกว่าเป้าหมาย ตรวจสอบความสม่ำเสมอการกินยา "
            "อาหารวิตามินเคปริมาณมาก และยาที่เพิ่งเริ่มใหม่",
            "พิจารณาเพิ่มขนาดยารวมต่อสัปดาห์ 5–15% และนัดตรวจซ้ำใน 1–2 สัปดาห์",
        )
    if value <= hi:
        return INRAssessment(
            "in_range", "อยู่ในเป้าหมาย", "success", 0,
            "INR อยู่ในช่วงเป้าหมาย ให้กินยาขนาดเดิมต่อเนื่อง",
            "คงขนาดยาเดิม นัดตรวจตามกำหนด",
        )
    if value <= hi + 1.0:
        return INRAssessment(
            "high", "สูงกว่าเป้าหมาย", "warning", 1,
            "INR สูงกว่าเป้าหมายเล็กน้อย เฝ้าระวังอาการเลือดออก "
            "ทบทวนยาและอาหารที่เพิ่งเปลี่ยน",
            "พิจารณาลดขนาดยารวมต่อสัปดาห์ 5–15% หรืองดยา 1 มื้อ แล้วตรวจซ้ำใน 1 สัปดาห์",
        )
    if value < 5.0:
        return INRAssessment(
            "high", "สูง", "danger", 1,
            "INR สูง ควรติดต่อผู้ป่วยเพื่อประเมินอาการเลือดออก "
            "และทบทวนขนาดยาโดยเภสัชกร/แพทย์",
            "พิจารณางดยา 1–2 มื้อ แล้วลดขนาดยารวมต่อสัปดาห์ 10–20% ตรวจซ้ำใน 3–7 วัน",
        )
    if value < 9.0:
        return INRAssessment(
            "critical_high", "สูงมาก", "danger", 2,
            "INR สูงมาก เสี่ยงเลือดออก ต้องติดต่อผู้ป่วยวันนี้ "
            "หากมีเลือดออกให้มาโรงพยาบาลทันที",
            "งดยา และปรึกษาแพทย์เรื่องวิตามินเค ตรวจ INR ซ้ำใน 24–48 ชั่วโมง",
        )
    return INRAssessment(
        "critical_high", "อันตราย", "danger", 2,
        "INR อยู่ในระดับอันตราย ต้องให้ผู้ป่วยมาโรงพยาบาลทันที",
        "งดยา และให้แพทย์พิจารณาให้วิตามินเคทันที",
    )


def inr_band_counts(values: list[tuple[float, float, float]]) -> dict[str, int]:
    """Count results by band for a list of (value, target_min, target_max)."""
    counts = {"low": 0, "in_range": 0, "high": 0}
    for value, lo, hi in values:
        assessment = assess_inr(value, lo, hi)
        if assessment.band in ("low", "critical_low"):
            counts["low"] += 1
        elif assessment.band == "in_range":
            counts["in_range"] += 1
        elif assessment.band in ("high", "critical_high"):
            counts["high"] += 1
    return counts


# ---------------------------------------------------------------------------
# Drug and food interactions
# ---------------------------------------------------------------------------
# effect: "increase" raises INR / bleeding risk, "decrease" lowers INR.
DRUG_INTERACTIONS: list[dict] = [
    {"name": "Aspirin / NSAIDs (ibuprofen, diclofenac, naproxen)", "effect": "increase",
     "severity": "high", "note": "เพิ่มความเสี่ยงเลือดออกในทางเดินอาหาร แม้ INR ไม่เปลี่ยน"},
    {"name": "ยาปฏิชีวนะกลุ่ม Cotrimoxazole (Bactrim)", "effect": "increase",
     "severity": "high", "note": "เพิ่ม INR ได้มากและเร็ว ควรตรวจ INR ซ้ำภายใน 3–5 วัน"},
    {"name": "Metronidazole", "effect": "increase", "severity": "high",
     "note": "ยับยั้งการกำจัดวาร์ฟาริน ทำให้ INR สูงขึ้น"},
    {"name": "Fluconazole / Ketoconazole / Itraconazole", "effect": "increase",
     "severity": "high", "note": "ยาฆ่าเชื้อรากลุ่ม azole เพิ่ม INR อย่างมีนัยสำคัญ"},
    {"name": "Amiodarone", "effect": "increase", "severity": "high",
     "note": "เพิ่ม INR ค่อยเป็นค่อยไปและอยู่นานหลายสัปดาห์ มักต้องลดขนาดวาร์ฟาริน"},
    {"name": "Ciprofloxacin / Levofloxacin / Erythromycin / Clarithromycin",
     "effect": "increase", "severity": "moderate", "note": "เพิ่ม INR ระหว่างใช้ยา"},
    {"name": "Omeprazole", "effect": "increase", "severity": "moderate",
     "note": "อาจเพิ่ม INR เล็กน้อย"},
    {"name": "Paracetamol ขนาดสูงต่อเนื่อง (>2 g/วัน หลายวัน)", "effect": "increase",
     "severity": "moderate", "note": "ใช้เป็นครั้งคราวปลอดภัย แต่ใช้ต่อเนื่องอาจเพิ่ม INR"},
    {"name": "Simvastatin / Fenofibrate", "effect": "increase", "severity": "moderate",
     "note": "อาจเพิ่ม INR ควรตรวจติดตามหลังเริ่มยา"},
    {"name": "Tramadol", "effect": "increase", "severity": "moderate",
     "note": "มีรายงานเพิ่ม INR"},
    {"name": "Rifampicin", "effect": "decrease", "severity": "high",
     "note": "ลด INR อย่างมาก มักต้องเพิ่มขนาดวาร์ฟาริน และปรับกลับเมื่อหยุดยา"},
    {"name": "Carbamazepine / Phenytoin / Phenobarbital", "effect": "decrease",
     "severity": "high", "note": "เร่งการกำจัดวาร์ฟาริน ทำให้ INR ลดลง"},
    {"name": "Griseofulvin", "effect": "decrease", "severity": "moderate",
     "note": "ลดผลของวาร์ฟาริน"},
    {"name": "วิตามินเคเสริม / อาหารเสริมสูตรรวม", "effect": "decrease",
     "severity": "moderate", "note": "ต้านฤทธิ์วาร์ฟารินโดยตรง"},
    {"name": "สมุนไพร: แปะก๊วย, กระเทียมสกัด, ขิงขนาดสูง, โสม, ตังกุย",
     "effect": "increase", "severity": "moderate",
     "note": "เพิ่มความเสี่ยงเลือดออก ควรแจ้งเภสัชกรก่อนใช้"},
    {"name": "สมุนไพร: St. John's Wort", "effect": "decrease", "severity": "high",
     "note": "ลดระดับวาร์ฟารินอย่างมาก"},
    {"name": "แอลกอฮอล์ (ดื่มหนักเป็นครั้งคราว)", "effect": "increase",
     "severity": "high", "note": "ดื่มหนักครั้งเดียวเพิ่ม INR; ดื่มประจำเรื้อรังลด INR"},
]

# Vitamin K content of foods common in Thai households (µg per ~100 g serving).
VITAMIN_K_FOODS: list[dict] = [
    {"name": "ผักคะน้า", "level": "สูงมาก", "amount": "~700 µg", "advice": "กินได้ แต่ให้ปริมาณสม่ำเสมอทุกสัปดาห์"},
    {"name": "ผักโขม", "level": "สูงมาก", "amount": "~480 µg", "advice": "กินได้ แต่อย่าเพิ่มปริมาณกะทันหัน"},
    {"name": "บรอกโคลี", "level": "สูง", "amount": "~140 µg", "advice": "กินในปริมาณเท่าเดิมสม่ำเสมอ"},
    {"name": "ใบบัวบก", "level": "สูง", "amount": "~120 µg", "advice": "ระวังน้ำใบบัวบกปริมาณมาก"},
    {"name": "ผักบุ้ง", "level": "ปานกลาง", "amount": "~90 µg", "advice": "กินได้ตามปกติ"},
    {"name": "กะหล่ำปลี", "level": "ปานกลาง", "amount": "~76 µg", "advice": "กินได้ตามปกติ"},
    {"name": "ถั่วลันเตา / ถั่วฝักยาว", "level": "ปานกลาง", "amount": "~40 µg", "advice": "กินได้ตามปกติ"},
    {"name": "แตงกวา / มะเขือเทศ / ฟักทอง", "level": "ต่ำ", "amount": "<20 µg", "advice": "กินได้อิสระ"},
    {"name": "ข้าว / เนื้อสัตว์ / ปลา / ไข่", "level": "ต่ำมาก", "amount": "<5 µg", "advice": "ไม่มีผลต่อ INR"},
    {"name": "ชาเขียวเข้มข้น (ใบชา)", "level": "สูง", "amount": "แปรผัน", "advice": "หลีกเลี่ยงการดื่มปริมาณมากผิดปกติ"},
]

BLEEDING_RED_FLAGS = [
    "เลือดออกไม่หยุดนานกว่า 10 นาที",
    "อาเจียนเป็นเลือด หรือมีสีคล้ายกากกาแฟ",
    "อุจจาระสีดำเหมือนยางมะตอย",
    "ปัสสาวะสีแดงหรือสีน้ำล้างเนื้อ",
    "ปวดศีรษะรุนแรงเฉียบพลัน แขนขาอ่อนแรง พูดไม่ชัด",
    "ช้ำขนาดใหญ่โดยไม่มีสาเหตุ หรือช้ำหลายตำแหน่ง",
    "ไอเป็นเลือด",
    "ประจำเดือนมามากผิดปกติ",
]


def screen_medication_text(text: str) -> list[dict]:
    """Flag known interacting drugs mentioned in free-text notes.

    Deliberately keyword-based and generous: this is a prompt for the
    pharmacist to look closer, not an authoritative interaction check.
    """
    if not text:
        return []
    haystack = text.lower()
    hits = []
    for entry in DRUG_INTERACTIONS:
        keywords = [
            part.strip().lower()
            for chunk in entry["name"].replace("/", ",").split(",")
            for part in chunk.split("(")
            if len(part.strip()) >= 4
        ]
        for keyword in keywords:
            cleaned = keyword.rstrip(")").strip()
            if cleaned and cleaned in haystack:
                hits.append(entry)
                break
    return hits


# ---------------------------------------------------------------------------
# Dose helpers
# ---------------------------------------------------------------------------
def weekly_total_mg(daily_doses: dict[int, float]) -> float:
    """Sum a Monday..Sunday dose map into a weekly total."""
    return round(sum(float(v or 0) for v in daily_doses.values()), 2)


def suggest_weekly_adjustment(
    current_weekly_mg: float, inr_value: float, target_min: float, target_max: float
) -> dict:
    """Advisory weekly-dose bracket for a given INR. Never applied automatically."""
    assessment = assess_inr(inr_value, target_min, target_max)
    percent_map = {
        "critical_low": (10, 20),
        "low": (5, 15),
        "in_range": (0, 0),
        "high": (-15, -5),
        "critical_high": (-20, -10),
    }
    low_pct, high_pct = percent_map.get(assessment.band, (0, 0))
    if not current_weekly_mg or low_pct == high_pct == 0:
        suggested = None
    else:
        suggested = (
            round(current_weekly_mg * (1 + low_pct / 100), 1),
            round(current_weekly_mg * (1 + high_pct / 100), 1),
        )
    return {
        "assessment": assessment,
        "current_weekly_mg": current_weekly_mg,
        "suggested_range": suggested,
        "percent_range": (low_pct, high_pct),
    }


def next_inr_interval_days(assessment_band: str, previous_days: int = 28) -> int:
    """How soon to recheck INR after a result — shorter when out of range."""
    if assessment_band in ("critical_low", "critical_high"):
        return 3
    if assessment_band in ("low", "high"):
        return 7
    return min(max(previous_days, 14), 42)


def days_of_stock(pill_inventory: int, weekly_mg: float, tablet_mg: float = 3.0) -> int | None:
    """Rough days of supply left, given tablets on hand and the weekly dose."""
    if not pill_inventory or not weekly_mg or weekly_mg <= 0:
        return None
    tablets_per_week = weekly_mg / max(tablet_mg, 0.5)
    if tablets_per_week <= 0:
        return None
    return int(pill_inventory / tablets_per_week * 7)


# ---------------------------------------------------------------------------
# Symptom triage
# ---------------------------------------------------------------------------
SYMPTOM_FIELDS = {
    "bleeding": {"label": "เลือดออกผิดปกติ", "severe": True},
    "bruising": {"label": "จ้ำเลือด / ช้ำง่าย", "severe": False},
    "headache": {"label": "ปวดศีรษะ", "severe": False},
    "dizziness": {"label": "เวียนศีรษะ / หน้ามืด", "severe": False},
    "nausea": {"label": "คลื่นไส้ อาเจียน", "severe": False},
}


def symptom_labels(report: dict) -> list[str]:
    labels = [
        info["label"] for key, info in SYMPTOM_FIELDS.items() if report.get(key)
    ]
    if report.get("other"):
        labels.append(str(report["other"])[:120])
    return labels


def triage_symptom(report: dict) -> dict:
    """Derive urgency and the automatic reply text for a symptom report."""
    severity = int(report.get("severity") or 1)
    has_bleeding = bool(report.get("bleeding"))
    urgent = severity >= 4 or has_bleeding and severity >= 3

    if urgent:
        response = (
            "⚠️ ระบบได้รับรายงานอาการของคุณแล้ว และแจ้งเภสัชกรทันที\n"
            "หากมีเลือดออกไม่หยุด อาเจียนเป็นเลือด อุจจาระสีดำ "
            "ปัสสาวะเป็นเลือด หรือปวดศีรษะรุนแรง "
            "กรุณาไปโรงพยาบาลที่ใกล้ที่สุดทันที ไม่ต้องรอการติดต่อกลับ"
        )
    elif severity >= 3 or has_bleeding:
        response = (
            "ระบบได้รับรายงานอาการของคุณแล้ว เภสัชกรจะติดต่อกลับภายในวันทำการถัดไป\n"
            "ระหว่างนี้ให้กินยาตามเดิม สังเกตอาการ และหากอาการแย่ลง "
            "กรุณาติดต่อโรงพยาบาลทันที"
        )
    else:
        response = (
            "ระบบบันทึกอาการของคุณเรียบร้อยแล้ว เภสัชกรจะทบทวนในการติดตามครั้งถัดไป\n"
            "หากอาการรุนแรงขึ้น กรุณารายงานซ้ำหรือติดต่อโรงพยาบาล"
        )
    return {
        "severity": severity,
        "urgent": urgent,
        "labels": symptom_labels(report),
        "auto_response": response,
    }


# ---------------------------------------------------------------------------
# Patient education
# ---------------------------------------------------------------------------
EDUCATION_TOPICS = [
    {
        "anchor": "basics",
        "title": "วาร์ฟารินคืออะไร",
        "icon": "💊",
        "color": "#4f46e5",
        "points": [
            "เป็นยาต้านการแข็งตัวของเลือด ช่วยป้องกันลิ่มเลือดอุดตัน",
            "ต้องกินตรงเวลาเดิมทุกวัน แนะนำช่วงเย็นหลังอาหาร",
            "ห้ามหยุดยา เพิ่มยา หรือลดยาเอง แม้รู้สึกสบายดี",
            "ต้องตรวจเลือด INR ตามนัดทุกครั้ง",
        ],
    },
    {
        "anchor": "vitk",
        "title": "อาหารและวิตามินเค",
        "icon": "🥬",
        "color": "#059669",
        "points": [
            "ผักใบเขียวเข้มมีวิตามินเคสูง ซึ่งลดฤทธิ์ยา",
            "ไม่ต้องงดเด็ดขาด แต่ให้กินปริมาณ 'สม่ำเสมอ' ทุกสัปดาห์",
            "หลีกเลี่ยงการกินผักใบเขียวปริมาณมากผิดปกติเป็นครั้งคราว",
            "งดแอลกอฮอล์ และแจ้งเภสัชกรก่อนใช้สมุนไพรหรืออาหารเสริม",
        ],
    },
    {
        "anchor": "danger",
        "title": "สัญญาณอันตราย ต้องพบแพทย์ทันที",
        "icon": "🚨",
        "color": "#dc2626",
        "points": BLEEDING_RED_FLAGS[:5],
    },
    {
        "anchor": "missed",
        "title": "ลืมกินยาทำอย่างไร",
        "icon": "⏰",
        "color": "#f59e0b",
        "points": [
            "นึกได้ภายใน 12 ชั่วโมง → กินทันทีในขนาดปกติ",
            "เกิน 12 ชั่วโมง → ข้ามมื้อนั้นไป แล้วกินมื้อถัดไปตามปกติ",
            "ห้ามกินยาสองมื้อรวมกันเพื่อชดเชยเด็ดขาด",
            "จดบันทึกไว้และแจ้งเภสัชกรในการตรวจครั้งถัดไป",
        ],
    },
    {
        "anchor": "drugs",
        "title": "ยาที่ต้องระวัง",
        "icon": "⚠️",
        "color": "#0891b2",
        "points": [
            "แจ้งทุกครั้งว่ากินวาร์ฟารินอยู่ เมื่อไปพบแพทย์หรือทันตแพทย์",
            "ห้ามซื้อยาแก้ปวดกลุ่ม NSAIDs (ibuprofen, diclofenac) กินเอง",
            "ยาปฏิชีวนะหลายชนิดทำให้ INR เปลี่ยน ต้องแจ้งเภสัชกร",
            "ปวดไข้ทั่วไปใช้พาราเซตามอลได้ แต่ไม่เกิน 2 กรัมต่อวัน",
        ],
    },
    {
        "anchor": "daily",
        "title": "การใช้ชีวิตประจำวัน",
        "icon": "🏠",
        "color": "#9333ea",
        "points": [
            "ใช้แปรงสีฟันขนนุ่ม และมีดโกนไฟฟ้าแทนใบมีด",
            "ระวังการหกล้ม โดยเฉพาะผู้สูงอายุ",
            "พกบัตรประจำตัวผู้ใช้ยาวาร์ฟารินติดตัวเสมอ",
            "แจ้งแพทย์ก่อนทำหัตถการหรือถอนฟันทุกครั้ง",
        ],
    },
]


def education_text() -> str:
    """Plain-text education summary — the LINE fallback when Flex is unavailable."""
    lines = ["📚 คู่มือผู้ป่วยวาร์ฟาริน", ""]
    for topic in EDUCATION_TOPICS[:4]:
        lines.append(f"{topic['icon']} {topic['title']}")
        lines.extend(f"• {point}" for point in topic["points"][:3])
        lines.append("")
    return "\n".join(lines).strip()
