"""A cheap household-state snapshot for the router backend, taken at wake.

Option ``live_context_entities`` (env ``LIVE_CONTEXT_ENTITIES``): a comma or
space separated list of Home Assistant entity ids. Empty (the default) turns
the whole feature off: nothing is fetched and nothing is added to any prompt.

When set, every GPT-Live session open (a wake) starts one background fetch of
those entities' states from Home Assistant's REST API, in parallel with the
socket open. The fetch is bounded by ``FETCH_TIMEOUT_S``; past it, the
snapshot is skipped for that session. Nothing ever waits for it: the router
backend's volatile context (``router_context`` in app/main.py, the last
developer item before the user's words) includes the snapshot only if it is
already there when the backend request is built. So it cannot delay first
audio, it never enters the cached prefix (instructions, tools, history), and
it cannot open, hold or extend a billed session.

The text is bounded to ``MAX_CHARS`` (about 375 tokens at most) and labelled
as a start-of-conversation snapshot that can be out of date, so the backend
reads a device with a tool before saying it changed.
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Any, Callable, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MAX_ENTITIES = 30
MAX_CHARS = 1500
MAX_LINE_CHARS = 120
FETCH_TIMEOUT_S = 0.8
HA_API = "http://supervisor/core/api"
HEADER = ("Home state when this conversation started ({stamp}; a snapshot that can be "
          "out of date, so read a device with a tool before saying it changed, and never "
          "use it to confirm an action):")


def parse_entities(raw: Optional[str]) -> List[str]:
    """The option's valid entity ids, de-duplicated, at most MAX_ENTITIES."""
    seen: List[str] = []
    for token in re.split(r"[\s,]+", (raw or "").strip().lower()):
        if token and ENTITY_ID.match(token) and token not in seen:
            seen.append(token)
    if len(seen) > MAX_ENTITIES:
        logger.warning(f"⚠️ live_context_entities: using the first {MAX_ENTITIES} of {len(seen)}")
    return seen[:MAX_ENTITIES]


def _line(state: dict) -> str:
    attrs = state.get("attributes") or {}
    name = str(attrs.get("friendly_name") or state.get("entity_id") or "").strip()
    value = str(state.get("state") or "unknown").strip()
    unit = str(attrs.get("unit_of_measurement") or "").strip()
    text = f"- {name}: {value}{(' ' + unit) if unit else ''}"
    text = " ".join(text.split())  # no newlines from attribute text
    return text[:MAX_LINE_CHARS]


def format_snapshot(states: Iterable[Optional[dict]], max_chars: int = MAX_CHARS,
                    now: Optional[datetime] = None) -> str:
    """The bounded snapshot text, or "" when no state came back."""
    lines = [_line(s) for s in states if isinstance(s, dict) and s.get("entity_id")]
    if not lines:
        return ""
    now = now or datetime.now().astimezone()
    hour = now.strftime("%I").lstrip("0") or "12"
    out = HEADER.format(stamp=f"{hour}:{now.strftime('%M %p')}")
    omitted = "\n- (more entities omitted)"
    for index, line in enumerate(lines):
        if len(out) + 1 + len(line) > max_chars - (len(omitted) if index < len(lines) - 1 else 0):
            if len(out) + len(omitted) <= max_chars:
                out += omitted
            break
        out += "\n" + line
    return out[:max_chars]


class HouseholdSnapshot:
    """One best-effort state fetch per session open; read without waiting."""

    def __init__(self, entities: List[str], token_getter: Callable[[], str] = lambda: "",
                 base_url: str = HA_API, timeout_s: float = FETCH_TIMEOUT_S,
                 max_chars: int = MAX_CHARS,
                 client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.entities = list(entities)
        self._token_getter = token_getter
        self._base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_chars = int(max_chars)
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=self.timeout_s))
        self._task: Optional[asyncio.Task] = None
        self._text = ""
        self.fetches = 0
        self.skipped = 0

    @classmethod
    def from_env(cls) -> "HouseholdSnapshot":
        return cls(parse_entities(os.environ.get("LIVE_CONTEXT_ENTITIES", "")),
                   token_getter=lambda: os.environ.get("SUPERVISOR_TOKEN", ""))

    @property
    def enabled(self) -> bool:
        return bool(self.entities)

    def start(self) -> None:
        """Begin this session's fetch (session open). Never blocks, never raises."""
        if not self.enabled:
            return
        # A previous session's text must not be read as this session's.
        self._text = ""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        try:
            self._task = asyncio.get_running_loop().create_task(self._refresh())
        except RuntimeError:
            self._task = None

    def cancel(self) -> None:
        """The session closed: a fetch still running is dropped (household-context review)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def text(self) -> str:
        """The snapshot if it is ready, else "" (callers never wait)."""
        return self._text

    def block(self) -> str:
        """The snapshot as a context block appended to the volatile tail."""
        return f"\n\n{self._text}" if self._text else ""

    async def wait(self) -> str:
        """Tests and diagnostics: the outcome of the current fetch."""
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return self._text

    async def _refresh(self) -> None:
        self.fetches += 1
        try:
            states = await asyncio.wait_for(self._fetch_all(), self.timeout_s)
        except asyncio.TimeoutError:
            self.skipped += 1
            logger.info(f"🏠 household snapshot skipped (no answer in {self.timeout_s:g} s)")
            return
        except Exception as e:
            self.skipped += 1
            logger.debug(f"household snapshot failed: {e!r}")
            return
        self._text = format_snapshot(states, self.max_chars)

    async def _fetch_all(self) -> list:
        token = self._token_getter()
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}"}
        async with self._client_factory() as client:
            async def one(entity: str):
                try:
                    response = await client.get(f"{self._base_url}/states/{entity}", headers=headers)
                    response.raise_for_status()
                    return response.json()
                except Exception:
                    return None
            return list(await asyncio.gather(*(one(e) for e in self.entities)))
