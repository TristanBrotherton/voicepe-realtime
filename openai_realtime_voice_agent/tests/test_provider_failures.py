"""Every error must be answered with the right thing, not the same thing."""

import re

from app.provider_failures import Failure, classify, _TRANSIENT_CODES, _OURS_PATTERNS


def test_openai_out_of_money_switches_immediately():
    assert classify("You exceeded your current quota, please check your plan") is Failure.MONEY
    assert classify("insufficient_quota") is Failure.MONEY
    assert classify("Billing hard limit has been reached") is Failure.MONEY


def test_gemini_exhausted_quota_switches_immediately():
    # Google says RESOURCE_EXHAUSTED for a spent budget too. It never clears
    # in the seconds a retry would take, so switching is the useful answer.
    assert classify("429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric") is Failure.MONEY


def test_a_dead_key_switches_immediately():
    assert classify("Incorrect API key provided: sk-abc") is Failure.AUTH
    assert classify("401 Unauthorized") is Failure.AUTH
    assert classify("PERMISSION_DENIED") is Failure.AUTH
    assert classify("API key not valid. Please pass a valid API key.") is Failure.AUTH


def test_a_model_that_does_not_exist_switches_immediately():
    # Gemini's live models are all preview names. When one is retired the
    # session is refused before it opens. Retrying it forever would be silence;
    # the backup is the useful answer.
    assert classify(
        "1008 models/gemini-2.0-flash-live-001 is not found for API version "
        "v1beta, or is not supported for bidiGenerateContent"
    ) is Failure.AUTH


def test_a_plain_rate_limit_is_worth_one_retry():
    # Tokens-per-minute clears by itself in under a minute. Burning the switch
    # on it would move the house to the backup for half an hour for nothing.
    assert classify("Rate limit reached for gpt-realtime-2 in organization org-x") is Failure.TRANSIENT


def test_a_dead_socket_is_worth_one_retry():
    assert classify("keepalive ping timeout") is Failure.TRANSIENT
    assert classify("received 1011 (internal error)") is Failure.TRANSIENT
    assert classify("500 Internal Server Error") is Failure.TRANSIENT
    assert classify("realtime receive loop ended — connection closed") is Failure.TRANSIENT


def test_numeric_codes_match_word_boundaries_only():
    # Word boundary test: "500" should not match inside "1500".
    # We test the regex matcher directly because both a broken (bare "500") and
    # correct (\b500\b) matcher would return TRANSIENT (the fallback value).
    # Testing the private _TRANSIENT_CODES is the honest way to verify the fix.
    text = "reached maximum call duration of 1500 seconds"
    assert not any(re.search(pattern, text) for pattern in _TRANSIENT_CODES)

    # 403 in a request ID like "req_1403abc" should not be matched as auth failure.
    # This one can discriminate via return value: broken matcher → AUTH, correct → TRANSIENT.
    assert classify("Unknown error in req_1403abc") is Failure.TRANSIENT


def test_our_own_faults_never_switch_engine():
    # A tool that threw is not the engine's fault. Switching would hide our bug
    # behind a provider change and cost money on the other account.
    assert classify("play_media failed: 500 Internal Server Error") is Failure.APP
    assert classify("search_home: HA_API_URL or COMMS_NYCKEL missing") is Failure.APP
    assert classify("") is Failure.APP


def test_tool_name_alone_must_not_claim_an_error():
    # Tool name alone must not claim an error. Both a broken (bare "remember")
    # and correct (remember with " failed" or ":") matcher would pass this if
    # we only test the return value (both → TRANSIENT, the fallback). Test the
    # regex pattern directly to verify it does NOT match a bare tool name.
    text = "the server remembered your quota was exceeded"
    assert not any(re.search(pattern, text) for pattern in _OURS_PATTERNS)


def test_money_wins_over_rate_limit_when_both_words_appear():
    # OpenAI's quota error is delivered as a 429 and says "rate limit" in some
    # phrasings. The money reading is the one that must win.
    assert classify("Rate limit reached: you exceeded your current quota") is Failure.MONEY


def test_auth_wins_over_transient_when_both_markers_appear():
    # AUTH (permanent, reconfiguration needed) is checked before TRANSIENT
    # (temporary, try again soon). When both patterns appear, AUTH must win.
    assert classify("401 Unauthorized: rate limit exceeded") is Failure.AUTH
