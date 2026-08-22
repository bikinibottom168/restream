"""Shared FastAPI dependencies and the Jinja2 environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.core.i18n import DEFAULT_LANGUAGE, LANGUAGES, make_translator, normalise
from app.core.security import mask_secret, mask_url_token, shorten_url
from app.core.state import ChannelState, parse_state
from app.core.timeutil import format_local, humanize_duration, isoformat
from app.web.context import AppContext

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["duration"] = humanize_duration
templates.env.filters["localtime"] = format_local
templates.env.filters["mask_url"] = mask_url_token
templates.env.filters["short_url"] = shorten_url
templates.env.filters["mask"] = mask_secret
templates.env.filters["isoformat"] = isoformat
templates.env.globals["ChannelState"] = ChannelState
templates.env.globals["parse_state"] = parse_state
templates.env.globals["languages"] = LANGUAGES


def set_template_language(code: str) -> str:
    """Install the translator used by every template.

    The language is one application-wide setting, so it lives in the Jinja
    environment rather than being threaded through each render call. Called at
    startup and again whenever the operator changes it.
    """
    language = normalise(code)
    templates.env.globals["t"] = make_translator(language)
    templates.env.globals["current_language"] = language
    return language


# Sensible default until the stored setting is loaded during startup.
set_template_language(DEFAULT_LANGUAGE)

_basic = HTTPBasic(auto_error=False)


def get_ctx(request: Request) -> AppContext:
    """Return the application context stored on the FastAPI app."""
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - only if startup failed catastrophically
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application is not ready",
        )
    return ctx


async def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """Optional HTTP Basic auth, enabled by setting ADMIN_USERNAME/PASSWORD."""
    ctx = get_ctx(request)
    if not ctx.auth_enabled:
        return
    if credentials is None or not ctx.check_credentials(
        credentials.username, credentials.password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Restream Manager"'},
        )


def base_context(request: Request, ctx: AppContext, **extra: Any) -> dict[str, Any]:
    """Template variables every page needs."""
    context: dict[str, Any] = {
        "request": request,
        "app_name": ctx.settings.app_name,
        "app_version": ctx.settings.app_version,
        "ffmpeg_ok": ctx.streams.ffmpeg_info.available,
        "ffprobe_ok": ctx.streams.ffprobe_info.available,
        "telegram_ok": ctx.notifier.configured,
        "startup_errors": ctx.startup_errors,
        "show_full_source_url": ctx.store.get_bool("show_full_source_url"),
    }
    context.update(extra)
    return context
