"""Jinja2 environment: shared globals, filters and the render helper."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from warfarin import __version__
from warfarin.clinical import (
    APPOINTMENT_STATUSES,
    APPOINTMENT_TYPES,
    DOSE_STATUS_LABELS,
    INDICATIONS,
    SEX_LABELS,
    SYMPTOM_STATUS_LABELS,
)
from warfarin.config import get_settings
from warfarin.security import ROLES
from warfarin.time_utils import (
    THAI_MONTHS,
    THAI_MONTHS_SHORT,
    now_dt,
    thai_date,
    today,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_templates: Jinja2Templates | None = None

STATUS_BADGES = {
    "taken": "badge-success",
    "late": "badge-warning",
    "missed": "badge-danger",
    "planned": "badge-default",
    "new": "badge-danger",
    "replied": "badge-warning",
    "resolved": "badge-success",
    "scheduled": "badge-primary",
    "attended": "badge-success",
    "cancelled": "badge-default",
}


def _percent_class(value) -> str:
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return "text-slate-500"
    if percent >= 90:
        return "text-emerald-600"
    if percent >= 70:
        return "text-amber-600"
    return "text-red-600"


def _format_datetime(value) -> str:
    if not value:
        return ""
    text = str(value)
    return text.replace("T", " ")[:16]


def _short_time(value) -> str:
    if not value:
        return ""
    return str(value).replace("T", " ")[11:16]


def _open_symptom_count() -> int:
    """Sidebar badge count — never let a DB hiccup break a page render."""
    try:
        from warfarin.symptoms import open_count

        return open_count()
    except Exception:  # pragma: no cover - defensive
        return 0


def get_templates() -> Jinja2Templates:
    """Build (once) the Jinja environment used by every route."""
    global _templates
    if _templates is not None:
        return _templates

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = templates.env
    settings = get_settings()

    # Autoescape is on by default for .html; keep templates strict about
    # undefined names in development so typos surface during testing.
    env.trim_blocks = True
    env.lstrip_blocks = True
    if not settings.is_production:
        env.cache = {}

    env.globals.update(
        app_version=__version__,
        hospital_name=settings.hospital_name,
        hospital_phone=settings.hospital_phone,
        base_url=settings.base_url,
        roles=ROLES,
        indications=INDICATIONS,
        sex_labels=SEX_LABELS,
        appointment_types=APPOINTMENT_TYPES,
        appointment_statuses=APPOINTMENT_STATUSES,
        symptom_status_labels=SYMPTOM_STATUS_LABELS,
        dose_status_labels=DOSE_STATUS_LABELS,
        status_badges=STATUS_BADGES,
        thai_months=THAI_MONTHS,
        thai_months_short=THAI_MONTHS_SHORT,
        today=today,
        now=now_dt,
    )
    env.filters.update(
        thai_date=thai_date,
        datetime_str=_format_datetime,
        time_str=_short_time,
        percent_class=_percent_class,
    )
    _templates = templates
    return templates


def reset_templates() -> None:
    """Drop the cached environment (tests changing settings)."""
    global _templates
    _templates = None


def render(
    request: Request,
    template: str,
    context: dict | None = None,
    status_code: int = 200,
    **kwargs,
):
    """Render a template with the session/user/CSRF context always present."""
    from warfarin.deps import expected_csrf_token

    data = dict(context or {})
    data.update(kwargs)
    data.setdefault("user", getattr(request.state, "user", None))
    data.setdefault("csrf_token", expected_csrf_token(request))
    data.setdefault("current_path", request.url.path)
    data.setdefault("flash", request.query_params.get("msg", ""))
    data.setdefault("flash_kind", request.query_params.get("kind", "success"))
    if data.get("user") is not None:
        data.setdefault("open_symptom_count", _open_symptom_count())
    return get_templates().TemplateResponse(
        request, template, data, status_code=status_code
    )


def ensure_static_dir() -> str:
    """Create the static directory if the deployment does not ship one."""
    static_dir = TEMPLATE_DIR.parent / "static"
    os.makedirs(static_dir, exist_ok=True)
    return str(static_dir)
