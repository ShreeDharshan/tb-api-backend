from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes.alarms import router as alarm_router
from src.api.routes.devices import router as devices_router
from src.api.routes.health import router as health_router
from src.api.routes.reports import router as reports_router
from src.api.routes.telemetry import router as telemetry_router
from src.config import get_settings, parse_tb_accounts
from src.core.logging import configure_logging
from src.tasks.alarm_scheduler import stop_scheduler
from src.tasks.task_runner import start_background_tasks


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    parse_tb_accounts()  # force-load and validate fallback behavior at startup

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.app_debug,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(devices_router)
    app.include_router(alarm_router)
    app.include_router(telemetry_router)
    app.include_router(reports_router)

    @app.on_event("startup")
    def on_startup() -> None:
        start_background_tasks()

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        stop_scheduler()

    return app


app = create_app()
