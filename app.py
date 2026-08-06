"""ASGI entrypoint — ระบบติดตามการกินยาวาร์ฟาริน (Warfarin Medication Tracker).

Run with:  uvicorn app:app --host 0.0.0.0 --port 8000

The application itself lives in the `warfarin` package; this module only
constructs it so `app:app` keeps working for existing deployments.
"""
from __future__ import annotations

from warfarin.app_factory import create_app

app = create_app()

__all__ = ["app"]
