"""LAN announce endpoint: a route back to the device for external agents.

The household's agent (OpenClaw) can finish long-running work minutes after
the voice request that started it. This endpoint lets it speak the outcome in
the room: POST /announce {"message": "..."} → the text plays through the
device's TTS announcement lane (the same guarded path timers use, so the
assistant can't hear itself and reply).

Enabled only when BOTH announce_port and announce_token options are set.
Auth is a bearer token; binding is on the host network, so treat the token
as the only lock and keep it long. 503 when no device is connected — the
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
_recent: list = []  # (monotonic, normalized_text)


async def start_announce_server(port: int, token: str, announcer, is_connected) -> None:
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
        if not is_connected():
            return web.json_response({"error": "no device connected"}, status=503)
        now = time.monotonic()
        norm = " ".join(message.lower().split())
        _recent[:] = [(t, m) for t, m in _recent if now - t < DUPLICATE_WINDOW_S]
        for _, prev in _recent:
            if difflib.SequenceMatcher(None, norm, prev).ratio() >= DUPLICATE_RATIO:
                logger.info(f"📢 duplicate announce suppressed: {message[:60]}")
                return web.json_response({"status": "duplicate_suppressed",
                                          "note": "already announced — do not retry or re-announce"})
        _recent.append((now, norm))
        logger.info(f"📢 announce: {message[:80]}")
        try:
            await announcer(message)
        except Exception as e:
            logger.warning(f"⚠️ announce failed: {e!r}")
            return web.json_response({"error": "announcement failed"}, status=500)
        return web.json_response({"status": "announced"})

    app = web.Application()
    app.router.add_post("/announce", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"📢 Announce endpoint listening on :{port}/announce")
