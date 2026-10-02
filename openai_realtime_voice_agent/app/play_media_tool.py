"""Play something by name, using Music Assistant's own search.

Home Assistant's HassMediaSearchAndPlay searches every provider at once and
ranks the results itself. Ask for "P3" -- a national radio station -- and it
starts a Spotify track called "Knæklys", because a track happened to score
higher than a station. Nothing in the tool lets the assistant say "I mean the
radio station".

Music Assistant can. `music_assistant.search` takes a media_type filter and
returns one bucket per kind, across every provider the house has connected
(the local library, Spotify, TuneIn, Radio Browser, iTunes podcasts). So the
model names the kind of thing it wants and gets that kind. Verified live:

    search "P3"    type=radio    -> 'P3' (tunein), 'Sveriges Radio P3' (library)
    search "Kent"  type=artist   -> 'Kent' (library), 'kent' (spotify)
    search "chill" type=playlist -> 'Chill-mix' (spotify)

Then `music_assistant.play_media` plays the winning uri on a chosen player.

Everything here is a REST call through raawr-comms (app/ha_api.py), the same way
search_home_tool reads states.
"""
import logging
import os
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

import httpx

from app import ha_api

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)


# The kinds Music Assistant can search for. Order matters when the model does
# not name one: a bare "put on P3" is a radio station long before it is a
# Spotify artist who also happens to be called P3.
MEDIA_TYPES = ["radio", "playlist", "artist", "album", "podcast", "audiobook", "track"]

# search returns one bucket per kind, and the bucket names are plural.
_BUCKET = {
    "radio": "radio",
    "playlist": "playlists",
    "artist": "artists",
    "album": "albums",
    "podcast": "podcasts",
    "audiobook": "audiobooks",
    "track": "tracks",
}

# Music Assistant players carry this attribute and nothing else does, so it is
# how a playable speaker is told apart from the plain ESPHome media player that
# sits on the same device.
_MA_MARKER = "mass_player_type"

_SEARCH_LIMIT = 8

# How many results to try before giving up. Dead streams come in ones and twos,
# not in tens, and every attempt is a second of the user waiting.
_PLAY_ATTEMPTS = 3

# Cached because it never changes while the add-on runs.
_config_entry_id: Optional[str] = None


def get_play_media_tool_definition() -> Dict[str, Any]:
    """OpenAI Realtime function-tool definition for playing media."""
    return {
        "type": "function",
        "name": "play_media",
        "description": (
            "Play music, radio, a playlist, podcast or audiobook on a speaker. "
            "Set media_type whenever you can tell: 'P3' and 'BBC' are radio, "
            "'Kent' an artist, 'Abbey Road' an album."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to play, as the user said it.",
                },
                "media_type": {
                    "type": "string",
                    "enum": MEDIA_TYPES,
                    "description": "Leave out only when the user was vague.",
                },
                "player": {
                    "type": "string",
                    "description": (
                        "Room name, or 'hela huset'. Leave out for the room "
                        "the user is in."
                    ),
                },
            },
            "required": ["query"],
        },
    }


def _fold(text: str) -> str:
    """Lowercase and strip accents, so 'Kök' matches 'kok'."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _score_name(name: str, query: str) -> int:
    """How well a result's name answers the query. 0 means no match at all."""
    folded_name = _fold(name)
    folded_query = _fold(query)
    if not folded_name or not folded_query:
        return 0
    if folded_name == folded_query:
        return 100
    words = [w for w in folded_query.split() if w]
    hits = sum(1 for w in words if w in folded_name)
    # One word out of several is not a match, it is a coincidence: searching for
    # "zzzz obefintlig station" must not settle for a station called "La Baby
    # Station". Demand at least half the words.
    if hits * 2 < len(words):
        return 0
    # All words present is much better than some of them; a short name carrying
    # them is better than a long one that buries them.
    complete = 50 if hits == len(words) else 0
    return complete + hits * 5 + max(0, 20 - len(folded_name) // 4)


def _score_player(name: str, wanted: str) -> int:
    """How well a speaker's name answers what the user called it.

    Swedish rooms are said in the definite form -- the speaker is "Kök" and the
    user says "köket", the speaker is "Kontor" and the user says "kontoret". So
    a word also counts as a match when one is the start of the other.
    """
    folded_name = _fold(name)
    folded_wanted = _fold(wanted)
    if not folded_name or not folded_wanted:
        return 0
    if folded_name == folded_wanted:
        return 100
    name_words = folded_name.split()
    score = 0
    for word in folded_wanted.split():
        if word in folded_name:
            score += 10
            continue
        if len(word) >= 3 and any(
            w.startswith(word) or word.startswith(w) for w in name_words if len(w) >= 3
        ):
            score += 8
    if not score:
        return 0
    # A shorter name carrying the same words is the more exact answer.
    return score + max(0, 20 - len(folded_name) // 3)


def _rank(buckets: Dict[str, Any], query: str, media_type: str) -> List[Dict[str, Any]]:
    """Order the search results, best first.

    A whole list rather than one winner, because a result that looks right can
    still refuse to play: Radio Browser hands out streams that Music Assistant
    then cannot resolve, and answers the play call with MediaNotFoundError. The
    caller works down the list.

    Args:
        buckets: The service response, one list per plural media kind.
        query: What the user asked for.
        media_type: The requested kind, or "" to consider every kind.

    Returns:
        Matching items, each with its kind added as "_kind", best first.
    """
    kinds = [media_type] if media_type in _BUCKET else MEDIA_TYPES
    scored: List[Any] = []
    for rank, kind in enumerate(kinds):
        for item in buckets.get(_BUCKET[kind]) or []:
            uri = item.get("uri")
            if not uri:
                continue
            score = _score_name(item.get("name") or "", query)
            if not score:
                continue
            # Prefer what the house already owns. An external provider can
            # hold an exact name match that is the wrong thing entirely -- the
            # TuneIn station called "P3" is Danish, while the library's
            # "Sveriges Radio P3" is the one the user means. A favourite is an
            # even stronger sign, because a person put it there.
            if str(uri).startswith("library://"):
                score += 30
            if item.get("favorite"):
                score += 15
            # And prefer the kinds listed first when no kind was asked for.
            score += (len(kinds) - rank)
            scored.append((score, {**item, "_kind": kind}))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def _pick(buckets: Dict[str, Any], query: str, media_type: str) -> Optional[Dict[str, Any]]:
    """The single best result, or None."""
    ordered = _rank(buckets, query, media_type)
    return ordered[0] if ordered else None


async def _get(client: httpx.AsyncClient, path: str, **params) -> Any:
    response = await client.get(
        ha_api.url(path),
        headers=ha_api.headers(),
        params=params or None,
    )
    response.raise_for_status()
    return response.json()


async def _post(client: httpx.AsyncClient, path: str, body: Dict[str, Any], **params) -> Any:
    response = await client.post(
        ha_api.url(path),
        headers=ha_api.headers(),
        params=params or None,
        json=body,
    )
    response.raise_for_status()
    return response.json()


async def _players(client: httpx.AsyncClient) -> List[Dict[str, str]]:
    """Every Music Assistant player that is currently reachable."""
    states = await _get(client, "/states")
    found = []
    for state in states:
        if not state.get("entity_id", "").startswith("media_player."):
            continue
        attributes = state.get("attributes") or {}
        if _MA_MARKER not in attributes:
            continue
        if state.get("state") in ("unavailable", "unknown"):
            continue
        found.append({
            "entity_id": state["entity_id"],
            "name": attributes.get("friendly_name") or state["entity_id"],
        })
    return found


async def _config_entry(client: httpx.AsyncClient) -> Optional[str]:
    """The Music Assistant config entry id, which search requires."""
    global _config_entry_id
    if _config_entry_id:
        return _config_entry_id
    entries = await _get(client, "/config/config_entries/entry", domain="music_assistant")
    for entry in entries or []:
        if entry.get("entry_id"):
            _config_entry_id = entry["entry_id"]
            return _config_entry_id
    return None


def create_play_media_tool_handler() -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """Create the play_media handler.

    With no player named, playback goes to the room this add-on instance is in
    (the INSTANCE_NAME option) -- "put on P3" in the office must not start the
    kitchen.
    """
    default_player = os.environ.get("INSTANCE_NAME", "").strip()

    async def play_media_tool_handler(params: "FunctionCallParams") -> None:
        args = params.arguments or {}
        query = str(args.get("query", "")).strip()
        media_type = str(args.get("media_type") or "").strip().lower()
        wanted = str(args.get("player") or "").strip() or default_player

        logger.info(
            f"🎵 play_media: {query!r} type={media_type or 'any'} player={wanted or 'unset'}"
        )

        if not query:
            await params.result_callback("The user did not say what to play.")
            return
        if not ha_api.configured():
            logger.error("❌ play_media: HA_API_URL or COMMS_NYCKEL missing")
            await params.result_callback("I cannot reach the music system right now.")
            return

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                players = await _players(client)
                if not players:
                    await params.result_callback("No speakers are available.")
                    return

                target = None
                if wanted:
                    scored = [(_score_player(p["name"], wanted), p) for p in players]
                    scored = [s for s in scored if s[0]]
                    if scored:
                        scored.sort(key=lambda s: s[0], reverse=True)
                        target = scored[0][1]
                if target is None:
                    names = ", ".join(sorted(p["name"] for p in players))
                    logger.info(f"🎵 play_media: no player matching {wanted!r}")
                    await params.result_callback(
                        f"There is no speaker called '{wanted}'. There is: {names}."
                    )
                    return

                entry_id = await _config_entry(client)
                if not entry_id:
                    await params.result_callback("The music library is not set up.")
                    return

                body: Dict[str, Any] = {
                    "config_entry_id": entry_id,
                    "name": query,
                    "limit": _SEARCH_LIMIT,
                }
                if media_type in _BUCKET:
                    body["media_type"] = [media_type]
                result = await _post(
                    client, "/services/music_assistant/search", body, return_response="true"
                )
                buckets = (result or {}).get("service_response") or {}

                candidates = _rank(buckets, query, media_type)
                looked = media_type or "anything"
                if not candidates:
                    logger.info(f"🎵 play_media: nothing found for {query!r} ({looked})")
                    await params.result_callback(
                        f"I could not find {looked} called '{query}'."
                    )
                    return

                # The best-looking result can still refuse to play: Radio
                # Browser lists streams that Music Assistant cannot resolve, and
                # the play call comes back as MediaNotFoundError. So work down
                # the list instead of giving up on the first dud.
                for match in candidates[:_PLAY_ATTEMPTS]:
                    try:
                        await _post(
                            client,
                            "/services/music_assistant/play_media",
                            {
                                "entity_id": target["entity_id"],
                                "media_id": match["uri"],
                                "media_type": match["_kind"],
                            },
                        )
                    except httpx.HTTPStatusError as e:
                        logger.info(
                            f"🎵 play_media: {match.get('name')!r} ({match['uri']}) "
                            f"would not play ({e.response.status_code}), trying the next"
                        )
                        continue
                    logger.info(
                        f"🎵 play_media: {match.get('name')!r} ({match['_kind']}, {match['uri']}) "
                        f"on {target['name']}"
                    )
                    await params.result_callback(
                        f"Playing {match.get('name')} on {target['name']}."
                    )
                    return

                logger.info(f"🎵 play_media: every candidate for {query!r} refused to play")
                await params.result_callback(
                    f"I found {looked} called '{query}' but it would not play."
                )
        except Exception as e:
            logger.error(f"❌ play_media failed: {e}", exc_info=True)
            await params.result_callback("Starting that did not work just now.")

    return play_media_tool_handler
