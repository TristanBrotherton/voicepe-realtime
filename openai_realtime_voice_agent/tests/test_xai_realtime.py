def test_create_response_option_does_not_shadow_pipecats_method():
    """Live 2026-10-02 20:30: storing the option as `_create_response` replaced
    pipecat's `_create_response()` with a bool, so every tool result crashed
    with "'bool' object is not callable" and the answer was lost. Built for
    real (through __init__), so the 0.25.1 assignment would fail it."""
    from app.providers import XAI, ProviderOptions, build_service

    svc = build_service(XAI, ProviderOptions(api_key="k", model="m", voice="rex", instructions="x",
                                             semantic_vad_create_response=False), [])
    assert svc._xai_create_response is False
    assert callable(svc._create_response)


def test_expressive_tags_are_offered_on_xai_and_can_be_turned_off(monkeypatch):
    from app.providers.xai_realtime import with_expressive_tags

    monkeypatch.delenv("XAI_EXPRESSIVE_TAGS", raising=False)
    assert "[chuckle]" in with_expressive_tags("Du är Björn.")
    assert with_expressive_tags("Du är Björn.").startswith("Du är Björn.")
    monkeypatch.setenv("XAI_EXPRESSIVE_TAGS", "false")
    assert with_expressive_tags("Du är Björn.") == "Du är Björn."
