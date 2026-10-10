"""play_media must pick the thing the user meant, on the speaker they meant."""

from app.play_media_tool import _pick, _score_name, _score_player

# Shapes copied from a live music_assistant.search response.
P3_TUNEIN = {"name": "P3", "uri": "tunein--x://radio/s25681", "favorite": False}
P3_LIBRARY = {"name": "Sveriges Radio P3", "uri": "library://radio/4", "favorite": True}
KENT_LIBRARY = {"name": "Kent", "uri": "library://artist/932", "favorite": False}
KENT_SPOTIFY = {"name": "kent", "uri": "spotify--x://artist/4KX", "favorite": False}
RHAPSODY = {"name": "Bohemian Rhapsody", "uri": "spotify--x://track/2Ji", "favorite": False}
BABY_STATION = {"name": "La Baby Station", "uri": "tunein--x://radio/s340179", "favorite": False}


def test_radio_wins_when_the_model_says_radio():
    # The whole point: HassMediaSearchAndPlay played a Spotify track for "P3".
    buckets = {"radio": [P3_TUNEIN, P3_LIBRARY], "tracks": [RHAPSODY]}
    assert _pick(buckets, "P3", "radio")["_kind"] == "radio"


def test_the_houses_own_favourite_beats_an_exact_foreign_name():
    # "P3" is also a Danish station on TuneIn. The library one is the one the
    # user put there and the one they mean.
    buckets = {"radio": [P3_TUNEIN, P3_LIBRARY]}
    assert _pick(buckets, "P3", "radio")["uri"] == "library://radio/4"


def test_library_beats_spotify_for_the_same_artist():
    buckets = {"artists": [KENT_SPOTIFY, KENT_LIBRARY]}
    assert _pick(buckets, "Kent", "artist")["uri"] == "library://artist/932"


def test_external_result_still_wins_when_the_library_has_nothing():
    buckets = {"tracks": [RHAPSODY]}
    assert _pick(buckets, "Bohemian Rhapsody", "track")["uri"].startswith("spotify")


def test_one_word_out_of_three_is_a_coincidence_not_a_match():
    # Music Assistant always returns something. Accepting it turned a nonsense
    # request into "La Baby Station" playing in the office.
    buckets = {"radio": [BABY_STATION]}
    assert _pick(buckets, "zzzz obefintlig station", "radio") is None


def test_nothing_is_picked_when_no_name_matches():
    assert _pick({"radio": [P3_TUNEIN]}, "kalle anka", "radio") is None


def test_items_without_a_uri_are_skipped():
    assert _pick({"radio": [{"name": "P3"}]}, "P3", "radio") is None


def test_without_a_media_type_the_earlier_kinds_win():
    # A bare "put on P3" is a radio station long before it is a Spotify artist
    # who is also called P3.
    buckets = {"artists": [{"name": "P3", "uri": "spotify--x://artist/4kE"}],
               "radio": [P3_LIBRARY]}
    assert _pick(buckets, "P3", "")["_kind"] == "radio"


def test_accents_do_not_block_a_match():
    swede = {"name": "Håkan Hellström", "uri": "library://artist/146"}
    assert _pick({"artists": [swede]}, "hakan hellstrom", "artist") is not None


def test_score_name_rewards_an_exact_match_most():
    assert _score_name("P3", "P3") > _score_name("Sveriges Radio P3", "P3")


# --- speakers ---------------------------------------------------------------

def test_swedish_definite_form_finds_the_speaker():
    # The speaker is "Kök"; people say "köket". Same for Kontor/kontoret.
    assert _score_player("Kök Sonos", "köket") > 0
    assert _score_player("Kontor", "kontoret") > 0
    assert _score_player("Uterum", "uterummet") > 0


def test_an_exact_speaker_name_outranks_a_partial_one():
    assert _score_player("Moas Rum", "moas rum") > _score_player("Sovrum", "moas rum")


def test_an_unknown_room_matches_nothing():
    assert _score_player("Kontor", "badrummet") == 0
    assert _score_player("Hela Huset", "garaget") == 0


# --- a result that looks right but will not play ----------------------------

def test_rank_returns_every_match_best_first():
    # Radio Browser hands out streams Music Assistant cannot resolve, so the
    # handler needs somewhere to go after the first choice fails.
    from app.play_media_tool import _rank
    ordered = _rank({"radio": [P3_TUNEIN, P3_LIBRARY]}, "P3", "radio")
    assert [item["uri"] for item in ordered] == [P3_LIBRARY["uri"], P3_TUNEIN["uri"]]


def test_rank_leaves_out_what_does_not_match():
    from app.play_media_tool import _rank
    ordered = _rank({"radio": [P3_TUNEIN, BABY_STATION]}, "P3", "radio")
    assert [item["name"] for item in ordered] == ["P3"]


# One add-on process serves several devices. With no room named, music goes to the room of the device that
# asked -- not to the process-wide INSTANCE_NAME (kitchen asked 2026-10-09 21:17 and the office played).
import httpx
import pytest
from types import SimpleNamespace

from app import play_media_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("device,speaker", [("koket", "media_player.kok"), ("kontoret", "media_player.kontor"),
                                            ("10.0.3.9", "media_player.kontor"), ("unknown", "media_player.kontor")])
async def test_no_room_named_plays_on_the_asking_device(monkeypatch, device, speaker):
    monkeypatch.setenv("HA_API_URL", "http://comms/")
    monkeypatch.setenv("COMMS_NYCKEL", "k")
    monkeypatch.setenv("INSTANCE_NAME", "kontor")  # the process-wide value that used to win
    monkeypatch.setattr(play_media_tool, "_config_entry_id", None, raising=False)
    played = []

    def handler(request):
        path = request.url.path
        if path.endswith("/states"):
            return httpx.Response(200, json=[
                {"entity_id": e, "state": "idle",
                 "attributes": {"friendly_name": n, "mass_player_type": "player"}}
                for e, n in (("media_player.kok", "Kök"), ("media_player.kontor", "Kontor"))])
        if path.endswith("/config_entries/entry"):
            return httpx.Response(200, json=[{"entry_id": "ma1"}])
        if path.endswith("/music_assistant/search"):
            return httpx.Response(200, json={"service_response": {
                "radio": [{"name": "Sveriges Radio P3", "uri": "library://radio/4", "favorite": True}]}})
        if path.endswith("/music_assistant/play_media"):
            played.append(request.read().decode())
        return httpx.Response(200, json=[])

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    said = []

    async def result_callback(text):
        said.append(text)

    params = SimpleNamespace(arguments={"query": "P3", "media_type": "radio"}, result_callback=result_callback)
    await play_media_tool.create_play_media_tool_handler(device)(params)
    assert len(played) == 1 and speaker in played[0], (played, said)


def test_koket_ar_entydigt_voice_pe_och_sonos_bara_nar_det_sags():
    """Kitchen 2026-10-10: "Kök Voice" and "Kök Sonos" scored 25/25 for "köket"; the radio went to whichever came first."""
    from app.play_media_tool import valj_spelare
    voice = {"entity_id": "media_player.home_assistant_voice_0a8f25", "name": "Kök Voice"}
    sonos = {"entity_id": "media_player.kok_3", "name": "Kök Sonos"}
    kontor = {"entity_id": "media_player.home_assistant_voice_0a6bc9", "name": "Kontor"}
    for ordning in ([voice, sonos, kontor], [sonos, voice, kontor], [kontor, sonos, voice]):
        assert valj_spelare(ordning, "koket") == voice and valj_spelare(ordning, "köket") == voice
        assert valj_spelare(ordning, "köket sonos") == sonos and valj_spelare(ordning, "kök sonos") == sonos
        assert valj_spelare(ordning, "kontoret") == kontor
    assert valj_spelare([sonos], "köket") == sonos  # no Voice PE reachable: Sonos is still better than nothing
    assert valj_spelare([voice, sonos], "garaget") is None
