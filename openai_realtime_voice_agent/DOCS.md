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

**One add-on instance serves several Voice PE devices**, each with its own
OpenAI session. Run separate instances only when rooms need different
configuration — see the
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
3. In the add-on, leave **`ha_mcp_url`** **blank** — it then uses the built-in
   endpoint (`http://supervisor/core/api/mcp`) with the add-on's own token. Leave
   **`longlived_token`** blank too, unless startup logs a 401/403 on
   `/core/api/mcp` (then paste a HA long-lived token there).

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
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔐 Privacy & safety →
🔍 Debug**, and every option has plain-language inline help.

| Option | Default | Note |
|---|---|---|
| `openai_model` | `gpt-realtime-2` | newest speech-to-speech model |
| `openai_voice` | `marin` | `marin`/`cedar` are the newest voices |
| `transcription_language` | *(blank)* | set your ISO code (e.g. `nl`): locks the language (the transcript is logged only with `log_transcripts`) |
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

### Privacy & safety defaults

- **What is stored** (`wake_capture`, default `auto`): counters and wake
  metadata only, no audio, unless you opt in. `log_transcripts` is off, so what
  you say is not written to the log.
- **Confirmations** (`confirm_actions`): unlocking, opening garage doors, gates
  and doors, and any alarm-panel action run only after a spoken yes, enforced
  in the add-on.
- **Device token** (`device_token`, recommended): without one, any host on your
  network can open a session. Set it with `device_auth: permissive`, flash each
  device with the same `va_token`, then switch `device_auth` back to `auto`.
- **What OpenAI receives**: audio after a wake, your instructions and memory
  notes, tool results, and the recognized speaker's name — see the
  [privacy FAQ](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/faq.md#what-about-privacy--what-leaves-my-network).

## 5. Web search

When **`enable_web_search`** is on (**the default**), the assistant gets a `web_search`
tool. When it needs current or general info (weather, news, facts), it calls that
tool; the add-on then makes a **second, server-side OpenAI call** (the Responses API
`web_search` built-in tool, on **`web_search_model`**) and reads a short spoken
answer back.

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds the search's own time (the device shows "thinking", and says "One
  moment." if it runs past about a second). Searches stop after 20 s
  (`web_search_timeout_s`).
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `web_search_model` and retry.

## 6. Voice timers

Set, cancel and list timers by voice. On expiry: one personal spoken announcement
(addressed to whoever set the timer), a 20-second grace window (any wake counts as
acknowledged), then a gentle bell only if unacknowledged — silenced by the center
button or "stop".

Setup: set **`timer_ring_entity`** to your device's exposed
`switch.<device>_timer_ringing` entity. For a shared multi-device add-on, use
**`timer_ring_entities`** with `device_id=entity_id` entries separated by commas.
Without either setting, the assistant will say timers are unavailable. Timers survive
the hourly session refresh but not add-on restarts.

## 7. Speaker awareness & voice enrollment

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

## 8. Voice-instructed memory

Say "remember that..." / "from now on..." and the note becomes a standing
instruction in every future conversation (it takes effect at the next session —
minutes, at most an hour). "Forget about..." removes matching notes; "what do you
remember" reads them back. Notes are stored in `/share/voice-memory/memory.md`
on this host (plain markdown — you can edit it by hand), capped at 60 notes,
each attributed to the household member whose voice gave it, and sent to OpenAI
as part of the assistant's instructions in every session. Only a recognized
household voice can change them — a convenience check, not a lock (with the
pitch heuristic a similar voice passes).

## 9. Agent integration (optional)

**`openclaw_url`**: direct endpoint for an external agent
(`POST {"question", "room", "device_id"}` → `{"answer"}`). When set, the add-on registers the
`ask_openclaw` escalation tool natively and calls the endpoint directly with a
~2.5-minute timeout — bypassing Home Assistant's hard 60-second MCP request cap
that kills long agent turns. You also get **`recall_memory`**: the bridge answers
`{"recall": "<query>"}` with `{"matches": [...]}` — a deterministic text search
(contacts, dates, preferences), no agent turn, with the full agent turn as
fallback.

**`announce_port` + `announce_token`** (set both): a LAN route *back to the
device*. `POST http://<ha-host>:<announce_port>/announce` with
`Authorization: Bearer <announce_token>` and body `{"message": "..."}` (plus
`"device_id"` to pick the device that asked) speaks the message aloud through
the device's guarded TTS lane — this is what lets a delegated task report back
by voice minutes later. Returns 503 when that device (or any device) is not
connected, so callers can fall back to a text channel. Generate a long random
token; the add-on runs on the host network, so the token is the lock.

The integration is agent-agnostic — any agent behind a small bridge works. Full
contracts and examples:
[Agent Integration](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/agent-integration.md).

## 10. False-wake flagging & HA sensors

Flag a false trigger by **double-pressing the center button**, pressing it
during the wake before any reply, or saying *"that was a false alarm"*; the flag
labels that device's own wake. By default only counters and wake metadata are
stored (no audio). `wake_capture: audio` keeps short clips on this host for
review, with automatic expiry. Labeled clips can become hard negatives for
wake-word retraining — see the
[wake-word learning loop](https://github.com/TristanBrotherton/voicepe-realtime/blob/main/docs/wake-word-learning.md).

Set **`instance_name`** (e.g. `kitchen`) to publish
`sensor.voicepe_kitchen_latency` (the last turn's timeline, with rolling
p50/p90), `_wake_word` (the active model, cutoff, window and sensitivity),
`_speaker`, `_active_timers`, `_wakes_today`, `_false_wakes_today` (both with
per-device counts, kept across restarts), `_openai_cost_today`, `_voice_prints`
and `binary_sensor.voicepe_kitchen_enrollment_active` for dashboards and
automations.

## 11. Reading the logs

The add-on log shows each turn: one `⏱️ turn` line with its timings (no words),
`📞 phase ->` (device state), tool calls, and `🔌 …reconnecting` / `✅ reconnected`
on a connection recovery. With `log_transcripts` on it also shows `🗣️ user:` and
`🤖 assistant:` lines. View it on the add-on **Log** tab.

## Known limitations

- **A brief reconnect about once an hour.** OpenAI limits a realtime session to
  60 minutes. The add-on refreshes proactively during a quiet moment, so you'll
  rarely notice it, but a reconnect can occasionally cause a ~1–2 second pause.
- **Timers don't survive add-on restarts** (they do survive the hourly session
  refresh).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.
- **The default "Hey Leonard" model** was trained with the maintainer's household
  voices. Recall for other voices and from across the room has not been
  measured; if it is hard to wake, try "Moderately sensitive" and flag false
  wakes so you can see the trade-off.

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
