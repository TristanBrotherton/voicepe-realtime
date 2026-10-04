#!/usr/bin/with-contenv bashio
set -e

# --- 🔑 Basics ---
OPENAI_API_KEY=$(bashio::config 'openai_api_key')
SPEAKER_MALE_NAME=$(bashio::config 'speaker_male_name')
WAKE_SOUND_ENTITY=$(bashio::config 'wake_sound_entity')
TIMER_RING_ENTITY=$(bashio::config 'timer_ring_entity')
TIMER_RING_ENTITIES=$(bashio::config 'timer_ring_entities')
INSTANCE_NAME=$(bashio::config 'instance_name')
ENROLLMENT_PHRASE=$(bashio::config 'enrollment_phrase')
ENROLLMENT_TTS_VOICE=$(bashio::config 'enrollment_tts_voice')
SPEAKER_FEMALE_NAME=$(bashio::config 'speaker_female_name')
MALE_ONLY_TOOLS=$(bashio::config 'male_only_tools')
INSTRUCTIONS=$(bashio::config 'instructions')
TRANSCRIPTION_LANGUAGE=$(bashio::config 'transcription_language')

# --- 🗣️ Model & voice ---
OPENAI_MODEL=$(bashio::config 'openai_model')
OPENAI_VOICE=$(bashio::config 'openai_voice')
OPENAI_SPEED=$(bashio::config 'openai_speed')
MAX_OUTPUT_TOKENS=$(bashio::config 'max_output_tokens')

# --- 💬 Conversation ---
FOLLOW_UP_LISTEN_SECONDS=$(bashio::config 'follow_up_listen_seconds')
FOLLOW_UP_OPEN_DELAY_MS=$(bashio::config 'follow_up_open_delay_ms')
WAKE_OPEN_DELAY_MS=$(bashio::config 'wake_open_delay_ms')
VAD_EAGERNESS=$(bashio::config 'vad_eagerness')
PHASE_IDLE_DEBOUNCE_MS=$(bashio::config 'phase_idle_debounce_ms')

# --- 🌐 Web search ---
ENABLE_WEB_SEARCH=$(bashio::config 'enable_web_search')
WEB_SEARCH_MODEL=$(bashio::config 'web_search_model')

# --- 🎚️ Audio ---
PLAYBACK_PREBUFFER_MS=$(bashio::config 'playback_prebuffer_ms')
OUTPUT_LEAD_BUFFER_MS=$(bashio::config 'output_lead_buffer_ms')
NOISE_REDUCTION=$(bashio::config 'noise_reduction')

# --- 🏠 Home Assistant ---
HA_MCP_URL=$(bashio::config 'ha_mcp_url')
LONGLIVED_TOKEN=$(bashio::config 'longlived_token')
MCP_TOOL_ALLOWLIST=$(bashio::config 'mcp_tool_allowlist')
OPENCLAW_URL=$(bashio::config 'openclaw_url')
ANNOUNCE_PORT=$(bashio::config 'announce_port')
ANNOUNCE_TOKEN=$(bashio::config 'announce_token')

# --- ⚙️ Advanced ---
WEBSOCKET_PORT=$(bashio::config 'websocket_port')
SESSION_REUSE_TIMEOUT_SECONDS=$(bashio::config 'session_reuse_timeout_seconds')
MAX_CONTEXT_MESSAGES=$(bashio::config 'max_context_messages')
TRANSCRIPTION_MODEL=$(bashio::config 'transcription_model')

# --- 🔐 Privacy & safety ---
WAKE_CAPTURE=$(bashio::config 'wake_capture')
TRIGGER_CAPTURE=$(bashio::config 'trigger_capture')
LOG_TRANSCRIPTS=$(bashio::config 'log_transcripts')
DEVICE_TOKEN=$(bashio::config 'device_token')
DEVICE_AUTH=$(bashio::config 'device_auth')
CONFIRM_ACTIONS=$(bashio::config 'confirm_actions')
SLOW_TOOL_ACK=$(bashio::config 'slow_tool_ack')
SPEAK_ERRORS=$(bashio::config 'speak_errors')
TTS_STYLE=$(bashio::config 'tts_style')

# --- 🔍 Debug ---
ENABLE_RECORDING=$(bashio::config 'enable_recording')

# Validate required configuration
if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "OPENAI_API_KEY is required but not set"
    exit 1
fi

# Export environment variables
export OPENAI_API_KEY
export SPEAKER_MALE_NAME
export WAKE_SOUND_ENTITY
export TIMER_RING_ENTITY
export TIMER_RING_ENTITIES
export INSTANCE_NAME
export ENROLLMENT_PHRASE
export ENROLLMENT_TTS_VOICE
export SPEAKER_FEMALE_NAME
export MALE_ONLY_TOOLS
export INSTRUCTIONS
export TRANSCRIPTION_LANGUAGE
export OPENAI_MODEL
export OPENAI_VOICE
export OPENAI_SPEED
export MAX_OUTPUT_TOKENS
export FOLLOW_UP_LISTEN_SECONDS
export FOLLOW_UP_OPEN_DELAY_MS
export WAKE_OPEN_DELAY_MS
export VAD_EAGERNESS
export PHASE_IDLE_DEBOUNCE_MS
export ENABLE_WEB_SEARCH
export WEB_SEARCH_MODEL
export PLAYBACK_PREBUFFER_MS
export OUTPUT_LEAD_BUFFER_MS
export NOISE_REDUCTION
export LONGLIVED_TOKEN
export MCP_TOOL_ALLOWLIST
export OPENCLAW_URL
export ANNOUNCE_PORT
export ANNOUNCE_TOKEN
export WEBSOCKET_PORT
export SESSION_REUSE_TIMEOUT_SECONDS
export MAX_CONTEXT_MESSAGES
export TRANSCRIPTION_MODEL
export ENABLE_RECORDING
export WAKE_CAPTURE
export TRIGGER_CAPTURE
export LOG_TRANSCRIPTS
export DEVICE_TOKEN
export DEVICE_AUTH
export CONFIRM_ACTIONS
export SLOW_TOOL_ACK
export SPEAK_ERRORS
export TTS_STYLE

# Optional privacy/safety/tool tuning (hidden options): export only when set,
# because bashio prints the literal string "null" for unset optionals.
for option in wake_capture_ttl_days wake_label_ttl_days false_wake_flag_window_s \
              guest_mode_entity device_allowlist confirm_tools slow_tools tts_voice \
              mcp_persistent_session web_search_timeout_s; do
    if bashio::config.has_value "${option}"; then
        value=$(bashio::config "${option}")
        case "${option}" in
            wake_capture_ttl_days) export WAKE_CAPTURE_TTL_DAYS="${value}" ;;
            wake_label_ttl_days) export WAKE_LABEL_TTL_DAYS="${value}" ;;
            false_wake_flag_window_s) export FALSE_WAKE_FLAG_WINDOW_S="${value}" ;;
            guest_mode_entity) export GUEST_MODE_ENTITY="${value}" ;;
            device_allowlist) export DEVICE_ALLOWLIST="${value}" ;;
            confirm_tools) export CONFIRM_TOOLS="${value}" ;;
            slow_tools) export SLOW_TOOLS="${value}" ;;
            tts_voice) export TTS_VOICE="${value}" ;;
            mcp_persistent_session) export MCP_PERSISTENT_SESSION="${value}" ;;
            web_search_timeout_s) export WEB_SEARCH_TIMEOUT_S="${value}" ;;
        esac
    fi
done

# The *_custom escape hatches (🗣️/🌐/⚙️) are optional WITHOUT defaults —
# bashio::config prints "null" for unset optionals, and main.py's
# _resolve_choice would treat that literal string as a real custom value.
# Only export when actually set.
if bashio::config.has_value 'openai_model_custom'; then
    OPENAI_MODEL_CUSTOM=$(bashio::config 'openai_model_custom')
    export OPENAI_MODEL_CUSTOM
fi
if bashio::config.has_value 'openai_voice_custom'; then
    OPENAI_VOICE_CUSTOM=$(bashio::config 'openai_voice_custom')
    export OPENAI_VOICE_CUSTOM
fi
if bashio::config.has_value 'web_search_model_custom'; then
    WEB_SEARCH_MODEL_CUSTOM=$(bashio::config 'web_search_model_custom')
    export WEB_SEARCH_MODEL_CUSTOM
fi
if bashio::config.has_value 'transcription_model_custom'; then
    TRANSCRIPTION_MODEL_CUSTOM=$(bashio::config 'transcription_model_custom')
    export TRANSCRIPTION_MODEL_CUSTOM
fi

# Legacy server_vad escape hatch (⚙️ Advanced, optional WITHOUT defaults).
# bashio::config prints the string "null" for unset optional keys, which would
# crash main.py's float()/int() parsing — so only export when actually set.
# Unset = main.py's hardwired defaults (semantic_vad; 0.5/300/800 if server_vad
# is ever selected).
if bashio::config.has_value 'turn_detection_type'; then
    TURN_DETECTION_TYPE=$(bashio::config 'turn_detection_type')
    export TURN_DETECTION_TYPE
fi
if bashio::config.has_value 'vad_threshold'; then
    VAD_THRESHOLD=$(bashio::config 'vad_threshold')
    export VAD_THRESHOLD
fi
if bashio::config.has_value 'vad_prefix_padding_ms'; then
    VAD_PREFIX_PADDING_MS=$(bashio::config 'vad_prefix_padding_ms')
    export VAD_PREFIX_PADDING_MS
fi
if bashio::config.has_value 'vad_silence_duration_ms'; then
    VAD_SILENCE_DURATION_MS=$(bashio::config 'vad_silence_duration_ms')
    export VAD_SILENCE_DURATION_MS
fi

# Removed options (v0.4.29) — no longer exported; main.py env defaults take
# over: SEMANTIC_VAD_CREATE_RESPONSE=true, ENABLE_DISCONNECT_TOOL=false,
# INTERRUPT_RESPONSE=false, DEVICE_INPUT_SAMPLE_RATE=16000.

# Export HA_MCP_URL if set (empty string means use default in main.py)
if [ -n "$HA_MCP_URL" ]; then
    export HA_MCP_URL
fi

# SUPERVISOR_TOKEN is automatically provided by Home Assistant when homeassistant_api: true

# Start the application
export PYTHONUNBUFFERED=1
cd /app
exec python3 -m app.main
