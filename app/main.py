"""Application entry point.

Startup order (as specified):

1. check FFmpeg / ffprobe
2. initialise the database
3. load providers (authentication is lazy and never fatal)
4. load channels
5. auto-start the channels that should run
6. start the watchdog
7. serve the dashboard

A provider that cannot authenticate does **not** stop the dashboard from
opening - the failure is shown in the header instead.

Run it with::

    python -m app.main
    uvicorn app.main:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.console import disable_quick_edit
from app.version import APP_TITLE, APP_VERSION
from app.web import api, routes, websocket
from app.web.context import AppContext

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the context on startup, tear it down on shutdown."""
    ctx = AppContext()
    app.state.ctx = ctx
    try:
        await ctx.startup()
    except Exception:  # noqa: BLE001 - the dashboard must still come up
        logger.exception("startup failed - the dashboard will report the problem")
    try:
        yield
    finally:
        await ctx.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=(
            "Relay authorised media inputs to your own RTMP infrastructure, "
            "with health monitoring and automatic recovery."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    application.include_router(routes.router)
    application.include_router(api.router)
    application.include_router(websocket.router)

    @application.exception_handler(Exception)
    async def unhandled(request, exc: Exception) -> JSONResponse:  # type: ignore[no-untyped-def]
        """Log everything; never leak a stack trace to the browser."""
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error - see logs/app.log for details"},
        )

    logger.debug("application created (host=%s port=%s)", settings.app_host, settings.app_port)
    return application


app = create_app()


def _configure_event_loop() -> None:
    """Windows needs the Proactor loop for asyncio subprocesses."""
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def main() -> None:
    """Console entry point: ``python -m app.main``."""
    import uvicorn

    _configure_event_loop()
    # Before anything writes to the console: a stray click in a Windows console
    # window pauses stdout, and a paused stdout freezes the event loop with it.
    disable_quick_edit()
    settings = get_settings()
    settings.ensure_dirs()

    print(f"{APP_TITLE} v{APP_VERSION}")
    print(f"Dashboard: http://{settings.app_host}:{settings.app_port}")
    if settings.app_host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: the dashboard is bound to a non-loopback address. "
            "Set ADMIN_USERNAME/ADMIN_PASSWORD before exposing it."
        )

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
