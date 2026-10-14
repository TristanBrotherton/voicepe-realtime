"""A fast, tool-less verdict that lets plain chat skip the pre-result hold.

In router mode (app/live_router.py) everything the voice says before a
verified result that is not an acknowledgement is held until the router's
verdict. For a chat turn that verdict is the full backend call (1.1-2.3 s with
every tool), so a "hello" waited two to three seconds and then played in a
burst, where native ChatGPT voice answers in under one.

This module runs, in parallel with the full route, one tiny Responses call on a
small fast model with no tools and a few output tokens. It answers CHAT,
NO_REQUEST or REQUEST. The service may act on exactly one answer, CHAT, and
only when every deterministic veto in the service also agrees (see
``OpenAILiveLLMService._chat_fast_path_allowed``). Every other outcome
(REQUEST, NO_REQUEST, an unparseable answer, an error, the latency budget
running out) is ``UNKNOWN`` to the service and changes nothing: the turn is
handled exactly as v33 handles it. The classifier can make chat faster; it can
never open the gate for a request.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

RESPONSES_URL = "https://api.openai.com/v1/responses"

# Measured 2026-10-12 from this Mac (fastchat DESIGN.md): the fastest model
# with zero false gate openings (wording veto + verdict) over 225 labelled and
# 68 held-out calls; its only raw false CHAT ("Um") is caught by the wording
# veto. gpt-5.6-luna at priority had zero raw false CHAT too, ~130 ms slower
# at p50 and twice the cost; set LIVE_FAST_CHAT_MODEL to use it instead.
CLASSIFIER_MODEL = "gpt-5.4-nano"
CLASSIFIER_SERVICE_TIER: Optional[str] = None  # priority is not honoured for nano (measured)
CLASSIFIER_REASONING_EFFORT: Optional[str] = "none"
# The Responses API's floor for max_output_tokens is 16; the answer is one word.
CLASSIFIER_MAX_OUTPUT_TOKENS = 16
# Hard budget from transcript close. A verdict later than this is worth little
# (the backend's own verdict is due at ~1.1 s) and is ignored.
CLASSIFIER_TIMEOUT_S = 1.0

# Charged when a call was sent but no usage came back (timeout, cancel,
# error): the call may still be billed, so the spend ledger counts a
# conservative estimate (about 255 input tokens measured; the output floor).
FLAT_ESTIMATE_USAGE = {"input_tokens": 400, "output_tokens": 16}

CHAT = "CHAT"
NO_REQUEST = "NO_REQUEST"
REQUEST = "REQUEST"
UNKNOWN = "UNKNOWN"

CLASSIFIER_INSTRUCTIONS = """You label one utterance heard by a smart-home voice assistant. The assistant can control devices (lights, locks, doors, media, thermostat, vacuum, car), keep lists, timers and notes, read calendars and messages, check the weather and the news, and search the web.

Answer with exactly one word:
REQUEST - the user wants something done, set, changed, remembered, looked up or checked: any device, list, timer, note, message, calendar, weather, news, sports, prices, travel times, the time or date, anything current or personal, a reply to a question the assistant just asked (yes, no, the other one, do it), a follow-up to an earlier request, or a statement that implies an action ("it's dark in here", "I'm cold").
CHAT - addressed to the assistant and fully answerable from general knowledge with no tool: greetings, thanks, small talk, jokes, definitions, explanations, arithmetic, conversions, history, advice, ideas.
NO_REQUEST - not addressed to the assistant: background speech, television, a conversation between other people, a fragment with no request.

If you are not sure, answer REQUEST."""


# Conservative per-1M-token prices (input, cached input, output) for the spend
# cap only, Fast/priority tier where it exists (developers.openai.com pricing,
# read 2026-10-12). An unknown model is charged at a deliberately high rate.
_PRICES = {
    "gpt-5.6-luna": (0.40, 0.04, 2.40),
    "gpt-6-luna": (0.20, 0.02, 1.00),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-4.1-nano": (0.20, 0.05, 0.80),
}
_UNKNOWN_PRICE = (2.50, 0.25, 15.0)


def classifier_usage_usd(model: str, usage: dict) -> float:
    """A conservative cost estimate of one classifier call."""
    usage = usage or {}
    inp = int(usage.get("input_tokens") or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    p_in, p_cached, p_out = _PRICES.get(model, _UNKNOWN_PRICE)
    return ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1e6


@dataclass
class Verdict:
    label: str            # CHAT | NO_REQUEST | REQUEST | UNKNOWN
    ms: int               # wall time of the call
    reason: str = ""      # why UNKNOWN (timeout, error, unparsed)
    raw: str = ""


PostFn = Callable[[dict], Awaitable[dict]]


def parse_label(text: str) -> str:
    """The one label in the model's answer, or UNKNOWN. Strict: anything but a
    single known word (allowing case and trailing punctuation) is UNKNOWN."""
    word = re.sub(r"[^A-Z_]", "", (text or "").strip().upper().replace(" ", "_"))
    return word if word in (CHAT, NO_REQUEST, REQUEST) else UNKNOWN


class ChatClassifier:
    """One tool-less Responses call per utterance, bounded by a hard timeout."""

    def __init__(self, api_key: str, *, model: str = CLASSIFIER_MODEL,
                 instructions: str = CLASSIFIER_INSTRUCTIONS,
                 service_tier: Optional[str] = CLASSIFIER_SERVICE_TIER,
                 reasoning_effort: Optional[str] = CLASSIFIER_REASONING_EFFORT,
                 timeout_s: float = CLASSIFIER_TIMEOUT_S, post: Optional[PostFn] = None,
                 cache_key: str = "voicepe-classifier",
                 on_usage: Optional[Callable[[dict], None]] = None):
        self.api_key = api_key
        self.model = model
        self.instructions = instructions
        self.service_tier = service_tier
        self.reasoning_effort = reasoning_effort
        self.timeout_s = float(timeout_s)
        self.cache_key = cache_key
        self.on_usage = on_usage
        self._post = post or self._http_post
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self._post is not self._http_post

    def body(self, text: str, context: str = "") -> dict:
        body: Dict[str, Any] = {
            "model": self.model,
            "instructions": self.instructions,
            "store": False,
            "max_output_tokens": CLASSIFIER_MAX_OUTPUT_TOKENS,
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        else:
            body["temperature"] = 0
        if self.service_tier:
            body["service_tier"] = self.service_tier
        if self.cache_key:
            body["prompt_cache_key"] = self.cache_key
        items = []
        if context:
            items.append({"role": "developer", "content": "The assistant last said: " + context[:300]})
        items.append({"role": "user", "content": text[:500]})
        body["input"] = items
        return body

    async def _http_post(self, body: dict) -> dict:  # pragma: no cover - the real API
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s + 1.0)
        response = await self._client.post(
            RESPONSES_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code >= 400:
            raise RuntimeError(f"classifier HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _charge_estimate(self) -> None:
        if self.on_usage is not None:
            try:
                self.on_usage(dict(FLAT_ESTIMATE_USAGE))
            except Exception:  # pragma: no cover - defensive
                pass

    async def classify(self, text: str, context: str = "") -> Verdict:
        """Never raises (except cancellation) and never takes longer than the budget."""
        started = time.monotonic()
        if not self.available or not (text or "").strip():
            return Verdict(UNKNOWN, 0, "unavailable")
        try:
            data = await asyncio.wait_for(self._post(self.body(text.strip(), context)), self.timeout_s)
        except asyncio.CancelledError:
            self._charge_estimate()
            raise
        except asyncio.TimeoutError:
            self._charge_estimate()
            return Verdict(UNKNOWN, int((time.monotonic() - started) * 1000), "timeout")
        except Exception as e:
            self._charge_estimate()
            return Verdict(UNKNOWN, int((time.monotonic() - started) * 1000), f"error {e!r}"[:120])
        ms = int((time.monotonic() - started) * 1000)
        if self.on_usage is not None:
            try:
                self.on_usage(data.get("usage") or {})
            except Exception:  # pragma: no cover - defensive
                pass
        if not isinstance(data, dict) or data.get("status") not in (None, "completed"):
            return Verdict(UNKNOWN, ms, f"status {data.get('status') if isinstance(data, dict) else '?'}")
        raw = "".join(part.get("text", "") for o in data.get("output") or [] if o.get("type") == "message"
                      for part in o.get("content") or [] if isinstance(part, dict))
        label = parse_label(raw)
        return Verdict(label, ms, "" if label != UNKNOWN else "unparsed", raw.strip()[:40])
