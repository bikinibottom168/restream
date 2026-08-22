"""HTML pages and the HTMX partials the dashboard polls."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.i18n import translate_provider_types
from app.core.logging import tail_file
from app.core.state import FILTER_GROUPS, parse_state
from app.database import crud
from app.database.db import run_db
from app.providers.factory import ProviderFactory
from app.providers.manager import PROVIDER_SECRET_KEYS
from app.streaming.mediamtx import download_asset as mediamtx_download_hint
from app.web.api import channel_list


def _autostart_status() -> dict[str, Any]:
    """Auto-start state for the settings page (never raises)."""
    try:
        from app.core import autostart

        return autostart.status()
    except Exception:  # noqa: BLE001 - the settings page must always render
        return {"supported": False, "installed": False, "error": ""}
from app.web.context import AppContext
from app.web.deps import base_context, get_ctx, require_auth, templates
from app.web.schemas import (
    serialize_channel,
    serialize_downtime,
    serialize_event,
    serialize_provider,
    summarise,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])


def _filter_channels(
    channels: list[dict[str, Any]], status: str, search: str
) -> list[dict[str, Any]]:
    """Apply the dashboard's status filter and search box."""
    result = channels
    group = FILTER_GROUPS.get(status)
    if group and status != "all":
        wanted = {state.value for state in group}
        result = [c for c in result if c["status"] in wanted]
    needle = (search or "").strip().lower()
    if needle:
        result = [
            c
            for c in result
            if needle in c["name"].lower()
            or needle in (c.get("group_title") or "").lower()
            or needle in (c.get("provider_ref") or "").lower()
        ]
    return result


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
async def page_dashboard(
    request: Request,
    status: str = Query(default="all"),
    q: str = Query(default=""),
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    channels = await channel_list(ctx)
    providers = await run_db(crud.list_providers)
    if not providers and not channels:
        return RedirectResponse(url="/setup", status_code=302)
    filtered = _filter_channels(channels, status, q)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        base_context(
            request,
            ctx,
            channels=filtered,
            summary=summarise(channels),
            system=ctx.streams.system_metrics(),
            status_filter=status,
            search=q,
            providers=providers,
            page="dashboard",
        ),
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def page_channel(
    request: Request, channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    channel = await run_db(crud.get_channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    provider = await run_db(crud.get_provider, channel.provider_id)
    snapshot = ctx.streams.snapshot(channel_id)
    payload = serialize_channel(
        channel,
        snapshot,
        reveal_url=ctx.store.get_bool("show_full_source_url"),
        default_rtmp=ctx.store.get_str("default_rtmp_server"),
        provider_name=provider.name if provider else "",
    )
    events = await run_db(crud.list_events, channel_id=channel_id, limit=30)
    history = await run_db(crud.list_downtime, channel_id=channel_id, limit=20)
    log_lines = tail_file(ctx.settings.ffmpeg_log_dir / f"{channel_id}.log", 120)
    providers = await run_db(crud.list_providers)
    return templates.TemplateResponse(
        request,
        "channel.html",
        base_context(
            request,
            ctx,
            channel=payload,
            raw=channel,
            snapshot=snapshot,
            viewer_urls=ctx.streams.viewer_urls_for(channel_id),
            events=[serialize_event(event) for event in events],
            history=[serialize_downtime(record) for record in history],
            log_lines=log_lines,
            providers=providers,
            page="channels",
        ),
    )


@router.get("/providers", response_class=HTMLResponse)
async def page_providers(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    rows = await run_db(crud.list_providers)
    channels = await run_db(crud.list_channels)
    providers = [
        serialize_provider(
            row,
            secrets_present={
                key: ctx.providers.has_secret(row.id, key) for key in PROVIDER_SECRET_KEYS
            },
            instance=ctx.providers.get(row.id),
        )
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "providers.html",
        base_context(
            request,
            ctx,
            providers=providers,
            provider_types=translate_provider_types(
                ProviderFactory.available(), ctx.store.get_str('ui_language')
            ),
            channels=channels,
            page="providers",
        ),
    )


@router.get("/providers/{provider_id}/debug", response_class=HTMLResponse)
async def page_provider_debug(
    request: Request, provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    row = await run_db(crud.get_provider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return templates.TemplateResponse(
        request,
        "provider_debug.html",
        base_context(request, ctx, provider=row, page="providers"),
    )


@router.get("/settings", response_class=HTMLResponse)
async def page_settings(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        base_context(
            request,
            ctx,
            values=ctx.store.as_dict(),
            telegram_token_set=ctx.has_telegram_token(),
            auth_enabled=ctx.auth_enabled,
            secret_backend=ctx.secrets.backend,
            ffmpeg=ctx.streams.ffmpeg_info,
            ffprobe=ctx.streams.ffprobe_info,
            capabilities=ctx.streams.ffmpeg_capabilities,
            buffer=ctx.streams.mediamtx_status(),
            mediamtx_download=mediamtx_download_hint(),
            autostart=_autostart_status(),
            page="settings",
        ),
    )


@router.get("/events", response_class=HTMLResponse)
async def page_events(
    request: Request,
    channel_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    events = await run_db(crud.list_events, channel_id=channel_id, limit=limit)
    channels = await run_db(crud.list_channels)
    return templates.TemplateResponse(
        request,
        "events.html",
        base_context(
            request,
            ctx,
            events=[serialize_event(event) for event in events],
            channels=channels,
            selected_channel=channel_id,
            page="events",
        ),
    )


@router.get("/history", response_class=HTMLResponse)
async def page_history(
    request: Request,
    channel_id: int | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    records = await run_db(crud.list_downtime, channel_id=channel_id, limit=300)
    summary = await run_db(crud.downtime_summary, days=7)
    channels = await run_db(crud.list_channels)
    return templates.TemplateResponse(
        request,
        "history.html",
        base_context(
            request,
            ctx,
            history=[serialize_downtime(record) for record in records],
            summary=summary,
            channels=channels,
            selected_channel=channel_id,
            page="history",
        ),
    )


@router.get("/logs", response_class=HTMLResponse)
async def page_logs(
    request: Request,
    source: str = Query(default="application"),
    channel_id: int | None = None,
    lines: int = Query(default=200, ge=10, le=2000),
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    if source == "channel" and channel_id is not None:
        path = ctx.settings.ffmpeg_log_dir / f"{channel_id}.log"
    else:
        source = "application"
        path = ctx.settings.log_dir / "app.log"
    channels = await run_db(crud.list_channels)
    return templates.TemplateResponse(
        request,
        "logs.html",
        base_context(
            request,
            ctx,
            log_lines=tail_file(path, lines),
            source=source,
            channels=channels,
            selected_channel=channel_id,
            lines=lines,
            page="logs",
        ),
    )


@router.get("/setup", response_class=HTMLResponse)
async def page_setup(request: Request, ctx: AppContext = Depends(get_ctx)) -> HTMLResponse:
    """First-run wizard: provider, RTMP default, Telegram, and the test buttons."""
    providers = await run_db(crud.list_providers)
    return templates.TemplateResponse(
        request,
        "setup.html",
        base_context(
            request,
            ctx,
            providers=providers,
            provider_types=translate_provider_types(
                ProviderFactory.available(), ctx.store.get_str('ui_language')
            ),
            values=ctx.store.as_dict(),
            telegram_token_set=ctx.has_telegram_token(),
            ffmpeg=ctx.streams.ffmpeg_info,
            ffprobe=ctx.streams.ffprobe_info,
            page="setup",
        ),
    )


# --------------------------------------------------------------------------- #
# HTMX partials (polled every few seconds - no full page reload)
# --------------------------------------------------------------------------- #
@router.get("/partials/channels", response_class=HTMLResponse)
async def partial_channels(
    request: Request,
    status: str = Query(default="all"),
    q: str = Query(default=""),
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    channels = await channel_list(ctx)
    return templates.TemplateResponse(
        request,
        "partials/channel_rows.html",
        {"request": request, "channels": _filter_channels(channels, status, q)},
    )


@router.get("/partials/summary", response_class=HTMLResponse)
async def partial_summary(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    channels = await channel_list(ctx)
    return templates.TemplateResponse(
        request,
        "partials/summary_cards.html",
        {"request": request, "summary": summarise(channels)},
    )


@router.get("/partials/system", response_class=HTMLResponse)
async def partial_system(
    request: Request, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/system_stats.html",
        {
            "request": request,
            "system": ctx.streams.system_metrics(),
            "ffmpeg_ok": ctx.streams.ffmpeg_info.available,
            "ffprobe_ok": ctx.streams.ffprobe_info.available,
        },
    )


@router.get("/partials/channel/{channel_id}", response_class=HTMLResponse)
async def partial_channel_detail(
    request: Request, channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> HTMLResponse:
    channel = await run_db(crud.get_channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    provider = await run_db(crud.get_provider, channel.provider_id)
    payload = serialize_channel(
        channel,
        ctx.streams.snapshot(channel_id),
        reveal_url=ctx.store.get_bool("show_full_source_url"),
        default_rtmp=ctx.store.get_str("default_rtmp_server"),
        provider_name=provider.name if provider else "",
    )
    return templates.TemplateResponse(
        request,
        "partials/channel_detail.html",
        {
            "request": request,
            "channel": payload,
            "show_full_source_url": ctx.store.get_bool("show_full_source_url"),
        },
    )


@router.get("/health", include_in_schema=True)
async def health(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Liveness endpoint (unauthenticated by design is *not* the case here -
    it sits behind the same optional basic auth as the rest of the dashboard)."""
    channels = await run_db(crud.list_channels)
    snapshots = ctx.streams.snapshots()
    online = sum(
        1
        for channel in channels
        if parse_state(
            (snapshots.get(channel.id) or {}).get("state") or channel.status
        ).is_running
    )
    payload = ctx.health()
    payload.update(
        {
            "channels_online": online,
            "channels_total": len(channels),
            "provider_session": any(
                provider.authenticated for provider in ctx.providers.all().values()
            ),
        }
    )
    return payload
