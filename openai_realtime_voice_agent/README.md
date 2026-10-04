# OpenAI Realtime 2 Voice Agent

Talk to your home with **OpenAI's Realtime** speech-to-speech models. This Home
Assistant add-on runs the realtime voice session and bridges it to Home Assistant
device control (via the official **MCP Server** integration), **web search**,
**voice timers**, **speaker recognition** with guided voice enrollment,
**voice-taught memory**, and optional **agent integration** for deep recall and
long-running task delegation with voice report-back.

It is the cloud-facing half of a two-part project. The other half is custom
**firmware for the Home Assistant Voice PE** device, which streams microphone audio
to this add-on and plays the reply back. **This add-on is designed for that Voice PE
firmware** (a thin client that talks a small WebSocket protocol); it is not a
drop-in for the stock HA voice pipeline.

> **You need both halves.** This add-on does nothing without the **Voice PE firmware**
> that streams audio to it →
> **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**.

## What it does

- **Natural voice conversations** (speech in → speech out, no separate STT/TTS
  step) — interrupt mid-sentence, follow up without re-waking.
- **Controls Home Assistant** through the official HA *MCP Server* integration —
  lights, switches, scenes, climate, etc., scoped to the entities you expose to
  Assist.
- **Knows who's speaking** — local voice-print identification, guided enrollment
  by voice ("train my voice"), speaker-gated tools.
- **Remembers what you teach it** — "remember that…" notes persist across
  sessions, stored on your Home Assistant host and included in the assistant's
  instructions; changes need a recognized household voice.
- **Voice timers** — personal spoken announcement, then a gentle bell only if
  unacknowledged.
- **Web search** (on by default) — weather, news, facts via a single OpenAI call.
- **Agent-ready** — connect any external agent for memory recall and
  background tasks that announce their results on the device that asked.
- **Measurable and careful** — per-turn latency sensors, spoken-yes
  confirmations for locks, garage doors, gates and alarms, a device token, and
  privacy controls for what is stored (counters and metadata by default).
- **Tunable from the UI** — model, voice, speed, turn detection, follow-up window,
  language, and more; every option has inline help.

## Quick start

1. Add this repository to Home Assistant (Settings → Add-ons → Add-on Store → ⋮ →
   **Repositories**): `https://github.com/TristanBrotherton/voicepe-realtime`
2. Install **OpenAI Realtime 2 Voice Agent** and open its **Configuration** tab.
3. Paste your **OpenAI API key**, install the HA **MCP Server** integration, expose
   a few entities to Assist, and **Start** the add-on.
4. Flash the **Voice PE firmware** from
   **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**
   (one-click adopt-and-update in ESPHome Builder).

Setup steps are on the **Documentation** tab (`DOCS.md`); the full guides live at
**<https://github.com/TristanBrotherton/voicepe-realtime>**.

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
