"""Application factory: wiring, startup checks and error handling."""
from __future__ import annotations

import logging
import logging.config
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from warfarin import __version__, scheduler, staff
from warfarin.config import get_settings, reset_settings_cache
from warfarin.db import db, healthcheck
from warfarin.deps import RedirectException
from warfarin.middleware import (
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from warfarin.migrations import LATEST_VERSION, run_migrations, schema_version
from warfarin.patients import backfill_access_tokens
from warfarin.templating import ensure_static_dir, render, reset_templates

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
            },
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            "warfarin": {"level": level, "propagate": True},
            "uvicorn.access": {"level": "WARNING", "propagate": True},
            "apscheduler": {"level": "WARNING", "propagate": True},
        },
    })


def initialise_database() -> None:
    """Run migrations and one-time data fixes before serving traffic."""
    applied = run_migrations()
    if applied:
        logger.info("Applied migrations: %s", ", ".join(applied))
    with db() as conn:
        filled = backfill_access_tokens(conn)
    if filled:
        logger.info("Generated portal access tokens for %d existing patients", filled)

    credentials = staff.bootstrap_admin()
    if credentials:
        if credentials["generated"]:
            logger.warning(
                "Created the first admin account '%s' with a generated password: %s\n"
                "Log in and change it immediately — it is shown only once.",
                credentials["username"], credentials["password"],
            )
        else:
            logger.info(
                "Created the first admin account '%s' from BOOTSTRAP_ADMIN_PASSWORD",
                credentials["username"],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialise_database()
    scheduler.start()
    settings = get_settings()
    if settings.line_push_enabled:
        try:
            from warfarin.rich_menu import create_rich_menu

            ok, detail = create_rich_menu()
            logger.info("LINE rich menu: %s (%s)", "ready" if ok else "skipped", detail)
        except Exception:
            logger.warning("Rich menu setup skipped", exc_info=True)
    logger.info(
        "Warfarin tracker %s started (env=%s, schema=%d/%d)",
        __version__, settings.env, schema_version(), LATEST_VERSION,
    )
    try:
        yield
    finally:
        scheduler.shutdown()


def create_app(settings_override: bool = False) -> FastAPI:
    """Build the ASGI application."""
    if settings_override:
        reset_settings_cache()
        reset_templates()
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="ระบบติดตามการกินยาวาร์ฟาริน",
        description="Warfarin Medication Tracker — " + settings.hospital_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    # Middleware runs bottom-up: session first, then headers, then logging.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SessionMiddleware)

    if settings.allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            allow_credentials=True,
        )

    app.mount("/static", StaticFiles(directory=ensure_static_dir()), name="static")

    from warfarin.routers import (
        admin,
        api,
        auth,
        dashboard,
        line_bp,
        patients,
        public,
        qr,
        reports,
        research,
        symptoms,
    )

    for router in (
        auth.router,
        dashboard.router,
        patients.router,
        reports.router,
        research.router,
        symptoms.router,
        admin.router,
        api.router,
        qr.router,
        public.router,
        line_bp.router,
        line_bp.staff_router,
    ):
        app.include_router(router)

    _register_error_handlers(app)
    _register_health(app)
    return app


def _register_health(app: FastAPI) -> None:
    @app.get("/ping", include_in_schema=False)
    def ping():
        ok, detail = healthcheck()
        if not ok:
            return PlainTextResponse(f"db unavailable: {detail}", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        ok, detail = healthcheck()
        settings = get_settings()
        body = {
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "schema_version": schema_version(),
            "schema_latest": LATEST_VERSION,
            "line_push": settings.line_push_enabled,
            "line_webhook": settings.line_webhook_enabled,
            "scheduler": settings.enable_scheduler,
        }
        if not ok:
            body["detail"] = detail
        return JSONResponse(body, status_code=200 if ok else 503)


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RedirectException)
    async def redirect_handler(request: Request, exc: RedirectException):
        return RedirectResponse(exc.location, status_code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code in (301, 302, 303, 307, 308) and "location" in {
            k.lower() for k in (exc.headers or {})
        }:
            return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(
            request, "error.html",
            {"status_code": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        return render(
            request, "error.html",
            {"status_code": 422, "detail": "ข้อมูลที่ส่งมาไม่ถูกต้อง กรุณาตรวจสอบแบบฟอร์ม"},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled error on %s %s", request.method, request.url.path
        )
        if _wants_json(request):
            return JSONResponse({"detail": "internal server error"}, status_code=500)
        try:
            return render(
                request, "error.html",
                {
                    "status_code": 500,
                    "detail": "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่อีกครั้ง "
                              "หากยังพบปัญหา กรุณาแจ้งผู้ดูแลระบบ",
                },
                status_code=500,
            )
        except Exception:
            return PlainTextResponse("internal server error", status_code=500)


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith(("/api/", "/webhook")):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept
