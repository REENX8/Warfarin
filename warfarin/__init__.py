"""ระบบติดตามการกินยาวาร์ฟาริน — Warfarin Medication Tracker.

Application package for Sukhirin Padee Hospital, Narathiwat.
"""
from __future__ import annotations

__version__ = "2.0.0"

__all__ = ["__version__", "create_app"]


def create_app(*args, **kwargs):
    """Lazy re-export of the application factory (avoids import cycles)."""
    from warfarin.app_factory import create_app as _create_app

    return _create_app(*args, **kwargs)
