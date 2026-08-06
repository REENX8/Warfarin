"""Application configuration, resolved once from the environment.

Production deployments must supply SECRET_KEY; anything else is optional and
degrades gracefully (LINE features simply stay disabled without credentials).
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

DEV_SECRET = "dev-only-insecure-secret-key"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    env: str = "production"
    secret_key: str = ""
    db_path: str = "./medtrack.db"
    base_url: str = "http://localhost:8000"
    hospital_name: str = "โรงพยาบาลสุไหงปาดี"
    hospital_phone: str = ""

    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    line_staff_register_code: str = ""

    session_hours: int = 8
    session_idle_minutes: int = 0
    cookie_secure: bool = True
    csrf_enabled: bool = True

    max_login_attempts: int = 5
    login_lockout_seconds: int = 900

    enable_scheduler: bool = True
    reminder_first_hour: int = 18
    reminder_first_minute: int = 0
    reminder_second_hour: int = 19
    reminder_second_minute: int = 30
    mark_missed_hour: int = 21
    mark_missed_minute: int = 0

    low_stock_threshold: int = 7
    inr_due_reminder_days: int = 3

    bootstrap_admin_user: str = "admin"
    bootstrap_admin_password: str = ""

    allowed_origins: tuple[str, ...] = field(default_factory=tuple)

    # --- derived -----------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_testing(self) -> bool:
        return self.env == "testing"

    @property
    def line_push_enabled(self) -> bool:
        return bool(self.line_channel_access_token)

    @property
    def line_webhook_enabled(self) -> bool:
        return bool(self.line_channel_secret)


def load_settings() -> Settings:
    """Build a Settings object from environment variables.

    Raises RuntimeError when running in production without a SECRET_KEY —
    silently generating one would invalidate every session on each restart
    and quietly weaken the CSRF and session-token signing.
    """
    env = os.getenv("APP_ENV", "production").strip().lower()
    if env not in ("production", "development", "testing"):
        env = "production"

    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not secret_key:
        if env == "production":
            raise RuntimeError(
                "SECRET_KEY environment variable is required in production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        secret_key = DEV_SECRET if env == "development" else secrets.token_urlsafe(32)
        logger.warning("Using a non-production SECRET_KEY (APP_ENV=%s)", env)

    base_url = os.getenv("BASE_URL", "http://localhost:8000").strip().rstrip("/")

    origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    origins = tuple(o.strip() for o in origins_raw.split(",") if o.strip())

    return Settings(
        env=env,
        secret_key=secret_key,
        db_path=os.getenv("DB_PATH", "./medtrack.db").strip(),
        base_url=base_url,
        hospital_name=os.getenv("HOSPITAL_NAME", "โรงพยาบาลสุไหงปาดี").strip(),
        hospital_phone=os.getenv("HOSPITAL_PHONE", "").strip(),
        line_channel_secret=os.getenv("LINE_CHANNEL_SECRET", "").strip(),
        line_channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
        line_staff_register_code=os.getenv("LINE_STAFF_REGISTER_CODE", "").strip(),
        session_hours=_env_int("SESSION_HOURS", 8),
        session_idle_minutes=_env_int("SESSION_IDLE_MINUTES", 0),
        cookie_secure=_env_bool("COOKIE_SECURE", base_url.startswith("https://")),
        csrf_enabled=_env_bool("CSRF_ENABLED", env != "testing"),
        max_login_attempts=_env_int("MAX_LOGIN_ATTEMPTS", 5),
        login_lockout_seconds=_env_int("LOGIN_LOCKOUT_SECONDS", 900),
        enable_scheduler=_env_bool("ENABLE_SCHEDULER", env != "testing"),
        reminder_first_hour=_env_int("REMINDER_FIRST_HOUR", 18),
        reminder_first_minute=_env_int("REMINDER_FIRST_MINUTE", 0),
        reminder_second_hour=_env_int("REMINDER_SECOND_HOUR", 19),
        reminder_second_minute=_env_int("REMINDER_SECOND_MINUTE", 30),
        mark_missed_hour=_env_int("MARK_MISSED_HOUR", 21),
        mark_missed_minute=_env_int("MARK_MISSED_MINUTE", 0),
        low_stock_threshold=_env_int("LOW_STOCK_THRESHOLD", 7),
        inr_due_reminder_days=_env_int("INR_DUE_REMINDER_DAYS", 3),
        bootstrap_admin_user=os.getenv("BOOTSTRAP_ADMIN_USER", "admin").strip() or "admin",
        bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip(),
        allowed_origins=origins,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used throughout the app."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings — used by tests that mutate the environment."""
    get_settings.cache_clear()
