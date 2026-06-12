<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="OpenAI Realtime 2 Voice Agent — a stylised Voice PE" width="160"/>
</p>

# OpenAI Realtime 2 Voice Agent (Home Assistant Voice PE)

> [!IMPORTANT]
> **This is 1 of 2 repos — you need both halves.** This repo is the **backend add-on**
> (the voice "brain"). It needs the custom Voice PE **firmware** to connect to it — the
> stock Home Assistant voice pipeline won't talk to this add-on. You must set up both:
> - 🧠 **Backend add-on** (this repo) — runs inside Home Assistant
> - 🔌 **Device firmware** → **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)** (flashed onto the Voice PE)
>
> 📖 New here? The full **[INSTALL guide](https://github.com/xandervanerven/home-assistant-voice-pe/blob/main/INSTALL.md)** walks through both halves, step by step.

A Home Assistant **add-on** that turns a [Voice PE](https://www.home-assistant.io/voice-pe/)
device into a low-latency voice assistant built on **OpenAI's Realtime API**
(`gpt-realtime-2`). The device streams microphone audio to this add-on over a
plain WebSocket; the add-on runs the Realtime speech-to-speech session and
controls Home Assistant through the official
**[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)**
integration. STT, TTS and the LLM all run in the Realtime session — there is no
Home Assistant `voice_assistant` pipeline on the audio path.

> Fork of **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**,
> retargeted at `gpt-realtime-2`, the official HA MCP Server, optional web search,
> and the **Voice PE thin-client firmware** (a separate repo — see below).

## Repository layout

- **[`openai_realtime_voice_agent/`](openai_realtime_voice_agent/)** — the Home
  Assistant add-on (Python / [Pipecat](https://github.com/pipecat-ai/pipecat)).
  This is the only thing you install.
  - [`DOCS.md`](openai_realtime_voice_agent/DOCS.md) — full setup: OpenAI key, the
    Home Assistant MCP connection, recommended settings, web search, all options.
  - [`CHANGELOG.md`](openai_realtime_voice_agent/CHANGELOG.md) — what changed per version.

The **device firmware** lives in its own repository —
**[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)**
(a custom `va_client` ESPHome component, specific to the Voice PE hardware).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/xandervanerven/ha-openai-realtime`.
2. Install **OpenAI Realtime 2 Voice Agent**. It ships with no prebuilt `image:`,
   so Home Assistant builds it locally on first install (a few minutes on a Pi).
3. Configure the add-on and flash the companion firmware — see
   [`openai_realtime_voice_agent/DOCS.md`](openai_realtime_voice_agent/DOCS.md).

(An optional GitHub Actions workflow can publish container images to ghcr.io; it
isn't needed for a normal local-build install.)

## How it works

```
Voice PE (ESP32-S3)  ──WS, 16 kHz PCM up──▶   this add-on    ──▶  OpenAI Realtime API
  va_client firmware  ◀──── 24 kHz PCM down──  (Pipecat)          (gpt-realtime-2)
                                                   │ tools
                                                   ▼
                                         Home Assistant MCP Server
```

The device does wake-word detection and XMOS audio cleanup locally and is a thin
client. Interrupt a reply with the **"stop"** word or the center button.

## Credits

- Forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**.
- Built on **[Pipecat](https://github.com/pipecat-ai/pipecat)**.
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE).
