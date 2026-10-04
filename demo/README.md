# Demo

A reproducible demo of Voice PE Realtime in about three minutes, with every
latency figure measured on the spot rather than quoted.

It runs against a **fictitious household**: a separate Home Assistant instance
with the built-in [demo integration](https://www.home-assistant.io/integrations/demo/)
(fake lights, locks, covers and media players). Never record a demo in a real
home: memory notes, timers, speaker names and device states all end up on
screen.

There are two ways to run it:

- **Scripted** — `simulated_device.py` plays a Voice PE over the real protocol
  with synthesized speech and measures every step. Reproducible, no hardware.
- **Live** — a real Voice PE on the same demo instance, with the dashboard card
  on screen. The script is how you rehearse it.

## Setup

1. A Home Assistant instance for the demo with `demo:` in its
   `configuration.yaml`, the MCP Server integration, and the demo entities
   exposed to Assist.
2. The add-on installed there, with privacy settings that keep the demo clean:
   `wake_capture: off`, `log_transcripts: false`, `enable_recording: false`,
   `instance_name: demo`. Start with an empty `/share/voice-memory/memory.md`.
3. The dashboard card from [`dashboard.yaml`](dashboard.yaml): the last turn's
   timeline, rolling p50/p90, the wake-word operating point and the counters.
4. Prompts for the scripted run (synthetic voices, git-ignored):

   ```
   pip install numpy websockets          # or use the add-on's environment
   OPENAI_API_KEY=sk-... python3 demo/make_prompts.py
   ```

## The script (≈3 minutes)

| # | Say | Shows |
|---|---|---|
| 1 | "Hey Leonard, turn on the kitchen lights." | Home Assistant control; the card fills in that turn's timeline |
| 2 | "Remember that the spare key is under the blue pot." | Voice-taught memory. Say it honestly: *stored on this box, and sent to OpenAI as part of the prompt* |
| 3 | "Set a pasta timer for one minute." | Timers; the personal announcement plays later in the demo |
| 4 | "What's the weather in Amsterdam tomorrow?" | A slow lookup: one short "One moment.", then the answer, with the tool's duration on the card |
| 5 | "Unlock the front door." → "Yes." | A risky action needs a spoken yes, enforced in the add-on, answered without repeating the wake word |
| 6 | "Tell me a long story about a lighthouse keeper." → "Stop." | Interrupting a reply |
| 7 | (Optional, pre-recorded) delegate a research task and hear it announced back | Agent delegation depends on private infrastructure and takes minutes — never run it live |

Steps 1–6 are [`scenarios.json`](scenarios.json). Step 1's wake word only
exists with a real device; the scripted run starts each turn the way the
center button does.

## Run it

```
python3 demo/simulated_device.py --url ws://<demo-ha-host>:8080/ --json results.json
python3 demo/simulated_device.py --url ws://<demo-ha-host>:8080/ --repeat 20   # rehearsal
python3 demo/simulated_device.py --dry-run     # tests the script itself; no add-on needed
```

Add `--token <device_token>` if the add-on requires one. For each step the
script prints:

| Column | Meaning |
|---|---|
| speech end→audio | Last voiced frame sent → first reply audio received, measured at the client. Includes the server's end-of-turn decision, the model, tools and the network; excludes the device's playback buffer. |
| reply | Seconds of reply audio received |
| bursts | Separate stretches of reply audio; a slow lookup's acknowledgement makes it 2 or more |
| stop→quiet | "Stop" sent → last reply audio received |
| turn | Wake → idle |

A failed step reports its outcome — `error`, `timeout`, `disconnected` — and
no timings, and the script exits non-zero. The add-on's own per-turn timeline
(the dashboard card, or `sensor.voicepe_demo_latency`) splits the same turn
into the server's end-of-turn decision, model time, tool time and send time.

**Rehearse before showing it live:** 20 runs, and go live only if every step
succeeds in at least 19 of them.

## Reporting numbers

Publish only what you measured, with its context:

- the summary from a live run (`--json`, `"dry_run": false`): p50/p90 per path
  and the number of runs behind them;
- the network (wired or Wi-Fi), the Home Assistant host, the OpenAI model, and
  the date — Realtime API latency changes over time;
- the failures, not just the successes.

Dry-run output is labeled "SIMULATED" and must never be quoted as a
measurement. No results are published in this repository yet.

## Disclose

- The default "Hey Leonard" model was trained with the maintainer's household
  voices; recall for other voices and distances has not been measured (see
  [Wake-word learning](../docs/wake-word-learning.md#8-the-shipped-model)).
- What leaves the network: [FAQ — privacy](../docs/faq.md#what-about-privacy--what-leaves-my-network).
