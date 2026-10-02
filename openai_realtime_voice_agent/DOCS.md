# Voice PE Realtime — Add-on Documentation

This add-on runs an **OpenAI Realtime** voice session (default model
`gpt-realtime-2`) and bridges it to Home Assistant control, web search, voice
timers, speaker recognition, and persistent voice-taught memory. It is the
backend half of a two-part project; the front half is custom **firmware for the
Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below).

```
Voice PE device  ──WebSocket──▶  this add-on  ──▶  OpenAI Realtime API
(mic up / speaker down)               │  tools
                                      ▼
                             HA MCP Server (/api/mcp)  → controls your home
```

**Full documentation** lives in the project repository — feature guides, the
complete option reference, agent integration, and FAQ:
<https://github.com/TristanBrotherton/voicepe-realtime/tree/main/docs>.
This page covers setup and day-to-day essentials.

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories**, add:
   `https://github.com/TristanBrotherton/voicepe-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   (There is no prebuilt image — Home Assistant builds it locally the first time,
   which takes a few minutes.)
4. Open the add-on's **Configuration** tab to set it up (next sections).

**One add-on instance serves one Voice PE device.** For multiple devices, run one
instance per device (a local-add-on copy with its own `slug`, `name` and
`websocket_port`) — see the
[Getting Started guide](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/getting-started.md#part-6--multiple-devices).

## 2. Get an OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — Realtime audio and web
   search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.

> **Heads-up on rate limits:** new accounts start at a low tokens-per-minute (TPM)
> tier. Realtime audio is token-heavy, so if you see *"Rate limit reached"* in the
> logs, raise your usage tier in the OpenAI dashboard, or keep
> `max_context_messages` modest (default 12).

## 3. Let it control Home Assistant (MCP)

The assistant controls your home through Home Assistant's **official MCP Server**.

1. In HA: **Settings → Devices & Services → Add Integration → "Model Context
   Protocol Server"** and add it.
2. **Expose the entities** you want voice control over to **Assist**
   (Settings → Voice assistants → *Exposed entities*). The MCP server only offers
   what's exposed.
3. The add-on holds **no Home Assistant key** (`homeassistant_api: false`).
   Every HA call — MCP, states, Music Assistant, its own sensors, the timer
   ring switch — goes to **`ha_api_url`** (raawr-comms, e.g.
   `http://10.10.0.118:3500/kanal/rost/kontoret/api`) with **`comms_nyckel`**.
   Comms holds HA's token and forwards only the calls it has handed out
   (`Backend/Comms/src/kontoret.ts` in the raawr repo); anything else is
   refused there and never reaches HA. Both empty = no house control.

You get a small fixed set of Assist tools (`HassTurnOn`, `HassTurnOff`,
`HassLightSet`, `GetLiveContext`, `GetDateTime`, …). **`GetLiveContext`** is the
"what's the current state?" tool — keep it; it's what answers *"is the light on?"*.

**`mcp_tool_allowlist`** (optional): a comma-separated whitelist of tool names. Leave
blank to expose all, or trim to just what you use, e.g.:
`HassTurnOn,HassTurnOff,HassLightSet,GetLiveContext,GetDateTime`

## 4. Recommended starting settings

**The defaults are the recommended settings** — for a first run you only need the
API key, the MCP integration (section 3), and ideally your language. The
Configuration tab is grouped: **🔑 Basics → 🗣️ Model & voice → 💬 Conversation →
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔍 Debug**, and every
option has plain-language inline help.

| Option | Default | Note |
|---|---|---|
| `openai_model` | `gpt-realtime-2` | newest speech-to-speech model |
| `openai_voice` | `marin` | `marin`/`cedar` are the newest voices |
| `transcription_language` | *(blank)* | set your ISO code (e.g. `nl`): locks the language + logs the user transcript |
| `instructions` | *(English default)* | the system prompt; swap the LANGUAGE line for your language |
| `follow_up_listen_seconds` | `8` | mic stays open this long so you can answer back |
| `follow_up_open_delay_ms` | `700` | echo guard before the follow-up mic opens; lower = snappier but risks ghost turns |
| `wake_open_delay_ms` | `700` | the same echo guard right after the wake chime |
| `vad_eagerness` | `low` | waits longest before deciding you're done talking |
| `playback_prebuffer_ms` | `150` | raise to ~250 if you hear crackle; 0 = play immediately |
| `max_context_messages` | `12` | bounds per-turn token cost |
| `enable_web_search` | `true` | online lookups; set `false` to disable |
| `web_search_model` | `gpt-5.5` | best-quality search model; mini/nano are cheaper |

The legacy `server_vad` turn-detection fields live at the bottom of ⚙️ Advanced and
only appear when you enable **"Show unused optional configuration options"** —
leave them unset unless you have a specific reason.

The **complete option reference** (every option, purpose, default, when to change
it) is in the
[Configuration Reference](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/configuration.md).

## 5. Voice provider & failover

By default, the assistant uses **OpenAI Realtime** (`gpt-realtime-2`). You can
also enable **Google Gemini Live** (`gemini-3.1-flash-live-preview`) as either
the primary or as an automatic fallback when OpenAI runs out of credits, loses
its key, or goes down.

| Option | Default | Purpose |
|---|---|---|
| `voice_provider` | `openai` | Which engine answers (`openai`, `gemini` or `xai`) |
| `voice_provider_backup` | `none` | Automatic failover when primary is unavailable (`none` / `openai` / `gemini` / `xai`) |
| `gemini_api_key` | *(blank)* | Google Gemini API key (required only if using Gemini) |
| `gemini_model` | `models/gemini-3.1-flash-live-preview` | Gemini live model (preview names are retired in turn; you can update it here) |
| `gemini_voice` | `Charon` | Gemini voice name |
| `xai_api_key` | *(blank)* | xAI API key (required only if using `xai`) |
| `xai_model` | `grok-voice-latest` | xAI realtime voice model |
| `xai_voice` | `rex` | xAI voice id (deepest male: `helios`) |
| `provider_cooldown_minutes` | `30` | How long the backup runs before trying the primary again |

**Automatic failover** is off by default (`voice_provider_backup: "none"`). When
enabled, if the primary runs out of money or its key becomes invalid, the backup
takes over automatically on the next turn; after `provider_cooldown_minutes` the
add-on tries the primary again. The backup must be the *other* engine: setting
it to the same engine as `voice_provider` means no failover at all, exactly like
`none`. (There is one API key per engine, so two accounts on the same engine
cannot be configured either.)

**With the default settings the engine and the audio path are unchanged**:
OpenAI primary, no failover, same voice, same latency. Two things do change if
you just update:

- **The assistant now remembers across reconnects.** The cached conversation is
  finally delivered to the engine on reconnect (it never was before, on either
  engine), and the session is re-seeded every hour instead of starting blank. So
  it can follow up on what was said before a drop — and each turn is billed with
  that history as its prefix, up to `max_context_messages` (default 12).
- **A new entity appears**, `sensor.voicepe_<instance>_motor`, showing which
  engine is answering and why.

## 6. Web search

When **`enable_web_search`** is on (**the default**), the assistant gets a `web_search`
tool. When it needs current or general info (weather, news, facts), it calls that
tool; the add-on then makes a **second, server-side OpenAI call** (the Responses API
`web_search` built-in tool, on **`web_search_model`**) and reads a short spoken
answer back.

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `web_search_model` and retry.

## 7. Voice timers

Set, cancel and list timers by voice. On expiry: one personal spoken announcement
(addressed to whoever set the timer), a 20-second grace window (any wake counts as
acknowledged), then a gentle bell only if unacknowledged — silenced by the center
button or "stop".

Setup: set **`timer_ring_entity`** to your device's exposed
`switch.<device>_timer_ringing` entity. For a shared multi-device add-on, use
**`timer_ring_entities`** with `device_id=entity_id` entries separated by commas.
Without either setting, the assistant will say timers are unavailable. Timers survive
the hourly session refresh but not add-on restarts.

## 8. Speaker awareness & voice enrollment

Set `speaker_male_name` / `speaker_female_name` and each wake is tagged with the
likely speaker (pitch heuristic for a one-male-one-female household); enroll voice
prints for true per-person identity. The assistant can address people by name, and
`male_only_tools` (comma-separated tool names) are enforced below the model — a
gated tool politely refuses unless the gated voice was identified. Convenience,
not biometric security. Leave names empty to disable.

**Enrollment**: say *"train my voice"* (any similar phrasing). The paired firmware
pins the mic open (wake/stop detection disarmed, cyan breathing LED, 10-minute cap,
center button aborts) while an automated audio coach guides 25 varied repetitions
of `enrollment_phrase` plus 90 seconds of natural speech. The recording is written
to `/share/voice-enrollment/<name>_<timestamp>.wav` (16 kHz mono PCM) and never
leaves your machine — OpenAI hears nothing during enrollment. Use the recordings
for custom wake-word training and voice prints
(`python3 -m app.build_voiceprint <name> <recording>` → `/share/voice-prints/`).
Options: `enrollment_phrase`, `enrollment_tts_voice`, `wake_sound_entity`
(auto-mutes the wake chime during sessions).

Full guide:
[Speaker recognition & voice enrollment](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/features.md#speaker-recognition--voice-enrollment).

## 9. Voice-instructed memory

Say "remember that..." / "from now on..." and the note becomes a standing
instruction in every future conversation (it takes effect at the next session —
minutes, at most an hour). "Forget about..." removes matching notes; "what do you
remember" reads them back. Notes are stored locally in
`/share/voice-memory/memory.md` (plain markdown — you can edit it by hand), capped
at 60 notes, each attributed to the household member whose voice gave it. Guests
and unidentified voices cannot change memory.

## 10. Agent integration (optional)

**`openclaw_url`**: direct endpoint for an external agent
(`POST {"question", "room"}` → `{"answer"}`). When set, the add-on registers the
`ask_openclaw` escalation tool natively and calls the endpoint directly with a
~2.5-minute timeout — bypassing Home Assistant's hard 60-second MCP request cap
that kills long agent turns. You also get **`recall_memory`**: the bridge answers
`{"recall": "<query>"}` with `{"matches": [...]}` — instant, deterministic recall
(contacts, dates, preferences) with the full agent turn as fallback.

**`announce_port` + `announce_token`** (set both): a LAN route *back to the
device*. `POST http://<ha-host>:<announce_port>/announce` with
`Authorization: Bearer <announce_token>` and body `{"message": "..."}` speaks the
message aloud through the device's guarded TTS lane — this is what lets a
delegated task report back by voice minutes later. Returns 503 when no device is
connected, so callers can fall back to a text channel. Generate a long random
token; the add-on runs on the host network, so the token is the lock.

The integration is agent-agnostic — any agent behind a small bridge works. Full
contracts and examples:
[Agent Integration](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/agent-integration.md).

## 11. False-wake flagging & HA sensors

Every wake's opening audio is archived locally (auto-pruned, newest 500). Flag a
false trigger by saying *"that was a false alarm"*, **double-pressing the center
button**, or automatically when a wake is silenced without speech. Labeled
captures become hard negatives for wake-word retraining — see the
[retrain flywheel](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/features.md#the-retrain-flywheel).

Set **`instance_name`** (e.g. `kitchen`) to publish
`sensor.voicepe_kitchen_speaker`, `_active_timers`, `_wakes_today`,
`_false_wakes_today` and `binary_sensor.voicepe_kitchen_enrollment_active` for
dashboards and automations.

## 12. Reading the logs

The add-on log shows each turn: `🗣️ user:` (when transcription language is set),
`🤖 assistant:` (the reply text), `📞 phase ->` (device state), tool calls, and
`🔌 …reconnecting` / `✅ reconnected` on a connection recovery. View it on the add-on
**Log** tab.

## Known limitations

- **A brief reconnect about once an hour.** OpenAI limits a realtime session to
  60 minutes. The add-on refreshes proactively during a quiet moment, so you'll
  rarely notice it, but a reconnect can occasionally cause a ~1–2 second pause.
- **Timers don't survive add-on restarts** (they do survive the hourly session
  refresh).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

**Using "stop":** say "stop" (or press the center button) to interrupt the assistant
*while it's speaking* — during a reply, or during the short listening window right
after one. It has no effect before the assistant has started answering (there's
nothing to stop yet).

## Firmware (Home Assistant Voice PE only)

This add-on expects the custom **Voice PE firmware** that turns the device into a
thin client (it streams mic audio here and plays the reply). That firmware:

- is **specific to the Home Assistant Voice PE** hardware (ESP32-S3 + XMOS), and
- lives in its own **public** repository:
  **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**.

You flash it once via a tiny per-device "stub" in ESPHome Builder; after that,
firmware updates are **one click** — no tokens, no copy-pasting. The full
from-scratch guide (flashing + adopting + first conversation) is the
[Getting Started guide](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/getting-started.md).

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
