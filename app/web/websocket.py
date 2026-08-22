"""Status websocket.

An alternative to HTMX polling for the dashboard: connect to ``/ws/status`` and
receive a status frame every few seconds.  Both mechanisms are supported so the
dashboard still works with JavaScript disabled for the websocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.web.api import channel_list
from app.web.context import AppContext
from app.web.schemas import summarise

logger = logging.getLogger(__name__)

router = APIRouter()

#: Seconds between status frames.
PUSH_INTERVAL = 3.0


@router.websocket("/ws/status")
async def status_socket(websocket: WebSocket) -> None:
    ctx: AppContext | None = getattr(websocket.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - startup failure
        await websocket.close(code=1013)
        return

    if ctx.auth_enabled:
        # The websocket inherits the browser session's Basic auth header.
        header = websocket.headers.get("authorization", "")
        if not _basic_ok(ctx, header):
            await websocket.close(code=1008)
            return

    await websocket.accept()
    logger.debug("status websocket connected")
    try:
        while True:
            channels = await channel_list(ctx)
            await websocket.send_json(
                {
                    "type": "status",
                    "summary": summarise(channels),
                    "system": ctx.streams.system_metrics(),
                    "channels": channels,
                }
            )
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.debug("status websocket disconnected")
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception:  # noqa: BLE001 - never let a socket kill the server
        logger.exception("status websocket failed")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)


def _basic_ok(ctx: AppContext, header: str) -> bool:
    import base64
    import binascii

    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    username, _, password = decoded.partition(":")
    return ctx.check_credentials(username, password)
