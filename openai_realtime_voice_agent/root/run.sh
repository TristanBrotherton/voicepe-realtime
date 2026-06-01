#!/usr/bin/with-contenv bashio
set -e

# Get configuration
OPENAI_API_KEY=$(bashio::config 'openai_api_key')
OPENAI_MODEL=$(bashio::config 'openai_model')
OPENAI_VOICE=$(bashio::config 'openai_voice')
WEBSOCKET_PORT=$(bashio::config 'websocket_port')
DEVICE_INPUT_SAMPLE_RATE=$(bashio::config 'device_input_sample_rate')
HA_MCP_URL=$(bashio::config 'ha_mcp_url')
LONGLIVED_TOKEN=$(bashio::config 'longlived_token')
MCP_TOOL_ALLOWLIST=$(bashio::config 'mcp_tool_allowlist')

# Get turn detection settings
VAD_THRESHOLD=$(bashio::config 'vad_threshold')
VAD_PREFIX_PADDING_MS=$(bashio::config 'vad_prefix_padding_ms')
VAD_SILENCE_DURATION_MS=$(bashio::config 'vad_silence_duration_ms')
TURN_DETECTION_TYPE=$(bashio::config 'turn_detection_type')
VAD_EAGERNESS=$(bashio::config 'vad_eagerness')
SEMANTIC_VAD_CREATE_RESPONSE=$(bashio::config 'semantic_vad_create_response')
ENABLE_DISCONNECT_TOOL=$(bashio::config 'enable_disconnect_tool')
INTERRUPT_RESPONSE=$(bashio::config 'interrupt_response')
TRANSCRIPTION_LANGUAGE=$(bashio::config 'transcription_language')
TRANSCRIPTION_MODEL=$(bashio::config 'transcription_model')
PHASE_IDLE_DEBOUNCE_MS=$(bashio::config 'phase_idle_debounce_ms')
FOLLOW_UP_LISTEN_SECONDS=$(bashio::config 'follow_up_listen_seconds')
FOLLOW_UP_OPEN_DELAY_MS=$(bashio::config 'follow_up_open_delay_ms')
PLAYBACK_PREBUFFER_MS=$(bashio::config 'playback_prebuffer_ms')

# Get instructions
INSTRUCTIONS=$(bashio::config 'instructions')

# Get session management settings
SESSION_REUSE_TIMEOUT_SECONDS=$(bashio::config 'session_reuse_timeout_seconds')
MAX_CONTEXT_MESSAGES=$(bashio::config 'max_context_messages')

# Get web search settings
ENABLE_WEB_SEARCH=$(bashio::config 'enable_web_search')
WEB_SEARCH_MODEL=$(bashio::config 'web_search_model')

# Get audio recording setting
ENABLE_RECORDING=$(bashio::config 'enable_recording')

# Validate required configuration
if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "OPENAI_API_KEY is required but not set"
    exit 1
fi

# Export environment variables
export OPENAI_API_KEY
export OPENAI_MODEL
export OPENAI_VOICE
export WEBSOCKET_PORT
export DEVICE_INPUT_SAMPLE_RATE
export LONGLIVED_TOKEN
export MCP_TOOL_ALLOWLIST

# Export turn detection settings
export VAD_THRESHOLD
export VAD_PREFIX_PADDING_MS
export VAD_SILENCE_DURATION_MS
export TURN_DETECTION_TYPE
export VAD_EAGERNESS
export SEMANTIC_VAD_CREATE_RESPONSE
export ENABLE_DISCONNECT_TOOL
export INTERRUPT_RESPONSE
export TRANSCRIPTION_LANGUAGE
export TRANSCRIPTION_MODEL
export PHASE_IDLE_DEBOUNCE_MS
export FOLLOW_UP_LISTEN_SECONDS
export FOLLOW_UP_OPEN_DELAY_MS
export PLAYBACK_PREBUFFER_MS

# Export instructions
export INSTRUCTIONS

# Export session management settings
export SESSION_REUSE_TIMEOUT_SECONDS
export MAX_CONTEXT_MESSAGES

# Export web search settings
export ENABLE_WEB_SEARCH
export WEB_SEARCH_MODEL

# Export audio recording setting
export ENABLE_RECORDING

# Export HA_MCP_URL if set (empty string means use default in main.py)
if [ -n "$HA_MCP_URL" ]; then
    export HA_MCP_URL
fi

# SUPERVISOR_TOKEN is automatically provided by Home Assistant when homeassistant_api: true

# Start the application
export PYTHONUNBUFFERED=1
exec python3 -m app.main

