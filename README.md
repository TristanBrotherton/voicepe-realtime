# Voice PE Realtime — backend add-on

**An OpenAI Realtime voice assistant platform for the Home Assistant Voice PE**:
speech-to-speech with native HA control, speaker awareness (sir/ma'am, per-voice
tool gating), and on-device guided voice enrollment for training custom wake
words and (roadmap) per-person voice-print identity. Pairs with the
[Voice PE Realtime firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware).
Originally derived from upstream work credited below; now developed independently here.


# OpenAI Realtime 2 Voice Agent (Home Assistant Voice PE) (TristanBrotherton fork)

> [!NOTE]
> Maintained fork (upstream credit: xandervanerven's ha-openai-realtime),
> tracked so installs don't depend on upstream availability. Currently identical to
> upstream v0.6.0. Pairs with the firmware fork
> [TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)
> (custom wake word + volume fixes). Planned divergence: multi-device support —
> upstream's backend accepts a single Voice PE per add-on instance (the pipecat
> WebsocketServerTransport drops the previous client when a new one connects), so
> today each device needs its own instance on its own port.

## Fork features: speaker context + voice enrollment

**Speaker context** (`speaker_male_name` / `speaker_female_name`): a pure-numpy
pitch classifier tags each wake with the likely speaker for a one-male-one-female
household. The verdict is injected as session context (the assistant can say
"sir"/"ma'am" and use names) and enforces `male_only_tools` below the model.
Honest limits: it distinguishes voice TYPES, not people — same-gender households
should leave it off or wait for the voice-print upgrade below. Leave both names
empty to disable entirely.

**Voice enrollment** (`enrollment_phrase`, `enrollment_tts_voice`): say "teach
me your voice" and the paired firmware enters a true enrollment mode (mic pinned
open, wake/stop models disarmed, cyan LED, 10-minute cap, top button to abort)
while an automated audio coach guides 25 varied wake-phrase repetitions plus
90 s of natural speech. Recordings land in
`/share/voice-enrollment/<name>_<timestamp>.wav` (16 kHz mono) — they are
PERSONAL DATA, stay on your box, and the add-on never touches them beyond
writing the file. Sessions are auto-tagged from the speaker verdict when
available; otherwise the assistant asks for a first name (also how you enroll
guests or same-gender households). Uses: training a custom microWakeWord model
on real household voices, and (roadmap) per-person voice-print speaker ID,
which replaces the pitch heuristic and works for any household composition.


> [!IMPORTANT]
> **This is 1 of 2 repos — you need both halves.** This repo is the **backend add-on**
> (the voice "brain"). It needs the custom Voice PE **firmware** to connect to it — the
> stock Home Assistant voice pipeline won't talk to this add-on. You must set up both:
> - 🧠 **Backend add-on** (this repo) — runs inside Home Assistant
> - 🔌 **Device firmware** → **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)** (flashed onto the Voice PE)
>
> 📖 New here? The full **[INSTALL guide](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/INSTALL.md)** walks through both halves, step by step.

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
**[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**
(a custom `va_client` ESPHome component, specific to the Voice PE hardware).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store → ⋮ → Repositories**
   and add `https://github.com/TristanBrotherton/voicepe-realtime-backend`.
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

## Known limitations

- **No voice timers or alarms yet** — every other Assist action (lights, switches,
  scenes, climate) and online questions work.
- **A brief reconnect about once an hour** (OpenAI's 60-minute session cap; the
  add-on refreshes proactively during a quiet moment, so it rarely interrupts).
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

## Credits

- Forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)**.
- Built on **[Pipecat](https://github.com/pipecat-ai/pipecat)**.
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)** (a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)**, Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).

## License

MIT — see [LICENSE](LICENSE).
