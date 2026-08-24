"""JSON API.

Every dashboard button calls one of these endpoints; nothing in the UI talks to
the streaming layer directly.  Responses never contain a credential, and source
URLs are masked unless ``SHOW_FULL_SOURCE_URL`` is enabled.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from app.core.i18n import explain_stream_error, translate_provider_types
from app.core.logging import tail_file
from app.core.security import mask_url_token
from app.core.settings_store import EDITABLE_KEYS, SettingsValidationError
from app.core.state import ChannelState
from app.database import crud
from app.database.db import run_db
from app.database.models import EventType
from app.providers.base import (
    ChannelInfo,
    DiscoveryNotSupported,
    ProviderAuthError,
    ProviderError,
)
from app.providers.factory import ProviderFactory
from app.providers.iptv import IptvProvider
from app.providers.manager import PROVIDER_SECRET_KEYS
from app.streaming.failover import parse_fallback_sources
from app.web.bulk import deduplicate, parse_channel_list
from app.web.context import AppContext
from app.web.deps import get_ctx, require_auth
from app.web.schemas import (
    BulkActionPayload,
    ChannelListPayload,
    ChannelPayload,
    ImportPayload,
    IptvPayload,
    IptvPreviewPayload,
    ProviderPayload,
    SettingsPayload,
    serialize_channel,
    serialize_downtime,
    serialize_event,
    serialize_provider,
    summarise,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_auth)])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def channel_list(ctx: AppContext, *, reveal: bool | None = None) -> list[dict[str, Any]]:
    """Every channel, merged with live supervisor state."""
    channels = await run_db(crud.list_channels)
    providers = {p.id: p.name for p in await run_db(crud.list_providers)}
    snapshots = ctx.streams.snapshots()
    default_rtmp = ctx.store.get_str("default_rtmp_server")
    show_full = ctx.store.get_bool("show_full_source_url") if reveal is None else reveal
    buffered = ctx.streams.buffer_enabled
    # 24h health: outage count + total downtime per channel, for uptime %.
    health = {
        item["channel_id"]: item
        for item in await run_db(crud.downtime_summary, days=1)
    }
    window = 24 * 3600
    rows: list[dict[str, Any]] = []
    for channel in channels:
        row = serialize_channel(
            channel,
            snapshots.get(channel.id),
            reveal_url=show_full,
            default_rtmp=default_rtmp,
            provider_name=providers.get(channel.provider_id or -1, ""),
        )
        row["buffered"] = buffered
        row["viewer_urls"] = ctx.streams.viewer_urls_for(channel.id) if buffered else {}
        stat = health.get(channel.id)
        down = min(window, int(stat["total_seconds"])) if stat else 0
        row["outages_24h"] = stat["outages"] if stat else 0
        row["uptime_24h"] = round((window - down) / window * 100, 1)
        row["last_error_explained"] = explain_stream_error(row.get("last_error") or "")
        rows.append(row)
    return rows


async def _require_channel(channel_id: int) -> Any:
    channel = await run_db(crud.get_channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


async def _require_provider(provider_id: int) -> Any:
    provider = await run_db(crud.get_provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return provider


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@router.get("/status")
async def api_status(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    channels = await channel_list(ctx)
    return {
        "summary": summarise(channels),
        "system": ctx.streams.system_metrics(),
        "ffmpeg": ctx.streams.ffmpeg_info.as_dict(),
        "ffprobe": ctx.streams.ffprobe_info.as_dict(),
        "ffmpeg_capabilities": ctx.streams.ffmpeg_capabilities,
        "buffer": ctx.streams.mediamtx_status(),
        "telegram": ctx.notifier.describe(),
        "providers": [
            {"id": pid, **provider.describe()}
            for pid, provider in ctx.providers.all().items()
        ],
        "startup_errors": ctx.startup_errors,
    }


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #
@router.get("/channels")
async def api_channels(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    channels = await channel_list(ctx)
    return {"channels": channels, "summary": summarise(channels)}


@router.get("/channels/{channel_id}")
async def api_channel(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    channel = await _require_channel(channel_id)
    provider = await run_db(crud.get_provider, channel.provider_id)
    return serialize_channel(
        channel,
        ctx.streams.snapshot(channel_id),
        reveal_url=ctx.store.get_bool("show_full_source_url"),
        default_rtmp=ctx.store.get_str("default_rtmp_server"),
        provider_name=provider.name if provider else "",
    )


@router.post("/channels", status_code=201)
async def api_create_channel(
    payload: ChannelPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    data = payload.model_dump()
    channel = await run_db(crud.create_channel, **data)
    await run_db(
        crud.add_event,
        event_type=EventType.CHANNEL_ADDED,
        message="created from dashboard",
        channel_id=channel.id,
        channel_name=channel.name,
    )
    if not channel.resolved_rtmp(ctx.store.get_str("default_rtmp_server")):
        await run_db(crud.set_channel_status, channel.id, ChannelState.CONFIG_REQUIRED)
    return {"ok": True, "id": channel.id}


@router.put("/channels/{channel_id}")
async def api_update_channel(
    channel_id: int, payload: ChannelPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    await _require_channel(channel_id)
    channel = await run_db(crud.update_channel, channel_id, **payload.model_dump())
    await run_db(
        crud.add_event,
        event_type=EventType.CHANNEL_UPDATED,
        message="edited from dashboard",
        channel_id=channel_id,
        channel_name=channel.name if channel else "",
    )
    supervisor = ctx.streams.peek(channel_id)
    if supervisor is not None and supervisor.is_running:
        await ctx.streams.restart_channel(channel_id)
    return {"ok": True}


@router.delete("/channels/{channel_id}")
async def api_delete_channel(
    channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    await _require_channel(channel_id)
    await ctx.streams.stop_channel(channel_id, "channel deleted")
    ctx.streams.forget(channel_id)
    ctx.notifier.forget_channel(channel_id)
    deleted = await run_db(crud.delete_channel, channel_id)
    return {"ok": deleted}


@router.post("/channels/{channel_id}/start")
async def api_start(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    return await ctx.streams.start_channel(channel_id)


@router.post("/channels/{channel_id}/stop")
async def api_stop(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    ctx.notifier.forget_channel(channel_id)
    return await ctx.streams.stop_channel(channel_id)


@router.post("/channels/{channel_id}/restart")
async def api_restart(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    return await ctx.streams.restart_channel(channel_id)


@router.post("/channels/{channel_id}/refresh")
async def api_refresh(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    return await ctx.streams.refresh_channel(channel_id)


@router.post("/channels/{channel_id}/test")
async def api_test_source(
    channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Resolve + ffprobe without starting FFmpeg or touching the RTMP output."""
    await _require_channel(channel_id)
    outcome = await ctx.streams.test_source(channel_id)
    return outcome.as_dict(reveal=ctx.store.get_bool("show_full_source_url"))


@router.post("/channels/{channel_id}/use-primary")
async def api_use_primary(
    channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Put the channel back on its primary source now, on the operator's word.

    Deliberately manual: automatic failback is off by default because the
    switch itself costs a glitch, so the operator picks the moment.
    """
    await _require_channel(channel_id)
    return await ctx.streams.switch_source(channel_id, 0)


@router.post("/channels/{channel_id}/use-backup")
async def api_use_backup(
    channel_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Move the channel to the next backup source now."""
    channel = await _require_channel(channel_id)
    sources = parse_fallback_sources(getattr(channel, "fallback_urls", ""))
    if not sources:
        return {"ok": False, "error": "this channel has no backup source configured"}
    supervisor = ctx.streams.peek(channel_id)
    current = supervisor.snapshot().get("active_source_index", 0) if supervisor else 0
    target = current + 1 if current + 1 <= len(sources) else 1
    return await ctx.streams.switch_source(channel_id, target)


@router.post("/channels/{channel_id}/enable")
async def api_enable(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    return await ctx.streams.set_enabled(channel_id, True)


@router.post("/channels/{channel_id}/disable")
async def api_disable(channel_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    await _require_channel(channel_id)
    ctx.notifier.forget_channel(channel_id)
    return await ctx.streams.set_enabled(channel_id, False)


@router.post("/channels/bulk")
async def api_bulk(
    payload: BulkActionPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    action = payload.action
    ids = payload.channel_ids
    if action == "start" and not ids:
        return await ctx.streams.start_all()
    if action == "stop" and not ids:
        return await ctx.streams.stop_all()
    if not ids:
        raise HTTPException(status_code=400, detail="no channels selected")

    if action == "restart":
        return await ctx.streams.restart_many(ids)
    if action == "refresh":
        return await ctx.streams.refresh_many(ids)

    results = []
    for channel_id in ids:
        if action == "start":
            results.append(await ctx.streams.start_channel(channel_id))
        elif action == "stop":
            results.append(await ctx.streams.stop_channel(channel_id))
        elif action == "enable":
            results.append(await ctx.streams.set_enabled(channel_id, True))
        elif action == "disable":
            results.append(await ctx.streams.set_enabled(channel_id, False))
    return {"ok": True, "count": len(results)}


@router.post("/channels/import-list")
async def api_import_channel_list(
    payload: ChannelListPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Create many channels at once from pasted lines or a JSON document.

    One channel per URL. A URL that points at media is stored as the channel's
    direct input; anything else is stored as the channel's endpoint and fetched
    at resolve time.
    """
    result = parse_channel_list(payload.text)
    if not result.entries:
        return {"ok": False, "created": [], "skipped": 0, "errors": result.errors or ["nothing to import"]}

    entries, duplicates_in_batch = deduplicate(result.entries)

    existing = await run_db(crud.list_channels)
    known = {
        (channel.resolve_url or channel.input_url or "").strip().lower()
        for channel in existing
        if (channel.resolve_url or channel.input_url)
    }
    known_names = {channel.name.strip().lower() for channel in existing}

    default_mode = ctx.store.get_str("default_stream_mode")
    default_rtmp = ctx.store.get_str("default_rtmp_server")
    created: list[dict[str, Any]] = []
    skipped = duplicates_in_batch

    for index, entry in enumerate(entries):
        marker = (entry.get("resolve_url") or entry.get("input_url") or "").strip().lower()
        if marker and marker in known:
            skipped += 1
            continue

        name = entry["name"]
        if name.strip().lower() in known_names:
            name = f"{name} ({index + 1})"

        stream_key = entry.get("stream_key") or ""
        if payload.stream_key_prefix and not stream_key:
            stream_key = f"{payload.stream_key_prefix}{index + 1:02d}"

        channel = await run_db(
            crud.create_channel,
            name=name,
            provider_id=payload.provider_id,
            provider_ref=entry.get("provider_ref") or "",
            resolve_url=entry.get("resolve_url") or "",
            input_url=entry.get("input_url") or "",
            logo_url=entry.get("logo_url") or "",
            group_title=entry.get("group_title") or "",
            rtmp_url=entry.get("rtmp_url") or "",
            stream_key=stream_key,
            stream_mode=payload.stream_mode or default_mode,
            enabled=payload.enabled,
            auto_start=payload.auto_start,
        )
        if not channel.resolved_rtmp(default_rtmp):
            await run_db(
                crud.set_channel_status, channel.id, ChannelState.CONFIG_REQUIRED
            )
        await run_db(
            crud.add_event,
            event_type=EventType.CHANNEL_ADDED,
            message="added from bulk import",
            channel_id=channel.id,
            channel_name=channel.name,
        )
        known.add(marker)
        known_names.add(name.strip().lower())
        created.append({"id": channel.id, "name": channel.name})

    return {
        "ok": bool(created),
        "created": created,
        "skipped": skipped,
        "errors": result.errors,
    }


@router.post("/channels/preview-list")
async def api_preview_channel_list(payload: ChannelListPayload) -> dict[str, Any]:
    """Parse a bulk list without saving anything, for the preview table."""
    result = parse_channel_list(payload.text)
    entries, duplicates = deduplicate(result.entries)
    return {
        "ok": bool(entries),
        "count": len(entries),
        "duplicates": duplicates,
        "errors": result.errors,
        "entries": [
            {
                "name": entry["name"],
                "resolve_url": mask_url_token(entry.get("resolve_url") or ""),
                "input_url": mask_url_token(entry.get("input_url") or ""),
                "stream_key": entry.get("stream_key") or "",
                "kind": "media" if entry.get("input_url") else "endpoint",
            }
            for entry in entries[:50]
        ],
    }


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
@router.get("/providers")
async def api_providers(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    rows = await run_db(crud.list_providers)
    return {
        "providers": [
            serialize_provider(
                row,
                secrets_present={
                    key: ctx.providers.has_secret(row.id, key)
                    for key in PROVIDER_SECRET_KEYS
                },
                instance=ctx.providers.get(row.id),
            )
            for row in rows
        ],
        "types": translate_provider_types(
            ProviderFactory.available(), ctx.store.get_str("ui_language")
        ),
    }


@router.post("/iptv")
async def api_iptv_save(
    payload: IptvPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Create or update an IPTV source and its channels in one call.

    The login section becomes the provider's configuration; each row of the
    channel list becomes a channel that carries its own URL. Existing channels
    with the same URL are skipped, so saving again to add a few more is safe.
    """
    config = IptvProvider.build_config(
        requires_login=payload.requires_login,
        base_url=payload.base_url,
        login_url=payload.login_url,
        success_url=payload.success_url,
        prime_url=payload.prime_url,
        username_field=payload.username_field,
        password_field=payload.password_field,
        url_path=payload.url_path,
    )

    # ---- provider -----------------------------------------------------
    if payload.id:
        provider = await run_db(crud.get_provider, payload.id)
        if provider is None:
            raise HTTPException(status_code=404, detail="provider not found")
        await run_db(
            crud.update_provider,
            payload.id,
            name=payload.name,
            type="iptv",
            config=config,
            enabled=payload.enabled,
        )
        provider_id = payload.id
    else:
        provider = await run_db(
            crud.create_provider,
            name=payload.name,
            type="iptv",
            config=config,
            enabled=payload.enabled,
        )
        provider_id = provider.id

    # ---- credentials (write-only; blank means "leave unchanged") -------
    if payload.requires_login:
        if payload.username is not None:
            ctx.providers.set_secret(provider_id, "username", payload.username)
        if payload.password is not None:
            ctx.providers.set_secret(provider_id, "password", payload.password)
    await ctx.providers.reload_one(provider_id)

    # ---- channels: update existing rows, create new ones, delete removed --
    existing = await run_db(crud.list_channels_for_provider, provider_id)
    by_id = {c.id: c for c in existing}
    known_urls = {
        (c.resolve_url or c.input_url or "").strip().lower(): c.id
        for c in existing
        if (c.resolve_url or c.input_url)
    }

    default_rtmp = ctx.store.get_str("default_rtmp_server")
    default_mode = payload.stream_mode or ctx.store.get_str("default_stream_mode")
    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    deleted: list[int] = []
    skipped = 0
    errors: list[str] = []
    submitted_ids: set[int] = set()

    for index, row in enumerate(payload.channels):
        url = (row.url or "").strip()
        name = (row.name or "").strip()
        if not url and row.id is None:
            continue
        if url and not url.lower().startswith(("http://", "https://")):
            errors.append(f"row {index + 1}: URL must start with http:// or https://")
            continue

        # ---- update a row that came from an existing channel ----------
        if row.id is not None and row.id in by_id:
            submitted_ids.add(row.id)
            fields: dict[str, Any] = {}
            if name:
                fields["name"] = name
            if url:
                fields["resolve_url"] = url
            fields["stream_key"] = (row.stream_key or "").strip()
            await run_db(crud.update_channel, row.id, **fields)
            updated.append({"id": row.id, "name": name or by_id[row.id].name})
            continue

        # ---- a new row -------------------------------------------------
        if not url:
            continue
        if url.lower() in known_urls:
            skipped += 1
            continue
        if not name:
            name = f"Channel {index + 1}"
        channel = await run_db(
            crud.create_channel,
            name=name,
            provider_id=provider_id,
            resolve_url=url,
            stream_key=(row.stream_key or "").strip(),
            stream_mode=default_mode,
            enabled=payload.enabled,
            auto_start=payload.auto_start,
        )
        if not channel.resolved_rtmp(default_rtmp):
            await run_db(
                crud.set_channel_status, channel.id, ChannelState.CONFIG_REQUIRED
            )
        await run_db(
            crud.add_event,
            event_type=EventType.CHANNEL_ADDED,
            message="added from IPTV form",
            channel_id=channel.id,
            channel_name=channel.name,
        )
        known_urls[url.lower()] = channel.id
        created.append({"id": channel.id, "name": channel.name})

    # ---- delete channels the operator removed from the list -----------
    # Only within the set the form actually loaded, so a form that never
    # populated the rows can never wipe existing channels.
    for channel_id in payload.known_channel_ids:
        if channel_id in by_id and channel_id not in submitted_ids:
            await ctx.streams.stop_channel(channel_id, "removed from IPTV form")
            ctx.streams.forget(channel_id)
            ctx.notifier.forget_channel(channel_id)
            if await run_db(crud.delete_channel, channel_id):
                deleted.append(channel_id)

    return {
        "ok": True,
        "provider_id": provider_id,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "errors": errors,
    }


@router.get("/iptv/{provider_id}/credentials")
async def api_iptv_credentials(
    provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Return the stored username/password for an IPTV source's edit form.

    The operator asked to see the credentials they entered when editing, so the
    dashboard can pre-fill them. This exposes them to the local dashboard only
    (bind stays on 127.0.0.1 unless the operator changes it); they are still
    never written to the database or to a log.
    """
    await _require_provider(provider_id)
    stored = ctx.providers.secrets_for(provider_id)
    return {
        "username": stored.get("username", ""),
        "password": stored.get("password", ""),
    }


@router.post("/iptv/test-url")
async def api_iptv_test_url(
    payload: IptvPreviewPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Resolve one channel URL through an unsaved IPTV config and ffprobe it.

    This is the per-row "test" button: it does exactly what a running channel
    would - log in if needed, fetch the URL, pull the media URL out with the
    chosen JSON field (or auto), and probe it - without saving anything or
    starting FFmpeg.
    """
    from app.core.security import mask_url_token
    from app.providers.base import ChannelInfo, ProviderError
    from app.providers.iptv import IptvProvider
    from app.streaming.probe import probe_stream

    url = (payload.url or "").strip()
    if not url:
        return {"ok": False, "error": "enter a channel URL first"}

    secrets: dict[str, str] = {}
    if payload.requires_login:
        stored = ctx.providers.secrets_for(payload.provider_id) if payload.provider_id else {}
        secrets["username"] = payload.username or stored.get("username", "")
        secrets["password"] = payload.password or stored.get("password", "")

    config = IptvProvider.build_config(
        requires_login=payload.requires_login,
        base_url=payload.base_url,
        login_url=payload.login_url,
        success_url=payload.success_url,
        prime_url=payload.prime_url,
        username_field=payload.username_field,
        password_field=payload.password_field,
        url_path=payload.url_path,
    )
    provider = IptvProvider(name="test", config=config, secrets=secrets)
    await provider.start()

    # Log in first, explicitly, so a login failure is reported as such and the
    # fetch below reuses the session.
    logged_in = False
    if payload.requires_login:
        if not secrets.get("username") or not secrets.get("password"):
            await provider.aclose()
            return {
                "ok": False,
                "stage": "login",
                "logged_in": False,
                "error": "enter the username and password above first",
            }
        try:
            await provider.authenticate(force=True)
            logged_in = True
        except ProviderError as exc:
            await provider.aclose()
            return {"ok": False, "stage": "login", "logged_in": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            await provider.aclose()
            return {"ok": False, "stage": "login", "logged_in": False, "error": str(exc)}

    try:
        stream = await provider.resolve_stream(
            ChannelInfo(id="test", name="test", metadata={"resolve_url": url})
        )
    except ProviderError as exc:
        await provider.aclose()
        return {"ok": False, "stage": "resolve", "logged_in": logged_in, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        await provider.aclose()
        return {"ok": False, "stage": "resolve", "logged_in": logged_in, "error": f"unexpected error: {exc}"}
    await provider.aclose()

    probe = await probe_stream(
        stream.url,
        ffprobe_path=ctx.store.get_str("ffprobe_path"),
        timeout=float(ctx.store.get_int("probe_timeout_seconds")),
        headers=stream.request_headers() or None,
        user_agent=stream.user_agent,
    )
    return {
        "ok": probe.ok,
        "stage": "probe",
        "logged_in": logged_in,
        "resolved_url": mask_url_token(stream.url),
        "video_codec": probe.video_codec,
        "audio_codec": probe.audio_codec,
        "resolution": probe.resolution,
        "summary": probe.summary(),
        "error": "" if probe.ok else probe.error,
    }


@router.post("/iptv/test-login")
async def api_iptv_test_login(
    payload: IptvPreviewPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Log in with the entered credentials only - no URL fetch, no probe.

    This is the form's "test login" button: it primes the login page, submits
    the username and password, and checks the success signal (a ``success_url``
    landing when configured, otherwise that no login form bounced back). It
    saves nothing and starts nothing.
    """
    from app.providers.base import ProviderError
    from app.providers.iptv import IptvProvider

    if not payload.requires_login:
        return {
            "ok": False,
            "logged_in": False,
            "error": "this source is not set to need a login",
        }

    stored = (
        ctx.providers.secrets_for(payload.provider_id) if payload.provider_id else {}
    )
    username = payload.username or stored.get("username", "")
    password = payload.password or stored.get("password", "")
    if not username or not password:
        return {
            "ok": False,
            "logged_in": False,
            "error": "enter the username and password above first",
        }

    if not (payload.login_url or "").strip() and not (payload.base_url or "").strip():
        return {
            "ok": False,
            "logged_in": False,
            "error": "enter the login URL first",
        }

    config = IptvProvider.build_config(
        requires_login=True,
        base_url=payload.base_url,
        login_url=payload.login_url,
        success_url=payload.success_url,
        prime_url=payload.prime_url,
        username_field=payload.username_field,
        password_field=payload.password_field,
        url_path=payload.url_path,
    )
    provider = IptvProvider(
        name="test", config=config, secrets={"username": username, "password": password}
    )
    await provider.start()
    try:
        await provider.authenticate(force=True)
    except ProviderError as exc:
        debug = getattr(provider, "_login_debug", {})
        await provider.aclose()
        return {"ok": False, "logged_in": False, "error": str(exc), "debug": debug}
    except Exception as exc:  # noqa: BLE001
        debug = getattr(provider, "_login_debug", {})
        await provider.aclose()
        return {
            "ok": False,
            "logged_in": False,
            "error": f"unexpected error: {exc}",
            "debug": debug,
        }
    debug = getattr(provider, "_login_debug", {})
    await provider.aclose()
    return {"ok": True, "logged_in": True, "message": "login succeeded", "debug": debug}


@router.post("/iptv/preview")
async def api_iptv_preview(
    payload: IptvPreviewPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Fetch one URL through an unsaved IPTV config and describe the response.

    Lets the operator see the JSON their source returns and pick the field that
    holds the stream URL, instead of guessing. Tokens are masked in everything
    returned. This performs the same authorised request the app makes at run
    time - it never logs into anything the operator has not configured here.
    """
    import json as _json

    from app.core.security import mask_url_token
    from app.providers.base import ChannelInfo, ProviderError
    from app.providers.extract import extract_stream_urls
    from app.providers.iptv import IptvProvider
    from app.providers.jsonpath import suggest_string_paths
    from app.providers.util import join_url

    url = (payload.url or "").strip()
    if not url:
        return {"ok": False, "error": "enter a channel URL first"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "the URL must start with http:// or https://"}

    secrets: dict[str, str] = {}
    if payload.requires_login:
        secrets["username"] = payload.username or (
            ctx.providers.secrets_for(payload.provider_id).get("username", "")
            if payload.provider_id
            else ""
        )
        secrets["password"] = payload.password or (
            ctx.providers.secrets_for(payload.provider_id).get("password", "")
            if payload.provider_id
            else ""
        )

    config = IptvProvider.build_config(
        requires_login=payload.requires_login,
        base_url=payload.base_url,
        login_url=payload.login_url,
        success_url=payload.success_url,
        prime_url=payload.prime_url,
        username_field=payload.username_field,
        password_field=payload.password_field,
    )
    provider = IptvProvider(name="preview", config=config, secrets=secrets)
    await provider.start()

    logged_in = False
    if payload.requires_login:
        if not secrets.get("username") or not secrets.get("password"):
            await provider.aclose()
            return {
                "ok": False,
                "stage": "login",
                "logged_in": False,
                "error": "enter the username and password above first",
            }
        try:
            await provider.authenticate(force=True)
            logged_in = True
        except ProviderError as exc:
            await provider.aclose()
            return {"ok": False, "stage": "login", "logged_in": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            await provider.aclose()
            return {"ok": False, "stage": "login", "logged_in": False, "error": str(exc)}

    try:
        target = join_url(payload.base_url, url)
        response = await provider._request(target)  # noqa: SLF001 - internal reuse
    except ProviderError as exc:
        await provider.aclose()
        return {"ok": False, "logged_in": logged_in, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        await provider.aclose()
        return {"ok": False, "logged_in": logged_in, "error": f"request failed: {exc}"}

    content_type = response.headers.get("content-type", "")
    body = response.text
    result: dict[str, Any] = {
        "ok": True,
        "logged_in": logged_in,
        "status": response.status_code,
        "content_type": content_type,
        "final_url": mask_url_token(str(response.url)),
    }

    # detected candidate media URLs (masked)
    candidates = extract_stream_urls(body, base_url=str(response.url))
    result["candidates"] = [mask_url_token(u) for u in candidates[:8]]

    # JSON field suggestions, when the body is JSON
    try:
        parsed = response.json()
    except (_json.JSONDecodeError, ValueError):
        parsed = None
    if parsed is not None:
        paths = suggest_string_paths(parsed)
        result["paths"] = [
            {
                "path": p["path"],
                "value": mask_url_token(p["value"]) if p["looks_like_url"] else p["value"],
                "looks_like_url": p["looks_like_url"],
                "looks_like_media": p["looks_like_media"],
            }
            for p in paths[:25]
        ]
        # a readable, masked snapshot of the JSON
        result["json_preview"] = _redact_json_preview(parsed)
    else:
        snippet = body[:1500]
        # mask any URL-with-token inside the raw text
        result["text_preview"] = " ".join(
            mask_url_token(tok) if "://" in tok else tok for tok in snippet.split(" ")
        )

    await provider.aclose()
    return result


def _redact_json_preview(node: Any, depth: int = 0) -> Any:
    """Recursively shorten strings and mask URL tokens for display."""
    from app.core.security import mask_url_token

    if isinstance(node, dict):
        return {k: _redact_json_preview(v, depth + 1) for k, v in list(node.items())[:40]}
    if isinstance(node, list):
        return [_redact_json_preview(v, depth + 1) for v in node[:8]]
    if isinstance(node, str):
        if "://" in node:
            return mask_url_token(node)
        return node if len(node) <= 160 else node[:160] + "…"
    return node


@router.get("/providers/types")
async def api_provider_types(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {
        "types": translate_provider_types(
            ProviderFactory.available(), ctx.store.get_str("ui_language")
        )
    }


@router.post("/providers", status_code=201)
async def api_create_provider(
    payload: ProviderPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    if payload.type not in ProviderFactory.types():
        raise HTTPException(status_code=400, detail=f"unknown provider type {payload.type!r}")
    provider = await run_db(
        crud.create_provider,
        name=payload.name,
        type=payload.type,
        config=payload.config,
        enabled=payload.enabled,
        is_default=payload.is_default,
    )
    _store_provider_secrets(ctx, provider.id, payload)
    await ctx.providers.reload_one(provider.id)
    return {"ok": True, "id": provider.id}


@router.put("/providers/{provider_id}")
async def api_update_provider(
    provider_id: int, payload: ProviderPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    await _require_provider(provider_id)
    if payload.type not in ProviderFactory.types():
        raise HTTPException(status_code=400, detail=f"unknown provider type {payload.type!r}")
    await run_db(
        crud.update_provider,
        provider_id,
        name=payload.name,
        type=payload.type,
        config=payload.config,
        enabled=payload.enabled,
        is_default=payload.is_default,
    )
    _store_provider_secrets(ctx, provider_id, payload)
    await ctx.providers.reload_one(provider_id)
    return {"ok": True}


def _store_provider_secrets(
    ctx: AppContext, provider_id: int, payload: ProviderPayload
) -> None:
    """Persist credentials to the keychain. ``None`` means 'leave unchanged'."""
    for key in PROVIDER_SECRET_KEYS:
        value = getattr(payload, key, None)
        if value is None:
            continue
        ctx.providers.set_secret(provider_id, key, value)


@router.delete("/providers/{provider_id}")
async def api_delete_provider(
    provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    await _require_provider(provider_id)
    ctx.providers.clear_secrets(provider_id)
    deleted = await run_db(crud.delete_provider, provider_id)
    await ctx.providers.reload_one(provider_id)
    return {"ok": deleted}


@router.post("/providers/{provider_id}/test-auth")
async def api_test_auth(
    provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Dashboard 'Test Authentication'."""
    await _require_provider(provider_id)
    provider = ctx.providers.get(provider_id) or await ctx.providers.reload_one(provider_id)
    if provider is None:
        return {"ok": False, "error": "provider is disabled or could not be built"}
    try:
        await provider.authenticate(force=True) if _accepts_force(provider) else await provider.authenticate()
    except ProviderError as exc:
        await run_db(crud.set_provider_auth_state, provider_id, ok=False, error=str(exc))
        await run_db(
            crud.add_event,
            event_type=EventType.PROVIDER_AUTH_FAILED,
            message=str(exc),
            level="error",
        )
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("provider auth test failed")
        return {"ok": False, "error": f"unexpected error: {exc}"}
    await run_db(crud.set_provider_auth_state, provider_id, ok=True, error="")
    await run_db(
        crud.add_event, event_type=EventType.PROVIDER_AUTH_OK, message="login successful"
    )
    return {"ok": True, "message": "Login Successful"}


def _accepts_force(provider: Any) -> bool:
    import inspect

    try:
        return "force" in inspect.signature(provider.authenticate).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


@router.post("/providers/{provider_id}/test-channels")
async def api_test_channels(
    provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Dashboard 'Test Channel List'."""
    await _require_provider(provider_id)
    provider = ctx.providers.get(provider_id)
    if provider is None:
        return {"ok": False, "error": "provider is disabled or could not be built"}
    try:
        channels: list[ChannelInfo] = await provider.list_channels()
    except DiscoveryNotSupported as exc:
        return {"ok": False, "error": str(exc), "unsupported": True}
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("channel list test failed")
        return {"ok": False, "error": f"unexpected error: {exc}"}
    return {
        "ok": True,
        "count": len(channels),
        "sample": [c.as_dict() for c in channels[:10]],
    }


@router.post("/providers/{provider_id}/test-resolve")
async def api_test_resolve(
    provider_id: int,
    channel_id: int = Body(embed=True),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Dashboard 'Test Stream Resolver': resolve then ffprobe, no FFmpeg."""
    await _require_provider(provider_id)
    channel = await _require_channel(channel_id)
    outcome = await ctx.resolver.test(channel)
    payload = outcome.as_dict(reveal=ctx.store.get_bool("show_full_source_url"))
    probe = outcome.probe
    payload["report"] = {
        "source_reachable": bool(probe and probe.ok),
        "video_codec": probe.video_codec if probe else "",
        "audio_codec": probe.audio_codec if probe else "",
        "resolution": probe.resolution if probe else "",
        "elapsed_ms": probe.elapsed_ms if probe else None,
    }
    return payload


@router.get("/providers/{provider_id}/debug")
async def api_provider_debug(
    provider_id: int, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Masked request/response trace for the provider debug page."""
    await _require_provider(provider_id)
    provider = ctx.providers.get(provider_id)
    if provider is None:
        return {"ok": False, "error": "provider is disabled or could not be built"}
    report_fn = getattr(provider, "debug_report", None)
    if report_fn is None:
        health = await provider.health()
        return {"ok": health.ok, "report": {"health": health.as_dict()}}
    try:
        report = await report_fn()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        logger.exception("provider debug failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "report": report}


@router.post("/test-login")
async def api_test_login(
    provider_id: int | None = Body(default=None, embed=True),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Authenticate against a provider (default provider when unspecified)."""
    if provider_id is None:
        row = await run_db(crud.get_default_provider)
        if row is None:
            return {"ok": False, "error": "no provider configured"}
        provider_id = row.id
    return await api_test_auth(provider_id, ctx)


@router.get("/autostart")
async def api_autostart_status(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Whether the app is set to start on boot and restart on crash."""
    from app.core import autostart

    return await asyncio.to_thread(autostart.status)


@router.post("/autostart/install")
async def api_autostart_install(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Register auto-start (Scheduled Task on Windows, LaunchAgent on macOS)."""
    from app.core import autostart

    result = await asyncio.to_thread(autostart.install)
    if result.get("ok"):
        await run_db(
            crud.add_event,
            event_type=EventType.CONFIG_CHANGED,
            message="auto-start enabled",
        )
    return result


@router.post("/autostart/remove")
async def api_autostart_remove(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Remove the auto-start entry."""
    from app.core import autostart

    result = await asyncio.to_thread(autostart.remove)
    if result.get("ok"):
        await run_db(
            crud.add_event,
            event_type=EventType.CONFIG_CHANGED,
            message="auto-start disabled",
        )
    return result


@router.post("/sync")
async def api_sync(
    provider_id: int | None = Body(default=None, embed=True),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Compare provider channel lists with the database."""
    return await ctx.streams.sync_channels(provider_id)


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
@router.post("/telegram/test")
async def api_telegram_test(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return await ctx.notifier.send_test()


# --------------------------------------------------------------------------- #
# events / history / logs
# --------------------------------------------------------------------------- #
@router.get("/events")
async def api_events(
    channel_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    events = await run_db(
        crud.list_events, channel_id=channel_id, limit=limit, offset=offset
    )
    return {"events": [serialize_event(event) for event in events]}


@router.get("/history")
async def api_history(
    channel_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    records = await run_db(crud.list_downtime, channel_id=channel_id, limit=limit)
    summary = await run_db(crud.downtime_summary, days=7)
    return {
        "history": [serialize_downtime(record) for record in records],
        "summary": summary,
    }


@router.get("/logs")
async def api_logs(
    source: str = Query(default="application"),
    channel_id: int | None = None,
    lines: int = Query(default=200, ge=10, le=2000),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Tail a log file. The file is never fully read into memory."""
    if source == "channel":
        if channel_id is None:
            raise HTTPException(status_code=400, detail="channel_id is required")
        path = ctx.settings.ffmpeg_log_dir / f"{channel_id}.log"
    else:
        path = ctx.settings.log_dir / "app.log"
    return {"source": source, "path": path.name, "lines": tail_file(path, lines)}


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@router.get("/settings")
async def api_get_settings(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return {
        "settings": ctx.store.as_dict(),
        "editable": sorted(EDITABLE_KEYS),
        "telegram_token_set": ctx.has_telegram_token(),
        "admin_auth_enabled": ctx.auth_enabled,
        "secret_backend": ctx.secrets.backend,
    }


@router.post("/settings")
async def api_set_settings(
    payload: SettingsPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in payload.values.items():
        try:
            applied[key] = await ctx.persist_setting(key, value)
        except SettingsValidationError as exc:
            errors[key] = str(exc)

    if payload.telegram_bot_token is not None:
        ctx.set_telegram_token(payload.telegram_bot_token)
    if payload.admin_password:
        ctx.set_admin_password(payload.admin_password)

    if errors:
        return {"ok": False, "applied": applied, "errors": errors}
    return {"ok": True, "applied": applied}


# --------------------------------------------------------------------------- #
# configuration backup
# --------------------------------------------------------------------------- #
@router.get("/config/export")
async def api_export(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Export channels, providers and settings. Never exports credentials."""
    channels = await run_db(crud.list_channels)
    providers = await run_db(crud.list_providers)
    return {
        "version": ctx.settings.app_version,
        "exported_at": ctx.started_at.isoformat(),
        "settings": ctx.store.overrides(),
        "providers": [
            {
                "name": provider.name,
                "type": provider.type,
                "enabled": provider.enabled,
                "is_default": provider.is_default,
                "config": provider.config,
            }
            for provider in providers
        ],
        "channels": [
            {
                "name": channel.name,
                "provider_ref": channel.provider_ref,
                "provider_name": next(
                    (p.name for p in providers if p.id == channel.provider_id), ""
                ),
                "input_url": channel.input_url,
                "logo_url": channel.logo_url,
                "group_title": channel.group_title,
                "rtmp_url": channel.rtmp_url,
                "stream_key": channel.stream_key,
                "stream_mode": channel.stream_mode,
                "enabled": channel.enabled,
                "auto_start": channel.auto_start,
                "sort_order": channel.sort_order,
                "playback_referer": channel.playback_referer,
                "playback_user_agent": channel.playback_user_agent,
                "playback_headers_json": channel.playback_headers_json,
            }
            for channel in channels
        ],
        "note": (
            "Credentials and the Telegram bot token are intentionally excluded. "
            "Re-enter them after importing."
        ),
    }


@router.post("/config/import")
async def api_import(
    payload: ImportPayload, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Import a configuration export. Existing rows are matched by name."""
    data = payload.data
    report = {"providers": 0, "channels": 0, "settings": 0, "errors": []}

    for key, value in (data.get("settings") or {}).items():
        try:
            await ctx.persist_setting(key, value)
            report["settings"] += 1
        except SettingsValidationError as exc:
            report["errors"].append(f"setting {key}: {exc}")

    existing_providers = {p.name: p for p in await run_db(crud.list_providers)}
    name_to_id: dict[str, int] = {name: p.id for name, p in existing_providers.items()}
    for entry in data.get("providers") or []:
        name = str(entry.get("name", "")).strip()
        provider_type = str(entry.get("type", "")).strip()
        if not name or provider_type not in ProviderFactory.types():
            report["errors"].append(f"provider {name or '?'}: unknown type {provider_type!r}")
            continue
        if name in existing_providers:
            await run_db(
                crud.update_provider,
                existing_providers[name].id,
                type=provider_type,
                config=entry.get("config") or {},
                enabled=bool(entry.get("enabled", True)),
                is_default=bool(entry.get("is_default", False)),
            )
            name_to_id[name] = existing_providers[name].id
        else:
            created = await run_db(
                crud.create_provider,
                name=name,
                type=provider_type,
                config=entry.get("config") or {},
                enabled=bool(entry.get("enabled", True)),
                is_default=bool(entry.get("is_default", False)),
            )
            name_to_id[name] = created.id
        report["providers"] += 1

    existing_channels = {c.name: c for c in await run_db(crud.list_channels)}
    for entry in data.get("channels") or []:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        fields = {
            "name": name,
            "provider_id": name_to_id.get(str(entry.get("provider_name", "")), None),
            "provider_ref": str(entry.get("provider_ref", "")),
            "input_url": str(entry.get("input_url", "")),
            "logo_url": str(entry.get("logo_url", "")),
            "group_title": str(entry.get("group_title", "")),
            "rtmp_url": str(entry.get("rtmp_url", "")),
            "stream_key": str(entry.get("stream_key", "")),
            "stream_mode": str(entry.get("stream_mode", "copy")),
            "enabled": bool(entry.get("enabled", True)),
            "auto_start": bool(entry.get("auto_start", False)),
            "sort_order": int(entry.get("sort_order", 0) or 0),
            "playback_referer": str(entry.get("playback_referer", "")),
            "playback_user_agent": str(entry.get("playback_user_agent", "")),
            "playback_headers_json": str(entry.get("playback_headers_json", "")),
        }
        if name in existing_channels:
            await run_db(crud.update_channel, existing_channels[name].id, **fields)
        else:
            await run_db(crud.create_channel, **fields)
        report["channels"] += 1

    await ctx.providers.reload()
    return {"ok": not report["errors"], "report": report}
