# Voice PE Realtime

**Turn a Home Assistant Voice PE into the voice assistant you actually wanted** — natural speech-to-speech conversation powered by the OpenAI Realtime API, sub-second smart-home control, a wake word trained on *your* household's voices, an assistant that knows who's speaking, remembers what you tell it, and finds you when long-running work is done.

It runs on your Home Assistant box. Wake-word detection, speaker identity, voice recordings, and memory all stay local. The cloud only hears you after you wake it.

## What it feels like

**"What's Grandma's number?"**
Instant recall from your household's long-term memory — answered in under a second. Facts you teach it by voice are built in and stay on your machine; connect your agent and it reaches everything the agent remembers too.

**"Research flight prices to London for October."**
"I'll look into it and report back." It hands the task to your agent, which browses the web in the background for as long as it takes — then the result is **announced out loud in the room you asked from**, or texted to you if you've stepped out. Long-running tasks that find you when they're done.

**Walking into the kitchen: "Set a pasta timer for 9 minutes."**
Nine minutes later: *"Alex, your pasta timer is done"* — spoken personally to whoever set it. A gentle bell follows only if nobody responds. Dismiss with a word or the button.

**It knows who's speaking.**
On-device wake word, local voice recognition. It can greet you by name, keep per-person context, and restrict chosen tools to specific speakers — enforced below the model, so it can't be talked around.

**"Remember that we park at the north lot."**
Teach it standing rules by voice. They persist forever, attributed to whoever said them, and only identified household voices can change them. "Forget that" removes them; "what do you remember?" reads them back.

**And it just converses.**
Speech in, speech out — no STT→LLM→TTS chain, so tone and timing feel human. Interrupt it mid-sentence with "stop". Follow up without repeating the wake word. Lights, climate, media, and shopping lists respond in under a second.

## What people do with it

Marked **†** = needs the optional [agent integration](docs/agent-integration.md). Everything else is built in.

- **"What's the wifi password?"** — say *"remember the wifi password is…"* once, and it's answered instantly forever. Same for the pool gate code, shoe sizes, where the spare key lives.
- **"When's Grandma's birthday?"** † — sub-second recall from your agent's long-term memory.
- **"What did we decide about the fence contractor?"** † — decisions and history, not just facts.
- **"Text Sam we're running ten minutes late."** † — hands covered in flour; your agent sends it through its channels.
- **"Call the pharmacy and ask if my prescription is ready, then tell me what they say."** † — pair it with [OpenClaw](https://openclaw.ai) and the [voice-call plugin](https://github.com/TristanBrotherton/openclaw-voice-call-realtime): your assistant places a real phone call, runs the errand, and the answer is spoken back in the room you asked from.
- **"Research flights to Tokyo in October and text me the three best options."** † — acknowledged now, browsed in the background for as long as it takes, delivered when done.
- **"Add everything for lasagna to the shopping list."** — native Home Assistant list tools, instant. Then *"set a pasta timer"* — dismissed or delivered by name when it's done.
- **A voice for your automations.** † — the announce endpoint accepts any authorized POST, so an agent's scheduled jobs (or any script on your LAN) can speak in the room: *"leave in fifteen minutes for the school run."*

Longer versions, with the how-it-works behind each: **[Stories](docs/stories.md)**.

## Features

- **OpenAI Realtime speech-to-speech** — `gpt-realtime-2` by default, any model id via custom
- **Native Home Assistant control** via the official MCP Server integration — scoped to exactly the entities you expose
- **Custom wake word** — "Hey Leonard" ships as the default (trained by this project); switch to Hey Jarvis / Okay Nabu from a dropdown in HA, or [train your own](docs/features.md#wake-words)
- **Speaker recognition** — local voice-print identification with guided voice enrollment (say *"train my voice"*)
- **Voice-instructed memory** — "remember…" / "forget…" / "what do you remember?", speaker-gated writes
- **Instant memory recall + agent escalation** — sub-second `recall_memory`, deep questions handed to [any agent you integrate](docs/agent-integration.md)
- **Long-running task delegation** — the agent reports back by voice, in the room that asked, via the announce endpoint
- **Voice timers** — personal announcement → grace period → gentle bell, dismissed by button or voice
- **False-wake flagging** — by voice, double-press, or automatically; feeds a [weekly retrain flywheel](docs/features.md#the-retrain-flywheel)
- **Web search** — current info via a single extra OpenAI call (on by default)
- **HA sensors** — current speaker, active timers, wakes today, false wakes today, enrollment active
- **Persona fully yours** — rewrite the instructions; ten OpenAI voices to build on
- **Production hardening** — proactive session refresh before OpenAI's 60-minute cap, reconnect recovery, echo/ghost-turn guards, stop-word authority, turn-liveness watchdogs

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
                              your agent  ←→  recall / delegate / announce (optional)
```

Three parts:

1. **Firmware** ([voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)) — turns the Voice PE into a thin, low-latency audio client. Wake word runs on-device.
2. **Backend add-on** (this repo) — owns the OpenAI Realtime session, Home Assistant tools, speaker identity, timers, and memory.
3. **Agent integration** (optional) — connect any agent for deep recall and long-running task delegation. Everything else works without one.

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
| [Features](docs/features.md) | Wake words, speaker recognition, memory, timers, false-wake flywheel, web search, sensors, persona |
| [Agent Integration](docs/agent-integration.md) | The bridge contracts: recall, escalation, and the announce endpoint — works with any agent |
| [FAQ](docs/faq.md) | Cost, privacy, reverting to stock, Raspberry Pi, languages, and more |

The firmware lives in its own repo: **[TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)**.

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and the official **[Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)** integration.

## License

[MIT](LICENSE). The firmware repo carries the upstream [ESPHome license](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/LICENSE).
