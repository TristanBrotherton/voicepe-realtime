"""One narrow door to every engine."""

import pytest
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.providers import (
    ProviderOptions,
    build_service,
    input_sample_rate,
    self_heals,
)
from app.providers.openai_realtime import SafeRealtimeLLMService

TOOLS = [{
    "type": "function",
    "name": "search_home",
    "description": "Search the house.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}]


def _options(**over):
    base = dict(
        api_key="sk-test",
        model="gpt-realtime-2",
        voice="cedar",
        instructions="Du är Björn.",
        max_output_tokens=1024,
        speed=1.0,
        noise_reduction="",
        turn_detection_type="semantic_vad",
        vad_eagerness="medium",
        vad_threshold=0.5,
        vad_prefix_padding_ms=300,
        vad_silence_duration_ms=500,
        semantic_vad_create_response=True,
        interrupt_response=True,
        transcription_model="gpt-4o-transcribe",
        transcription_language="sv",
    )
    base.update(over)
    return ProviderOptions(**base)


def test_the_device_rate_is_kept_for_gemini_and_raised_for_openai():
    # The Voice PE microphone produces 16 kHz. OpenAI wants 24 kHz, Gemini
    # wants exactly what the device already sends.
    assert input_sample_rate("openai") == 24000
    assert input_sample_rate("gemini") == 16000


def test_only_gemini_repairs_its_own_connection():
    # pipecat's OpenAI service has no reconnect logic at all; the Gemini one
    # has _reconnect and session resumption. ConnectionRecovery must know.
    assert self_heals("openai") is False
    assert self_heals("gemini") is True


def test_an_unknown_engine_is_refused_loudly():
    with pytest.raises(ValueError, match="unknown provider"):
        build_service("claude", _options(), TOOLS)
    with pytest.raises(ValueError, match="unknown provider"):
        input_sample_rate("claude")


def test_the_openai_service_carries_every_field_that_moved():
    # Every one of these fields moved from create_openai_service's inline
    # SessionProperties construction into openai_realtime.build(). If any of
    # them were dropped, defaulted, or mis-wired along the way, this is a
    # behaviour change heard live in the house -- so assert the real value
    # made it onto the real session, not just the three easy ones (tools,
    # instructions, voice).
    options = _options(
        speed=0.8,
        max_output_tokens=777,
        noise_reduction="near_field",
        transcription_model="gpt-4o-transcribe",
        transcription_language="sv",
    )
    service = build_service("openai", options, TOOLS)
    props = service._session_properties

    from app.early_ack import with_ack_hint
    assert props.tools == with_ack_hint(TOOLS)  # slow tools say what they do (0.25.6)
    assert props.instructions.startswith("Du är Björn.")
    # A missing wire-up would leave this at pydantic's default (None), not 777.
    assert props.max_output_tokens == 777
    assert props.audio.output.voice == "cedar"
    # A missing wire-up would leave speed at None (pydantic default) or 1.0
    # (the _options() default) -- 0.8 only appears if options.speed was read.
    assert props.audio.output.speed == 0.8
    # noise_reduction is None unless options.noise_reduction is truthy; a
    # dropped wire-up would leave this None even though we passed a value.
    assert props.audio.input.noise_reduction.type == "near_field"
    assert props.audio.input.transcription.model == "gpt-4o-transcribe"
    assert props.audio.input.transcription.language == "sv"
    # semantic_vad branch: these three all come from options, not from
    # SemanticTurnDetection's own defaults (which are None for all three).
    turn_detection = props.audio.input.turn_detection
    assert turn_detection.eagerness == "medium"
    assert turn_detection.create_response is True
    assert turn_detection.interrupt_response is True


def test_the_server_vad_branch_carries_its_own_fields():
    # semantic_vad and server_vad are mutually exclusive branches in build();
    # the semantic_vad test above never exercises this one. Without this test,
    # a mis-wired server_vad branch (e.g. threshold and silence_duration_ms
    # swapped, or hardcoded to TurnDetection's own defaults of 0.5/300/500
    # instead of options') would ship green.
    options = _options(
        turn_detection_type="server_vad",
        vad_threshold=0.91,
        vad_prefix_padding_ms=111,
        vad_silence_duration_ms=999,
    )
    service = build_service("openai", options, TOOLS)
    turn_detection = service._session_properties.audio.input.turn_detection

    assert turn_detection.type == "server_vad"
    assert turn_detection.threshold == 0.91
    assert turn_detection.prefix_padding_ms == 111
    assert turn_detection.silence_duration_ms == 999


def test_the_openai_service_still_no_ops_the_truncate():
    # The fork disables truncation-on-interruption: OpenAI bursts the reply
    # faster than real-time, so pipecat's real _truncate_current_audio_response
    # sends a conversation.item.truncate whose audio_end_ms overshoots the
    # audio that actually exists. OpenAI rejects that truncate, and the
    # rejection wedges the realtime session -- the user's very next question
    # gets silence. See the class docstring for the full story.
    #
    # An isinstance() check alone does not prove this override survived the
    # move: deleting the override entirely still leaves the object an
    # instance of SafeRealtimeLLMService, and pipecat's real implementation
    # also happens to return None with no side effect when called on a fresh,
    # unconnected service (no audio in flight yet) -- so calling it proves
    # nothing here either. The only assertion that actually discriminates
    # "override present" from "override silently dropped" is identity against
    # the parent class's version of the same method:
    assert (
        SafeRealtimeLLMService._truncate_current_audio_response
        is not OpenAIRealtimeLLMService._truncate_current_audio_response
    )
