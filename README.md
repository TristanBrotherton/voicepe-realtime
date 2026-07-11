# Voice PE Realtime — backend add-on

Turn a Home Assistant Voice PE into a natural speech-to-speech assistant powered
by the OpenAI Realtime API — with instant smart-home control, speaker awareness,
and on-device voice training. This is the **backend half**: a Home Assistant
add-on that owns the OpenAI session and your home's tools. It pairs with the
[Voice PE Realtime firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware),
which turns the device into a thin audio client.

## The experience

Say your wake word and just talk. Replies are generated speech-to-speech (no
STT→LLM→TTS chain), so tone and timing feel like conversation. Smart-home
actions run through Home Assistant's native tools and respond instantly —
lights, climate, media, shopping lists. A follow-up window keeps the mic open
after each reply so conversations flow without re-waking. Say "stop" mid-reply
and it stops.

- **It knows who's talking.** Configure two household names and each wake is
  voice-identified — the assistant can say "sir" or "ma'am", use names
  naturally, and restrict chosen tools to one speaker (enforced below the
  model, so it can't be talked around).
- **It learns your voices.** Say *"teach me my voice"*: the device pins its mic
  open (cyan breathing ring), an automated audio coach walks you through 25
  varied wake-word repetitions plus 90 seconds of natural speech, and the
  recording lands on your box — never sent to any cloud — ready for wake-word
  training or voice-print enrollment. Press the device button to stop anytime.
- **It learns from its mistakes.** Every wake's opening audio is archived
  locally (auto-pruned, newest 500). False trigger? Say *"that was a false
  alarm"* and it labels the capture for the next wake-word retrain.

## Features

- OpenAI Realtime speech-to-speech (`gpt-realtime-2.1` or any model id)
- Native Home Assistant control via the official MCP Server integration
- Speaker awareness + speaker-gated tools (`speaker_male_name`,
  `speaker_female_name`, `male_only_tools`)
- Guided voice enrollment (`enrollment_phrase`, `enrollment_tts_voice`),
  wake-chime auto-mute during sessions (`wake_sound_entity`)
- Failure harvesting: capture archive + `mark_false_wake` voice labeling
- Web search tool (secondary OpenAI call, configurable model)
- Persona fully yours via `instructions` (ours is a dry British butler)
- Production hardening: proactive session refresh before OpenAI's 60-minute
  cap, reconnect recovery, echo/ghost-turn guards, stop-word authority,
  turn-liveness watchdogs

## Install

1. Add this repository URL in **Settings → Add-ons → Add-on store → ⋮ →
   Repositories**, then install **OpenAI Realtime Voice Agent**.
2. Set your OpenAI API key. Install Home Assistant's **MCP Server** integration
   and expose your entities to Assist. Leave `ha_mcp_url` empty (it uses the
   built-in server).
3. Flash the paired
   [firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)
   on your Voice PE, pointing its `va_url` at this add-on (`ws://<ha-ip>:8080/`).

**Multiple devices:** the backend serves one device per instance. Run one
add-on instance per Voice PE, each on its own `websocket_port`, each device's
`va_url` pointing at its port.

## Getting started (comprehensive)

**You need**: a Home Assistant OS install (add-on support), a Home Assistant
Voice Preview Edition device, an OpenAI API key with billing enabled, and the
**ESPHome Device Builder** add-on for flashing.

### 1. Install this add-on
Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
`https://github.com/TristanBrotherton/voicepe-realtime-backend` → install
**OpenAI Realtime Voice Agent**. In its Configuration tab set `openai_api_key`.
Don't start it yet.

### 2. Give it your home
Install Home Assistant's **MCP Server** integration (Settings → Devices &
services → Add integration → "Model Context Protocol Server") and expose the
entities you want voice-controlled to Assist (Settings → Voice assistants →
Expose). Leave the add-on's `ha_mcp_url` empty — it finds the built-in server
automatically.

### 3. Flash the firmware
In ESPHome Device Builder create a new device; replace its yaml with
[`esphome-builder.dhcp.yaml`](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.dhcp.yaml)
from the firmware repo. Set the substitutions:
- Wi-Fi credentials for your network.
- `api_key` — an **ESPHome Noise encryption key** (NOT a Home Assistant token,
  NOT your OpenAI key): 32 random bytes, base64. Generate one with
  `openssl rand -base64 32` or the ESPHome docs' key generator.
- `ota_password` — any password you choose; it protects future OTA flashes.
- `va_url` = `ws://<your-HA-IP>:8080/`.

A factory-fresh Voice PE accepts the first flash wirelessly; after that
everything is OTA. Keep the device `name` stable if you're re-flashing an
already-adopted device.

**Going back to stock**: fully reversible — open the official
[Voice PE web installer](https://esphome.io/projects/?type=voice) in Chrome/Edge
with the device on USB (or use "Install" on the stock firmware in ESPHome
Builder) and it reflashes the factory firmware; re-adopt it in Home Assistant
as normal.

### 4. First conversation
Start the add-on and watch its log for `device (re)connected`. Say the wake
word (stock builds ship a standard model; see the firmware README to train
your own) and ask for a light. If tools are missing, re-check step 2; a
401/403 in the log means set `longlived_token` (HA profile → Security).

### 5. Make it yours
- **Persona** — rewrite `instructions`. The base *timbre* is fixed (you choose
  one of OpenAI's voices via `openai_voice`; no custom/cloned voices), but the
  model steers accent, delivery, attitude and pacing within it — e.g. ours is
  `ballad` instructed into understated Received Pronunciation. Character comes
  from instructions; the voice itself comes from the list.
- **Speakers** — set `speaker_male_name` / `speaker_female_name` for a
  one-male-one-female household: sir/ma'am, names, and `male_only_tools`
  gating. For verified per-person identity, enroll voices (next step) and
  build voice prints (`python3 -m app.build_voiceprint <name> <recording>`
  inside the container → `/share/voice-prints/`).
- **Enrollment** — say *"teach me my voice"*: a guided session records
  wake-word repetitions + natural speech to `/share/voice-enrollment/`,
  used for custom wake-word training (see the firmware README's flywheel
  section) and voice prints.
- **Timers** — set `timer_ring_entity` to your device's exposed
  `switch.<device>_timer_ringing` entity.
- **Sensors** — set `instance_name` (e.g. `kitchen`) to publish
  `sensor.voicepe_kitchen_speaker`, `_active_timers`, `_wakes_today`,
  `_false_wakes_today` for dashboards and automations.
- **False-wake labeling** — say "that was a false alarm" or double-press the
  device button; captures land in `/share/voice-probes/` for retraining.
- **Teach it by voice** — "remember that the bins go out Thursday", "from now
  on use Celsius": standing notes stored in `/share/voice-memory/memory.md`,
  speaker-attributed, folded into its instructions from the next session.
  "Forget..." removes them; "what do you remember" lists them. Only identified
  household voices can change memory.

### 6. Multiple devices
One add-on instance serves ONE device. For a second device, install a copy of
this add-on as a [local add-on](https://developers.home-assistant.io/docs/add-ons/tutorial/)
(copy `openai_realtime_voice_agent/` into `/addons`, change `slug` and `name`
in its config.yaml), give it a different `websocket_port` (e.g. 8082 — avoid
8081), and point the second device's `va_url` at that port.

### FAQ (from the community)

**What does it cost to run?** Usage-based OpenAI Realtime pricing — you pay per
audio token only while actually conversing (wake-word detection is on-device
and free; idle sessions cost nothing meaningful). A busy household day of a
few dozen short exchanges typically lands in the tens of cents; check your
OpenAI usage dashboard after a normal day for your own number. `max_context_messages`
caps per-reply history cost; trimming `mcp_tool_allowlist` reduces per-session
overhead.

**Privacy — what leaves my network?** Wake-word detection runs on the device;
nothing streams anywhere until a wake fires. After a wake, mic audio goes to
OpenAI's Realtime API for the conversation (that's the product), and web
search queries go to OpenAI when used. Everything else stays home: enrollment
recordings, wake captures, voice prints, and speaker identity never leave your
machine — identification runs locally in the add-on.

**Will it work on a Raspberry Pi?** The *backend* yes — it's I/O-bound, and a
Pi 4/5 running HAOS handles it (speaker-ID inference is a few hundred ms on
CPU, off the audio path). The *firmware* is Voice PE-only — it drives that
device's XMOS mic array and audio chain; other satellites aren't supported.
Wake-word *training* is the one heavy job, and it runs on any spare
Apple-Silicon/NVIDIA machine, optionally, occasionally.

**Which wake word do new installs get?** The stock "hey jarvis" model.
Training your own (any phrase, your voices) is the flywheel described in the
firmware README; our `hey_leonard` model in the firmware repo is the worked
example, not the default.

**Other languages?** The Realtime model is multilingual — set
`transcription_language` and write your `instructions` in your language. The
included enrollment coach prompts are English (PRs welcome).

**Does it need HAOS?** The add-on assumes HAOS/Supervised (local add-ons,
supervisor APIs for tools/sensors/timers). Container/Core installs would need
to run the backend manually and lose the supervisor integrations — not a
supported path today.

## Troubleshooting quick hits
- Crackle at reply start → raise `playback_prebuffer_ms` to ~250
- It answers itself / ghost turns → raise `wake_open_delay_ms` / `follow_up_open_delay_ms`
- Mishears in noise → try `noise_reduction: far_field` (default off; the
  device's XMOS already filters)
- Wake word too eager/deaf → the device's "Wake word sensitivity" select in HA

## Notable options

| Option | Purpose |
|---|---|
| `openai_model` / `openai_model_custom` | Realtime model (any model id via custom) |
| `openai_voice` | TTS voice (accent is steerable via `instructions`) |
| `follow_up_listen_seconds` | Mic-open window after replies (default 8) |
| `wake_open_delay_ms` / `follow_up_open_delay_ms` | Echo guards; lower = snappier, riskier |
| `playback_prebuffer_ms` | Raise (~250) if you hear start-of-reply crackle |
| `noise_reduction` | Usually `off` — the device's XMOS already filters |
| `mcp_tool_allowlist` | Trim the toolset for speed/cost |
| `openclaw_url` | Direct agent-escalation endpoint (dodges HA MCP's 60s cap) |

Recordings in `/share/voice-enrollment` and `/share/voice-probes` are personal
data: they stay on your machine and are never uploaded by this add-on.

## Also included

- **Voice timers** — set/cancel/list by voice; the device rings via its exposed
  `timer_ringing` switch (`timer_ring_entity` option), silenced by button or "stop"
- **Voice-print identity** — per-person speaker ID (TitaNet embeddings, enrolled
  centroids in `/share/voice-prints`, ≥3 s duration guard, pitch fallback);
  guests classify as unknown and get neutral handling
- **Voice-instructed memory** — "remember that the bins go out Thursday" /
  "from now on use Celsius" become standing instructions (speaker-attributed,
  household-only, stored locally, active from the next session); "forget..."
  and "what do you remember" manage them
- **Three ways to label a false wake**: say "that was a false alarm",
  double-press the device button, or silence a wake that never spoke (auto)

---
*Based on / inspired by xandervanerven's and fjfricke's ha-openai-realtime — with thanks.*
