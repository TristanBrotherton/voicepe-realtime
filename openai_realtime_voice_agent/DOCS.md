# OpenAI Realtime 2 Voice Agent — Documentation

This add-on runs an **OpenAI `gpt-realtime-2`** voice session and bridges it to Home
Assistant control and web search. It is the backend half of a two-part project; the
front half is custom **firmware for the Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below).

```
Voice PE device  ──WebSocket──▶  this add-on  ──▶  OpenAI Realtime API
(mic up / speaker down)               │  tools
                                       ▼
                              HA MCP Server (/api/mcp)  → controls your home
```

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories**, add:
   `https://github.com/TristanBrotherton/voicepe-realtime-backend`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   (There is no prebuilt image — Home Assistant builds it locally the first time,
   which takes a few minutes.)
4. Open the add-on's **Configuration** tab to set it up (next sections).

## 2. Get an OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — `gpt-realtime-2` (audio)
   and web search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.

> **Heads-up on rate limits:** new accounts start at a low tokens-per-minute (TPM)
> tier. `gpt-realtime-2` audio is token-heavy, so if you see *"Rate limit reached"*
> in the logs, raise your usage tier in the OpenAI dashboard, or keep
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

**`openclaw_url`** (optional): direct endpoint for an external agent escalation
tool (`POST {"question": ...}` → `{"answer": ...}`). If you expose an
"ask my agent" script through HA's MCP server, be aware Home Assistant core
hard-caps every MCP request at **60 seconds** — long agent turns (deep memory
recall, multi-step tasks) get killed mid-answer. Setting `openclaw_url` makes
the add-on register the `ask_openclaw` tool natively and call the endpoint
directly with a ~2.5-minute timeout; an MCP tool of the same name is skipped
so the model sees exactly one. Leave blank to use whatever your MCP server
exposes, unchanged.

With `openclaw_url` set you also get **`recall_memory`**: the bridge answers
`{"recall": "<query>"}` with matching lines from the agent's memory files —
instant, deterministic recall (contacts, dates, preferences) with the full
agent turn as fallback. See the bridge README for the endpoint contract.

**`announce_port` + `announce_token`** (optional, set both): a LAN route *back to
the device* for an external agent. `POST http://<ha-host>:<announce_port>/announce`
with `Authorization: Bearer <announce_token>` and body `{"message": "..."}` speaks
the message aloud through the device's guarded TTS lane (the same path timers
use — the assistant can't hear itself and reply). This is what lets a delegated
task ("research a vacation") report back by voice minutes later: the agent
replies instantly that it will follow up, works in the background, then posts
its summary here. Returns 503 when no device is connected, so callers can fall
back to a text channel. Generate a long random token; the add-on runs on the
host network, so the token is the lock.

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
| `wake_open_delay_ms` | `700` | the same echo guard right after the wake chime; lower = snappier wake but risks a ghost turn |
| `vad_eagerness` | `low` | waits longest before deciding you're done talking |
| `playback_prebuffer_ms` | `150` | raise to ~250 if you hear crackle; 0 = play immediately |
| `max_context_messages` | `12` | bounds per-turn token cost |
| `enable_web_search` | `true` | online lookups; set `false` to disable |
| `web_search_model` | `gpt-5.5` | best-quality search model; mini/nano are cheaper |

The legacy `server_vad` turn-detection fields live at the bottom of ⚙️ Advanced and
only appear when you enable **"Show unused optional configuration options"** —
leave them unset unless you have a specific reason.

## 5. Web search

When **`enable_web_search`** is on (**the default**), the assistant gets a `web_search` tool. When
it needs current or general info (weather, news, facts), it calls that tool; the
add-on then makes a **second, server-side OpenAI call** (the Responses API
`web_search` built-in tool, on **`web_search_model`**) and reads a short spoken
answer back.

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `web_search_model` and retry.

## 6. Options reference & tuning

Every option has a description on the **Configuration** tab. The ones worth knowing:

- **Model / voice / transcription model** are dropdowns with a **`custom`** entry +
  a `*_custom` text field if you want a value not in the list.
- **`transcription_language`** turns the side-channel transcript on. With it set you
  get `🗣️ user: …` lines in the add-on log (handy for debugging); it does **not**
  change what the model understands — the main model hears your audio natively.
- **`follow_up_open_delay_ms` / `playback_prebuffer_ms`** default to `700` / `150`
  — an echo guard and jitter cushion. Lowering them makes the device feel
  snappier, but below ~700 ms open delay the reply's own speaker tail can leak
  into the fresh follow-up mic and become a ghost turn (the assistant "answers
  nobody" or repeats itself); raise the prebuffer if you hear crackle at the
  start of replies.

## 7. Reading the logs

The add-on log shows each turn: `🗣️ user:` (when transcription language is set),
`🤖 assistant:` (the reply text), `📞 phase ->` (device state), tool calls, and
`🔌 …reconnecting` / `✅ reconnected` on a connection recovery. View it on the add-on
**Log** tab.

## Known limitations

- **No voice timers or alarms yet.** Setting a timer by voice isn't supported (the
  official Home Assistant timer intent isn't wired up). Everything else — lights,
  switches, scenes, climate, and questions — works.
- **A brief reconnect about once an hour.** OpenAI limits a realtime session to
  60 minutes. The add-on refreshes proactively during a quiet moment, so you'll
  rarely notice it, but a reconnect can occasionally cause a ~1–2 second pause.
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

You flash it once via a tiny per-device "stub" in ESPHome Builder; after that, firmware
updates are **one click** — no tokens, no copy-pasting. That repo has the full
from-scratch guide (flashing + adopting the device in ESPHome Builder).

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.


## Speaker awareness

Set `speaker_male_name` / `speaker_female_name` and each wake is tagged with the
likely speaker (pitch heuristic for a one-male-one-female household). The
assistant can address people by name or sir/ma'am, and `male_only_tools`
(comma-separated tool names) are enforced below the model — a gated tool
politely refuses unless the male voice was identified. Convenience, not
biometric security. Leave names empty to disable.

## Voice enrollment (guided, on-device)

Say "teach me my voice" (any similar phrasing). The paired firmware pins the
mic open (wake/stop detection disarmed, cyan breathing LED, 10-minute cap, top
button aborts) while an automated audio coach guides 25 varied repetitions of
`enrollment_phrase` plus 90 seconds of natural speech. The recording is written
to `/share/voice-enrollment/<name>_<timestamp>.wav` (16 kHz mono PCM) and never
leaves your machine — OpenAI hears nothing during enrollment. Use the
recordings to train a custom microWakeWord model on real household voices (see
the firmware repo) and, in future, per-person voice-print identification.
Options: `enrollment_phrase`, `enrollment_tts_voice` (any /v1/audio/speech voice).


## Voice-instructed memory

Say "remember that..." / "from now on..." and the note becomes a standing
instruction in every future conversation (it takes effect at the next session —
minutes, at most an hour). "Forget about..." removes matching notes; "what do
you remember" reads them back. Notes are stored locally in
`/share/voice-memory/memory.md` (plain markdown — you can edit it by hand),
capped at 60 notes, each attributed to the household member whose voice gave
it. Guests and unidentified voices cannot change memory.
