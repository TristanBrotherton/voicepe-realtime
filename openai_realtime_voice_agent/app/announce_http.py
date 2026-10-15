"""LAN announce endpoint: a route back to the device for external agents.

An external agent (for example OpenClaw) can finish long-running work minutes
after the voice request that started it. This endpoint lets it speak the outcome in
the room: POST /announce {"message": "..."} → the text plays through the
device's TTS announcement lane (the same guarded path timers use, so the
assistant can't hear itself and reply).

Enabled only when BOTH announce_port and announce_token options are set.
Auth is a bearer token; binding is on the host network, so treat the token
as the only lock and keep it long. 503 when no device is connected — the
caller (an agent) can fall back to a text channel.

Task results (router mode, app/live_tasks.py): POST /task-result
{"task_id": "...", "answer": "..."} completes a pending background task; the
background task list then delivers the answer into the open conversation, or
through its fallback chain, instead of the bare announcement lane. An
/announce whose ``task_id`` or ``announcement_id`` names a pending task is
routed the same way, so a bridge that only knows the announce contract still
lands its answer in the conversation.
"""
import asyncio
import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 600
# A background task's result is spoken by the Live voice or texted, not played
# on the bare lane, so it may be longer: an explanation's spoken summary is
# 20-40 s of speech; its ``detail`` (the write-up for a text) longer still.
MAX_TASK_TEXT_CHARS = 1500
MAX_TASK_DETAIL_CHARS = 4000
# Repeat guard: agents monitoring for a result can re-announce the same news
# every poll cycle (observed live: the same reply announced three times, a
# minute apart, in three phrasings). Near-duplicates within the window are
# accepted-but-not-spoken so the caller doesn't retry.
DUPLICATE_WINDOW_S = 600
_recent: list = []  # (monotonic, device_id, idempotency_key)
_pending: list = []  # (device_id, idempotency_key) currently being delivered
_announce_lock = asyncio.Lock()


async def start_announce_server(port: int, token: str, announcer, is_connected,
                                task_sink=None) -> web.AppRunner:
    """``task_sink(task_id, text, ok) -> awaitable bool`` claims a result for a
    pending background task (app.live_tasks.deliver_external_result)."""

    async def claim_task(task_id: str, text: str, ok: bool, detail: str = "") -> bool:
        if task_sink is None or not task_id:
            return False
        try:
            if detail:
                return bool(await task_sink(task_id, text, ok, detail))
            return bool(await task_sink(task_id, text, ok))
        except Exception as e:
            logger.warning(f"⚠️ task result sink failed: {e!r}")
            return False

    def task_detail(body: dict) -> str:
        return str(body.get("detail") or body.get("writeup") or "").strip()[:MAX_TASK_DETAIL_CHARS]

    async def handle_task_result(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        task_id = str(body.get("task_id") or body.get("request_id") or "").strip()[:200]
        text = str(body.get("answer") or body.get("message") or "").strip()[:MAX_TASK_TEXT_CHARS]
        # A missing "ok" is NOT ok: bridge error text must never be framed as
        # a verified result (longtask review ii).
        ok = body.get("ok") is True and not body.get("error")
        if not ok and not text:
            text = str(body.get("error") or "").strip()[:MAX_MESSAGE_CHARS]
        if not task_id:
            return web.json_response({"error": "task_id required"}, status=400)
        if not text:
            return web.json_response({"error": "empty answer"}, status=400)
        async with _announce_lock:
            now = time.monotonic()
            _recent[:] = [(t, target, m) for t, target, m in _recent if now - t < DUPLICATE_WINDOW_S]
            if any(key == f"task:{task_id}" for _, _, key in _recent):
                return web.json_response({"status": "duplicate_suppressed",
                                          "note": "this task result was already delivered — do not retry"})
        if not await claim_task(task_id, text, ok, task_detail(body) if ok else ""):
            return web.json_response({"error": "unknown task", "task_id": task_id}, status=404)
        async with _announce_lock:
            _recent.append((time.monotonic(), None, f"task:{task_id}"))
        logger.info(f"📬 task result accepted for {task_id}: {text[:80]}")
        return web.json_response({"status": "accepted", "task_id": task_id})

    async def handle(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        full = (body.get("message") or "").strip()
        message = full[:MAX_MESSAGE_CHARS]
        if not message:
            return web.json_response({"error": "empty message"}, status=400)
        # A result for a pending background task lands in the conversation,
        # not on the bare announcement lane (at the task length, with its
        # write-up when the bridge sent one).
        ok = body.get("ok") is True
        for task_id in (str(body.get("task_id") or "").strip()[:200],
                        str(body.get("announcement_id") or "").strip()[:200]):
            if task_id and await claim_task(task_id, full[:MAX_TASK_TEXT_CHARS], ok,
                                            task_detail(body) if ok else ""):
                async with _announce_lock:
                    _recent.append((time.monotonic(), None, f"task:{task_id}"))
                logger.info(f"📬 announce routed to pending task {task_id}: {message[:80]}")
                return web.json_response({"status": "accepted", "task_id": task_id,
                                          "note": "delivered through the conversation — do not re-announce"})
        # Optional room selection. With several devices connected, "the
        # device" is ambiguous: an explicit device_id names the room, and
        # omitting it speaks on whichever device was last used.
        device_id = (body.get("device_id") or "").strip() or None
        announcement_id = (body.get("announcement_id") or "").strip()[:200]
        if not is_connected(device_id):
            error = "device not connected" if device_id else "no device connected"
            return web.json_response({"error": error, "device_id": device_id}, status=503)
        norm = " ".join(message.lower().split())
        # Exact request IDs make retries idempotent without confusing two
        # distinct answers that happen to use similar wording. Legacy callers
        # without an ID still get exact-text retry suppression.
        idempotency_key = f"id:{announcement_id}" if announcement_id else f"text:{norm}"
        async with _announce_lock:
            now = time.monotonic()
            _recent[:] = [(t, target, m) for t, target, m in _recent if now - t < DUPLICATE_WINDOW_S]
            for cached_device_id, previous_key in [
                (target, key) for _, target, key in _recent
            ] + _pending:
                if cached_device_id == device_id and idempotency_key == previous_key:
                    logger.info(f"📢 duplicate announce suppressed: {message[:60]}")
                    return web.json_response({"status": "duplicate_suppressed",
                                              "note": "already announced — do not retry or re-announce"})
            _pending.append((device_id, idempotency_key))
        logger.info(f"📢 announce{f' [{device_id}]' if device_id else ''}: {message[:80]}")
        delivered = False
        try:
            delivered = await announcer(message, device_id)
        except Exception as e:
            logger.warning(f"⚠️ announce failed: {e!r}")
            return web.json_response({"error": "announcement failed"}, status=500)
        finally:
            async with _announce_lock:
                _pending.remove((device_id, idempotency_key))
                if delivered:
                    _recent.append((time.monotonic(), device_id, idempotency_key))
        if not delivered:
            return web.json_response({"error": "announcement failed"}, status=503)
        return web.json_response({"status": "announced", "device_id": device_id})

    app = web.Application()
    app.router.add_post("/announce", handle)
    app.router.add_post("/task-result", handle_task_result)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"📢 Announce endpoint listening on :{port}/announce")
    return runner
