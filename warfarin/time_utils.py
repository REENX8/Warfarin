"""Timezone helpers — the whole system operates in Asia/Bangkok (UTC+7).

Datetimes are stored in SQLite as naive ISO-8601 strings already converted to
Thai local time, so every read/write path must go through these helpers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")

DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%H:%M"


def now_dt() -> datetime:
    """Current local (Bangkok) time as a naive datetime."""
    return datetime.now(TZ).replace(tzinfo=None)


def now() -> str:
    """Current local time as an ISO-8601 string (what we store in SQLite)."""
    return now_dt().isoformat(timespec="seconds")


def today() -> str:
    """Current local date as YYYY-MM-DD."""
    return now_dt().strftime(DATE_FMT)


def today_date() -> date:
    return now_dt().date()


def days_ago(days: int) -> str:
    """Date string `days` days before today."""
    return (now_dt() - timedelta(days=days)).strftime(DATE_FMT)


def parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD, returning None for empty/invalid input."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), DATE_FMT).date()
    except (ValueError, AttributeError):
        return None


def parse_dt(value: str | None) -> datetime | None:
    """Parse a stored ISO datetime string, tolerating legacy microsecond forms."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def parse_time(value: str | None, default: str = "18:00") -> str:
    """Validate an HH:MM time string, falling back to `default`."""
    try:
        return datetime.strptime((value or "").strip(), TIME_FMT).strftime(TIME_FMT)
    except (ValueError, AttributeError):
        return default


THAI_MONTHS = {
    1: "มกราคม", 2: "กุมภาพันธ์", 3: "มีนาคม", 4: "เมษายน",
    5: "พฤษภาคม", 6: "มิถุนายน", 7: "กรกฎาคม", 8: "สิงหาคม",
    9: "กันยายน", 10: "ตุลาคม", 11: "พฤศจิกายน", 12: "ธันวาคม",
}

THAI_MONTHS_SHORT = {
    1: "ม.ค.", 2: "ก.พ.", 3: "มี.ค.", 4: "เม.ย.", 5: "พ.ค.", 6: "มิ.ย.",
    7: "ก.ค.", 8: "ส.ค.", 9: "ก.ย.", 10: "ต.ค.", 11: "พ.ย.", 12: "ธ.ค.",
}


def thai_date(value: str | date | datetime | None, short: bool = True) -> str:
    """Format a date as Thai Buddhist-era text, e.g. '6 ส.ค. 2569'."""
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = parse_date(value[:10])
        if parsed is None:
            return value
        value = parsed
    if isinstance(value, datetime):
        value = value.date()
    months = THAI_MONTHS_SHORT if short else THAI_MONTHS
    return f"{value.day} {months[value.month]} {value.year + 543}"


def safe_year_month(year, month, ref: date | None = None) -> tuple[int, int]:
    """Validate calendar query params; fall back to the current year/month."""
    ref = ref or today_date()
    try:
        year_i = int(year)
        month_i = int(month)
    except (TypeError, ValueError):
        return ref.year, ref.month
    if not (1 <= month_i <= 12) or not (2000 <= year_i <= ref.year + 1):
        return ref.year, ref.month
    return year_i, month_i
