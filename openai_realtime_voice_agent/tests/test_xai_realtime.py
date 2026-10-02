

def test_create_response_option_does_not_shadow_pipecats_method():
    """Live 2026-10-02 20:30: storing the option as `_create_response` replaced
    pipecat's `_create_response()` with a bool, so every tool result crashed
    with "'bool' object is not callable" and the answer was lost."""
    from app.providers.xai_realtime import XaiRealtimeLLMService

    assert callable(XaiRealtimeLLMService._create_response)
    svc = XaiRealtimeLLMService.__new__(XaiRealtimeLLMService)
    svc._xai_create_response = False
    assert callable(svc._create_response)
