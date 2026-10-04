# Voice PE Realtime

**Turn a Home Assistant Voice PE into the voice assistant you actually wanted** — natural speech-to-speech conversation powered by the OpenAI Realtime API, smart-home control with per-turn latency you can see in Home Assistant, a wake word you can retrain on *your* household's voices, an assistant that knows who's speaking, remembers what you tell it, and finds you when long-running work is done. Built on **[Home Assistant](https://www.home-assistant.io) / [OpenClaw](https://openclaw.ai)**: Home Assistant runs your home, OpenClaw is its memory, its hands, and its phone.

It runs on your Home Assistant box. Wake-word detection runs on the device; speaker identification, voice prints and any recordings stay on your box. OpenAI hears you only after a wake, and receives what the conversation needs: your instructions, your memory notes and tool results — [exactly what leaves your network](docs/faq.md#what-about-privacy--what-leaves-my-network).

## What it feels like

**"What's Grandma's number?"**
Recall from your household's long-term memory, without waiting for a full agent turn. Facts you teach it by voice are stored on your machine and become part of its instructions; connect [OpenClaw](https://openclaw.ai) and it reaches everything your agent remembers too.

**"Research flight prices to London for October."**
"I'll look into it and report back." It hands the task to [OpenClaw](https://openclaw.ai), which browses the web in the background for as long as it takes — then the result is **announced out loud in the room you asked from**, or texted to you if you've stepped out. Long-running tasks that find you when they're done.

**Walking into the kitchen: "Set a pasta timer for 9 minutes."**
Nine minutes later: *"Alex, your pasta timer is done"* — spoken personally to whoever set it. A gentle bell follows only if nobody responds. Dismiss with a word or the button.

**It knows who's speaking.**
On-device wake word, local voice recognition. It can greet you by name, keep per-person context, and restrict chosen tools to specific speakers — enforced below the model, so it can't be talked around. Voice matching is a convenience, not a lock: a similar voice can pass.

**"Remember that we park at the north lot."**
Teach it standing rules by voice. They persist, attributed to whoever said them, and changes need a recognized household voice. "Forget that" removes them; "what do you remember?" reads them back.

**And it just converses.**
Speech in, speech out — no STT→LLM→TTS chain, so tone and timing feel human. Interrupt it mid-sentence with "stop". Follow up without repeating the wake word.

**"Unlock the front door."**
*"Do you want me to unlock the front door?"* Locks, garage doors, gates and alarm panels need a spoken yes — enforced in the add-on, not left to the model's judgement.

**And you can see how fast it is.**
Every turn's timeline — wake to mic open, end of speech to first reply audio, each tool's duration — is published to Home Assistant, so you can measure your own setup instead of trusting a number in a README.

## What people do with it

Marked **†** = needs the optional [agent integration](docs/agent-integration.md) — built for [OpenClaw](https://openclaw.ai), works with any agent. Everything else is built in.

- **"What's the wifi password?"** — say *"remember the wifi password is…"* once, and it's answered from then on. Same for the pool gate code, shoe sizes, where the spare key lives.
- **"When's Grandma's birthday?"** † — recall from OpenClaw's long-term memory (a text search, no agent turn).
- **"What did we decide about the fence contractor?"** † — decisions and history, not just facts.
- **"Text Sam we're running ten minutes late."** † — hands covered in flour; OpenClaw sends it through any of its channels (iMessage, Telegram, WhatsApp, …).
- **"Call the pharmacy and ask if my prescription is ready, then tell me what they say."** † — pair it with [OpenClaw](https://openclaw.ai) and my [openclaw-voice-call-realtime](https://github.com/TristanBrotherton/openclaw-voice-call-realtime) plugin, which gives your assistant a real phone: it places the call, runs the errand, and the answer is spoken back in the room you asked from.
- **"Research flights to Tokyo in October and text me the three best options."** † — acknowledged now, browsed in the background for as long as it takes, delivered when done.
- **"Add everything for lasagna to the shopping list."** — native Home Assistant list tools. Then *"set a pasta timer"* — dismissed or delivered by name when it's done.
- **A voice for your automations.** † — the announce endpoint accepts any authorized POST, so OpenClaw's scheduled jobs (or any script on your LAN) can speak in the room: *"leave in fifteen minutes for the school run."*

Longer versions, with the how-it-works behind each: **[Stories](docs/stories.md)**.

## Features

- **OpenAI Realtime speech-to-speech** — `gpt-realtime-2` by default, any model id via custom
- **Native Home Assistant control** via the official MCP Server integration — scoped to exactly the entities you expose
- **Custom wake word** — "Hey Leonard" ships as the default (trained by this project); switch to Hey Jarvis / Okay Nabu from a dropdown in HA, or [train your own](docs/features.md#wake-words)
- **Speaker recognition** — local voice-print identification with guided voice enrollment (say *"train my voice"*)
- **Voice-instructed memory** — "remember…" / "forget…" / "what do you remember?", speaker-gated writes
- **[OpenClaw](https://openclaw.ai) integration** — `recall_memory` searches your agent's memory directly, deep questions escalated to a full agent turn; a [ready-to-run bridge](examples/openclaw-bridge/) ships in this repo ([contracts are agent-agnostic](docs/agent-integration.md))
- **Long-running task delegation** — OpenClaw reports back by voice, in the room that asked, via the announce endpoint
- **Voice timers** — personal announcement → grace period → gentle bell, dismissed by button or voice
- **False-wake flagging** — by voice or double-press, tied to the exact device and wake; feeds a [gated wake-word learning loop](docs/wake-word-learning.md) that never ships a model worse than the one you have
- **Web search** — current info via a single extra OpenAI call (on by default)
- **HA sensors** — per-turn latency (with p50/p90), the active wake-word model and threshold, current speaker, active timers, wakes and false wakes today, enrollment active
- **Persona fully yours** — rewrite the instructions; ten OpenAI voices to build on
- **Safety** — device token for the WebSocket, spoken-yes confirmations for locks/garage/gates/alarm, privacy controls for what is stored (nothing beyond counters by default)
- **Production hardening** — proactive session refresh before OpenAI's 60-minute cap, reconnect recovery that replays the request you were making, spoken error messages, echo/ghost-turn guards, stop-word authority, turn-liveness watchdogs

## Architecture at a glance

```
Home Assistant Voice PE           Home Assistant (your box)              Cloud
┌─────────────────────────┐   WS   ┌──────────────────────────┐   WS   ┌──────────────┐
│ custom ESPHome firmware │ ─────▶ │ this add-on              │ ─────▶ │ OpenAI       │
│ wake word + XMOS DSP    │ 16 kHz │ (session, tools, memory, │ 24 kHz │ Realtime API │
│ thin audio client       │ ◀───── │  speaker ID, timers)     │ ◀───── │              │
└─────────────────────────┘        └───────────┬──────────────┘        └──────────────┘
                                               │ tools
                                               ▼
                              HA MCP Server → controls your home
                              OpenClaw    ←→  recall / delegate / announce (optional)
```

Three parts:

1. **Firmware** ([voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)) — turns the Voice PE into a thin, low-latency audio client. Wake word runs on-device.
2. **Backend add-on** (this repo) — owns the OpenAI Realtime session, Home Assistant tools, speaker identity, timers, and memory.
3. **[OpenClaw](https://openclaw.ai) integration** (optional) — deep recall, messaging, calls, and long-running task delegation ([agent-agnostic contracts](docs/agent-integration.md)). Everything else works without it.

## Quick start

1. **Install the add-on**: Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/TristanBrotherton/voicepe-realtime` → install **OpenAI Realtime 2 Voice Agent**. Set your OpenAI API key.
2. **Give it your home**: add Home Assistant's **MCP Server** integration and expose the entities you want voice-controlled to Assist.
3. **Flash the firmware**: adopt your Voice PE in ESPHome Builder and paste in the [device stub](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.dhcp.yaml) from the firmware repo. First flash over USB, updates OTA.
4. Say **"Hey Leonard"** and ask for a light.

Full walkthrough (~30–45 minutes from zero): **[Getting Started](docs/getting-started.md)**.

## Documentation

| Guide | What's in it |
|---|---|
| [Getting Started](docs/getting-started.md) | Prerequisites, flashing, add-on install, first conversation, multi-device |
| [Stories](docs/stories.md) | What households actually do with it — and which feature makes each one work |
| [Configuration Reference](docs/configuration.md) | Every add-on option and firmware substitution — purpose, default, when to change it |
| [Features](docs/features.md) | Wake words, speaker recognition, memory, timers, false-wake flagging, latency, confirmations, web search, sensors, persona |
| [Wake-word learning](docs/wake-word-learning.md) | How false wakes become better models without shipping regressions: capture modes, labels, the release gate, shadow and canary |
| [Demo](demo/README.md) | A scripted, reproducible demo with timing — runs against a simulated device, no household needed |
| [Agent Integration](docs/agent-integration.md) | The bridge contracts: recall, escalation, and the announce endpoint — works with any agent |
| [FAQ](docs/faq.md) | Cost, privacy (what leaves your network), reverting to stock, Raspberry Pi, languages, and more |
| [Contributing](CONTRIBUTING.md) | PRs welcome — small, tested, explained |

The firmware lives in its own repo: **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**.

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.

## License

[MIT](LICENSE). The firmware repo carries the upstream [ESPHome license](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/LICENSE).
