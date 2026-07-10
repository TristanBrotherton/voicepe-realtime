# OpenAI Realtime 2 Voice Agent

Talk to your home with **OpenAI's `gpt-realtime-2`** speech-to-speech model. This
Home Assistant add-on runs the realtime voice session and bridges it to Home
Assistant device control (via the official **MCP Server** integration) and optional
**web search** — so you can say *"alexa, turn off the bedroom lamp"* or *"what's the
weather tomorrow?"* and get a spoken answer.

It is the cloud-facing half of a two-part project. The other half is custom
**firmware for the Home Assistant Voice PE** device, which streams microphone audio
to this add-on and plays the reply back. **This add-on is designed for that Voice PE
firmware** (a thin client that talks a small WebSocket protocol); it is not a
drop-in for the stock HA voice pipeline.

> **You need both halves.** This add-on does nothing without the **Voice PE firmware**
> that streams audio to it →
> **[TristanBrotherton/home-assistant-voice-pe](https://github.com/TristanBrotherton/home-assistant-voice-pe)**.

## What it does

- **Natural voice conversations** with `gpt-realtime-2` (speech in → speech out, no
  separate STT/TTS step).
- **Controls Home Assistant** through the official HA *MCP Server* integration —
  lights, switches, scenes, climate, etc., scoped to the entities you expose to
  Assist.
- **Web search** (on by default) — the assistant can look things up online
  (weather, news, facts) via a single OpenAI call. Turn it off if you don't want it.
- **Tunable from the UI** — model, voice, speaking speed, turn detection, a
  post-reply follow-up window, transcription language, and more. Every option has
  inline help on the Configuration tab.

## Quick start

1. Add this repository to Home Assistant (Settings → Add-ons → Add-on Store → ⋮ →
   **Repositories**): `https://github.com/TristanBrotherton/ha-openai-realtime`
2. Install **OpenAI Realtime 2 Voice Agent** and open its **Configuration** tab.
3. Paste your **OpenAI API key**, install the HA **MCP Server** integration, expose
   a few entities to Assist, and **Start** the add-on.
4. Flash the **Voice PE firmware** from
   **[TristanBrotherton/home-assistant-voice-pe](https://github.com/TristanBrotherton/home-assistant-voice-pe)**
   (one-click adopt-and-update in ESPHome Builder; full steps in that repo).

Full step-by-step instructions are on the **Documentation** tab (`DOCS.md`).

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.
