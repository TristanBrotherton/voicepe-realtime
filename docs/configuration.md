# Configuration Reference

Two places hold configuration:

- **The add-on** — the Configuration tab in Home Assistant. Options are grouped:
  🔑 Basics → 🗣️ Model & voice → 💬 Conversation → 🌐 Web search → 🎚️ Audio →
  🏠 Home Assistant → ⚙️ Advanced → 🔐 Privacy & safety → 🔍 Debug. Every option
  has inline help.
- **The firmware** — substitutions in your per-device stub in ESPHome Builder.

> The add-on UI renders every text option as a single-line input. For long text like
> `instructions`, use the Configuration tab's **⋮ → Edit in YAML** for a real editor.

> The `*_custom` fields, the legacy `server_vad` fields and the tuning options
> marked *(hidden)* below stay hidden until you toggle **"Show unused optional
> configuration options"** at the bottom of the tab. Unset hidden options use
> the defaults shown.

## 🔑 Basics

| Option | Default | Purpose / when to change |
|---|---|---|
| `openai_api_key` | *(empty)* | Your OpenAI key (`sk-...`), created at platform.openai.com with billing enabled. Everything — listening, thinking, speaking, web search — runs on this key. **Required.** |
| `instructions` | English voice-tuned prompt | The system prompt: personality, language, house rules. Write it like you'd brief a person. See [Persona & voices](features.md#persona--voices) for what it can and can't change. |
| `transcription_language` | *(empty)* | Two-letter ISO code (`en`, `nl`, `de`, …). Setting it pins the language and enables the user transcript, which is written to the log as `🗣️ user:` lines only when `log_transcripts` is on. Empty = auto-detect, no user transcript. |

## 🗣️ Model & voice

| Option | Default | Purpose / when to change |
|---|---|---|
| `openai_model` | `gpt-realtime-2` | The speech-to-speech model. Choices: `gpt-realtime-2` (newest, smartest), `gpt-realtime-1.5`, `gpt-realtime-mini` (cheaper, less capable), `gpt-realtime`, or `custom`. |
| `openai_model_custom` | *(hidden)* | Any valid Realtime model id, used when `openai_model` is `custom`. Expert escape hatch. |
| `openai_voice` | `marin` | The voice it speaks with. `marin`/`cedar` are the newest and most natural; also `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`. Restart the add-on after changing — a running conversation keeps its voice. |
| `openai_voice_custom` | *(hidden)* | Any valid OpenAI voice name, used when `openai_voice` is `custom`. |
| `openai_speed` | `1.0` | Speaking pace, `0.25`–`1.5`. Changes pace only, not the words. |
| `max_output_tokens` | `0` | Caps answer length in tokens (≈ 0.75 words each). `0` = no cap. Set ~`1024` if it rambles; too low cuts answers off mid-sentence. |

## 💬 Conversation

| Option | Default | Purpose / when to change |
|---|---|---|
| `follow_up_listen_seconds` | `8` | How long the mic stays open after each reply, so you can keep talking without the wake word. `0` disables follow-ups. |
| `follow_up_open_delay_ms` | `700` | Echo guard: pause between the end of a reply and the mic re-opening, so the speaker's tail can't become a ghost question. Lower (300–500) is snappier but risks the assistant answering its own echo — raise it back if it "answers nobody" or repeats itself. |
| `wake_open_delay_ms` | `700` | The same echo guard after the wake chime, before the mic opens. Lower for a snappier wake; raise if a wake sometimes triggers an answer to nothing. |
| `vad_eagerness` | `low` | How quickly it decides you're done talking. `low` waits patiently (best if you pause mid-sentence), `high` answers faster but may cut you off, `auto` lets OpenAI decide. |
| `phase_idle_debounce_ms` | `1500` | How long the assistant must stay silent before the device counts the answer as finished. Bridges pauses between sentences so the LED and "stop" keep working through long answers. Raise if the device flips to idle mid-answer. |
| `slow_tool_ack` | `true` | When a slow lookup (web search, your agent) is still running after about a second, the device says "One moment." once. Fast smart-home commands never trigger it. |
| `speak_errors` | `true` | If a request you started fails (rate limit, failed response), the device says so instead of silently going idle; it falls back to the error chime if the message can't be produced. |
| `tts_style` | *(empty)* | Speaking-style instructions for timer messages, announcements, acknowledgements and the enrollment coach, e.g. `Calm and friendly.` Empty = neutral. |
| `slow_tools` | *(hidden)* `web_search,ask_openclaw` | Tools that get the acknowledgement. Any tool whose measured median exceeds 1.2 s qualifies automatically. |
| `tts_voice` | *(hidden)* | Voice for those messages. Default: the assistant's voice when the speech endpoint supports it, otherwise `alloy`. |

## 🌐 Web search

| Option | Default | Purpose / when to change |
|---|---|---|
| `enable_web_search` | `true` | Online lookups (weather, news, facts). Each lookup is one extra OpenAI call on your key (a few cents) and adds the search's own time (shown per call in the latency sensor). Set `false` to disable. |
| `web_search_model` | `gpt-5.5` | The model that searches and summarises. `gpt-5.5` is best quality; `gpt-5.4`, `gpt-5`, mini and nano variants are cheaper but miss more. |
| `web_search_model_custom` | *(hidden)* | Any model supporting OpenAI's `web_search` tool, when set to `custom`. |
| `web_search_timeout_s` | *(hidden)* `20` | A search that takes longer is stopped and the assistant says so. |

## 🎚️ Audio

| Option | Default | Purpose / when to change |
|---|---|---|
| `playback_prebuffer_ms` | `150` | How much of the answer to buffer before playing — absorbs Wi-Fi jitter (the start-of-reply crackle) at the cost of that much reply latency. Raise to ~250 if you hear crackle; `0` = play immediately. |
| `output_lead_buffer_ms` | `0` | Holds the start of each reply in the add-on and sends it as one burst, giving the device a playback cushion. Helps if replies start with a click or stutter; costs about this much latency on the first word. `0` = off; try `400`. The latency sensor shows the effect. |
| `noise_reduction` | `off` | Extra input filtering before OpenAI. Usually leave `off` — the device's XMOS chip already filters. Try `near_field` (talking close) or `far_field` (across the room) if it mishears in noise. |

## 🏠 Home Assistant & speakers

| Option | Default | Purpose / when to change |
|---|---|---|
| `speaker_male_name` | *(empty)* | Name to use when a male voice is detected. Leave both name fields empty to disable speaker detection. |
| `speaker_female_name` | *(empty)* | Name to use when a female voice is detected. |
| `male_only_tools` | *(empty)* | Comma-separated tool names that only execute for the male voice. Enforced below the model — it can't be talked around. Convenience gating, not biometric security. |
| `wake_sound_entity` | *(empty)* | The device's wake-chime switch entity. When set, the chime is auto-muted during enrollment sessions so the coach's instructions stay audible. |
| `timer_ring_entity` | *(empty)* | The exposed `switch.<device>_timer_ringing` entity for the physical timer bell. Empty = voice timers unavailable (the assistant will say so). Timer speech always returns to the device that created the timer. |
| `timer_ring_entities` | *(empty)* | For one add-on serving several devices: each device's own bell, as `device_id=entity_id` pairs, e.g. `kitchen=switch.kitchen_timer_ringing,office=switch.office_timer_ringing`. |
| `instance_name` | *(empty)* | Sensor prefix, e.g. `kitchen` → `sensor.voicepe_kitchen_*`. Also sent to your agent as the `room` (next to the asking `device_id`). Empty = `device`. |
| `enrollment_phrase` | `hey leonard` | The wake phrase the enrollment coach asks you to repeat. **Set this to the wake word you actually use / plan to train.** |
| `enrollment_tts_voice` | `fable` | The voice of the enrollment coach (any OpenAI `/v1/audio/speech` voice). |
| `ha_mcp_url` | *(empty)* | Leave empty (recommended): uses HA's built-in MCP Server integration. Only set a URL if you run the separate ha-mcp add-on. |
| `longlived_token` | *(empty)* | Leave empty (recommended): the add-on uses its own supervisor permission. Only paste a long-lived token (HA profile → Security) if startup logs a 401/403 on `/core/api/mcp`. |
| `mcp_tool_allowlist` | *(empty)* | Comma-separated whitelist of MCP tool names; empty = all. The built-in server's set is already small; mainly useful with ha-mcp (80+ tools) to keep sessions fast and cheap. |
| `openclaw_url` | *(empty)* | Direct endpoint of your agent bridge. Enables the `ask_openclaw` escalation tool (called directly, ~2.5-minute budget, bypassing HA MCP's 60 s cap) and the `recall_memory` text search. Contract in [Agent Integration](agent-integration.md). |
| `announce_port` | `0` | Port for the announce endpoint — a LAN route back to the device so an agent can speak in the room. Enabled only when **both** this and `announce_token` are set. |
| `announce_token` | *(empty)* | Bearer token for the announce endpoint. The add-on runs on the host network, so the token is the lock — generate a long random one. |
| `mcp_persistent_session` | *(hidden)* `true` | Reuse one Home Assistant tool session instead of opening a new one per command. |

## ⚙️ Advanced

| Option | Default | Purpose / when to change |
|---|---|---|
| `websocket_port` | `8080` | The port Voice PE devices connect to. Must match each device's `va_url` in its firmware. One add-on instance accepts multiple devices on this port; change it only on a port clash. `8081` is used by dev builds. |
| `session_reuse_timeout_seconds` | `300` | If the device reconnects within this window (Wi-Fi blip, add-on restart), the conversation resumes where it left off. `0` = always start fresh. |
| `max_context_messages` | `12` | How many recent exchanges the session keeps. More = better in-conversation memory, but every answer re-bills the whole history — long chats get expensive and can hit rate limits. `0` = unlimited. |
| `transcription_model` | `gpt-4o-transcribe` | Transcribes your speech when `transcription_language` is set (written to the log only with `log_transcripts`). Does **not** affect understanding — the main model hears your audio natively. Also: `gpt-live-transcribe` (low-latency live transcription), `gpt-transcribe` (completed audio), `gpt-realtime-whisper`, `gpt-4o-mini-transcribe`, `whisper-1`. |
| `transcription_model_custom` | *(hidden)* | Custom transcription model id. |
| `turn_detection_type` | *(unset)* | Leave unset: `semantic_vad` (understands when your sentence is finished) is the hardwired default. `server_vad` is the legacy silence-timer method, kept as an escape hatch, tuned by the three fields below. |
| `vad_threshold` | *(unset)* | server_vad only: loudness to count as speech, 0–1 (default 0.5). Higher = fewer false triggers from background noise. |
| `vad_prefix_padding_ms` | *(unset)* | server_vad only: audio kept from just before speech was detected so your first word isn't clipped (default 300). |
| `vad_silence_duration_ms` | *(unset)* | server_vad only: how long a silence ends your turn (default 800). Raise if you get cut off while pausing. |

A device's `device_id` routes its session; it is not a credential. Set a
`device_token` (below) and never expose `websocket_port` outside your home
network.

## 🔐 Privacy & safety

| Option | Default | Purpose / when to change |
|---|---|---|
| `wake_capture` | `auto` | What is kept about wakes, on this host only: `off` = nothing; `metadata` = counters and wake metadata (time, device, model, cutoff, window, your flags), no audio; `audio` = metadata plus a short clip after each wake for reviewing false wakes; `auto` = `audio` if `enable_recording` is on, else `metadata`. See [Wake-word learning](wake-word-learning.md). |
| `trigger_capture` | `false` | Also keep the ~1.5 s **before** each wake (what the model fired on). Needs `wake_capture: audio` **and** the device's "Share wake trigger audio" switch. |
| `log_transcripts` | `false` | Write what you and the assistant said into the add-on log. Off by default — transcripts are personal data. |
| `device_token` | *(empty)* | Shared secret each Voice PE must present (the firmware's `va_token`). Empty = any host on your network can connect; the add-on logs a warning. |
| `device_auth` | `auto` | `auto` = require the token once one is set; `permissive` = accept devices without it but log them (use while reflashing); `enforce` = require it; `off` = never check. `enforce`/`permissive` without a token fall back to `off` with a warning. |
| `confirm_actions` | `lock,garage,gate,door,alarm` | Actions that run only after a spoken yes, enforced in the add-on: `lock` (unlocking), `garage`, `gate`, `door` (opening those covers), `alarm` (any alarm-panel action). Empty = no confirmations. |
| `confirm_tools` | *(hidden)* | Extra tool names (e.g. an exposed script) that always need confirmation. |
| `device_allowlist` | *(hidden)* | Comma-separated IPs or networks (e.g. `192.0.2.10/32`) allowed to connect at all. Works without reflashing. |
| `guest_mode_entity` | *(hidden)* | An `input_boolean` or switch; while it is on, nothing about wakes is stored. |
| `wake_capture_ttl_days` | *(hidden)* `30` | Unreviewed wake clips are deleted after this many days. |
| `wake_label_ttl_days` | *(hidden)* `180` | Clips you flagged as false wakes are deleted after this many days. |
| `false_wake_flag_window_s` | *(hidden)* `30` | A spoken "that was a false alarm" labels this device's wake only within this many seconds. |

**Turning on the device token without locking anyone out:** set `device_token`
and `device_auth: permissive`, reflash each Voice PE with the same value as
`va_token` in its stub, check the log shows every device connecting with a
valid token, then set `device_auth` back to `auto`.

## 🔍 Debug

| Option | Default | Purpose / when to change |
|---|---|---|
| `enable_recording` | `false` | Saves mic and speaker audio to files inside the add-on, for troubleshooting only. With `wake_capture: auto` it also keeps short wake clips. Leave off normally. |

---

## Firmware substitutions

Set these in your per-device stub in ESPHome Builder (the stub overrides the
firmware's defaults). The secrets (`wifi_ssid`, `wifi_password`, `ota_password`,
`api_key`) are passed as substitutions because a remote package can't use
`!secret` directly.

| Substitution | Default | Purpose / when to change |
|---|---|---|
| `name` | `home-assistant-voice` | The device's ESPHome name. **Keep it stable** across re-flashes of an adopted device. |
| `friendly_name` | `Home Assistant Voice` | Display name in Home Assistant. |
| `wifi_ssid` / `wifi_password` | *(from secrets)* | Your Wi-Fi credentials. |
| `ota_password` | *(from secrets)* | Protects over-the-air flashes. Use the one ESPHome generated at adopt time (or pick one on a fresh flash). |
| `api_key` | *(from secrets)* | ESPHome Noise/API encryption key — 32 random bytes, base64 (`openssl rand -base64 32`). Not an HA token, not your OpenAI key. |
| `va_url` | `ws://homeassistant.local:8080/` | WebSocket endpoint of the backend add-on. Change if your HA host has a different name/IP or the add-on uses another port, e.g. `ws://192.168.1.x:8082/`. |
| `va_token` | *(empty)* | The add-on's `device_token`, sent as an `Authorization` header. Empty = no token (for add-ons without one). |
| `va_client_ref` | `main` | Git ref the `va_client` component is fetched from. Pin it to the same tag or commit as the firmware package for reproducible builds. |
| `wake_word_model` | `models/hey_leonard.json`, pinned to a commit | The microWakeWord model URL. Point it at your own trained model, or at a stock model. Runtime switching between the built-in options needs no reflash — use the "Wake word" dropdown in HA. |
| `wake_model_name` / `wake_model_sha256` / `wake_model_window` | the shipped model's | Identity of the custom model, reported to Home Assistant and the add-on with every wake. Update them with `wake_word_model`. |
| `default_wake_word` | `Hey Leonard` | Which entry of the HA "Wake word" dropdown is selected on first boot (`Hey Leonard`, `Hey Jarvis`, `Okay Nabu`). |
| `wake_cutoff_slight` / `wake_cutoff_moderate` / `wake_cutoff_very` | `auto` | The custom model's three sensitivity tiers. `auto`: **Slightly** = the model's own calibrated cutoff (from its JSON), **Moderately** / **Very** = that minus `wake_cutoff_moderate_delta` / `wake_cutoff_very_delta`. A number (`round(p × 255)`; lower = more sensitive) overrides a tier — only for your own calibration. |
| `wake_cutoff_moderate_delta` / `wake_cutoff_very_delta` | `18` / `33` | Tier steps derived from the model's evaluation manifest (more sensitive tiers sit outside the validated false-accept budget). |
| `default_wake_sensitivity` | `Slightly sensitive` | The sensitivity a fresh device starts with. |
| `hidden_ssid` | `false` | Set `"true"` if your Wi-Fi SSID is hidden. |
| `timer_finished_sound_file` | `sounds/gentle_timer.flac` (this repo) | The timer bell — a gentle two-tone bell (~-14 dBFS) replacing the more intense stock ring. Point at any FLAC/MP3 URL to change it (the other `*_sound_file` substitutions swap the stock chimes the same way). |
| `static_ip` / `gateway` / `subnet` / `dns1` / `dns2` | *(DHCP)* | Only with the static-IP stub (`esphome-builder.static-ip.yaml`) — pins a fixed LAN IP. |

Firmware-side controls in Home Assistant, not in YAML:

- **"Wake word" dropdown** — Hey Leonard / Hey Jarvis / Okay Nabu, switched at
  runtime, no reflash.
- **"Wake word sensitivity" select** — Slightly (calibrated) / Moderately / Very
  sensitive, for every wake word.
- **"Share wake trigger audio" switch** — off by default; the device's half of
  the `trigger_capture` consent.
- **"Wake word operating point"** diagnostic — model, SHA-256 prefix, cutoff,
  window and tier currently in use.

The optional [`packages/wake-word-shadow.yaml`](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/packages/wake-word-shadow.yaml)
runs a candidate model log-only next to the live one (`shadow_wake_word_model`,
`shadow_wake_word`); see [Wake-word learning](wake-word-learning.md#6-deployment-shadow-canary-fleet).
