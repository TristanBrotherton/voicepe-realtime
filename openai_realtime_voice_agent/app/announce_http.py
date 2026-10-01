"""LAN announce endpoint: a route back to the device for external agents.

The household's agent (OpenClaw) can finish long-running work minutes after
the voice request that started it. This endpoint lets it speak the outcome in
the room: POST /announce {"message": "..."} → the text plays through the
device's TTS announcement lane (the same guarded path timers use, so the
assistant can't hear itself and reply).

Enabled only when BOTH announce_port and announce_token options are set.
Auth is a bearer token; binding is on the host network (0.0.0.0, or
ANNOUNCE_HOST=127.0.0.1 behind a reverse proxy), so treat the token as the
only lock and keep it long. 503 when no device is connected — the
caller (an agent) can fall back to iMessage.
"""
import asyncio
import difflib
import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 600
# Repeat guard: agents monitoring for a result can re-announce the same news
# every poll cycle (observed live: the same "Julie said yes" three times, a
# minute apart, in three phrasings). Near-duplicates within the window are
# accepted-but-not-spoken so the caller doesn't retry.
DUPLICATE_WINDOW_S = 600
DUPLICATE_RATIO = 0.75
_recent: list = []  # (monotonic, device_id, normalized_text)
_pending: list = []  # (device_id, normalized_text) currently being delivered
_announce_lock = asyncio.Lock()


async def start_announce_server(
    port: int, token: str, announcer, is_connected, host: str = "0.0.0.0"
) -> web.AppRunner:
    async def handle(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        message = (body.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
        if not message:
            return web.json_response({"error": "empty message"}, status=400)
        # Optional room selection. With several devices connected, "the
        # device" is ambiguous: an explicit device_id names the room, and
        # omitting it speaks on whichever device was last used.
        device_id = (body.get("device_id") or "").strip() or None
        if not is_connected(device_id):
            error = "device not connected" if device_id else "no device connected"
            return web.json_response({"error": error, "device_id": device_id}, status=503)
        norm = " ".join(message.lower().split())
        async with _announce_lock:
            now = time.monotonic()
            _recent[:] = [(t, target, m) for t, target, m in _recent if now - t < DUPLICATE_WINDOW_S]
            for cached_device_id, prev in [(target, m) for _, target, m in _recent] + _pending:
                if cached_device_id == device_id and difflib.SequenceMatcher(None, norm, prev).ratio() >= DUPLICATE_RATIO:
                    logger.info(f"📢 duplicate announce suppressed: {message[:60]}")
                    return web.json_response({"status": "duplicate_suppressed",
                                              "note": "already announced — do not retry or re-announce"})
            _pending.append((device_id, norm))
        logger.info(f"📢 announce{f' [{device_id}]' if device_id else ''}: {message[:80]}")
        delivered = False
        try:
            delivered = await announcer(message, device_id)
        except Exception as e:
            logger.warning(f"⚠️ announce failed: {e!r}")
            return web.json_response({"error": "announcement failed"}, status=500)
        finally:
            async with _announce_lock:
                _pending.remove((device_id, norm))
                if delivered:
                    _recent.append((time.monotonic(), device_id, norm))
        if not delivered:
            return web.json_response({"error": "announcement failed"}, status=503)
        return web.json_response({"status": "announced", "device_id": device_id})

    app = web.Application()
    app.router.add_post("/announce", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"📢 Announce endpoint listening on {host}:{port}/announce")
    return runner
