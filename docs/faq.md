# FAQ

### Can I go back to the stock firmware?

Yes — fully reversible, nothing about the hardware is changed. Two ways:

- Open the official [Voice PE web installer](https://esphome.io/projects/?type=voice)
  in Chrome/Edge with the device connected over **USB**, and it reflashes the
  factory firmware.
- Or click **Install** on the stock firmware config in ESPHome Builder.

Then re-adopt the device in Home Assistant as normal.

### Does it run on a Raspberry Pi?

The **add-on** does — it needs Home Assistant OS (or Supervised) for add-on
support, and it's I/O-bound: a Pi 4/5 running HAOS handles it (speaker-ID
inference is a few hundred ms on CPU, off the audio path). The heavy lifting —
speech understanding and generation — is OpenAI's cloud, so device requirements
are modest.

The **firmware** is Voice PE-only — it drives that device's XMOS mic array and
audio chain; other satellites aren't supported.

**Wake-word training** is the one heavy job, and it's optional and occasional:
it runs in ~2 hours on any spare Apple Silicon or NVIDIA machine, not on your HA
box.

### What does it cost?

Usage-based OpenAI Realtime pricing — you pay per token only when a response is
generated. Wake-word detection is on-device and free; idle sessions and
connects cost nothing. Measured on a real install (gpt-realtime-2.1, 41 tools,
July 2026):

| Turn | Cost |
|---|---|
| First turn of a session (instruction+tool prefix uncached) | ~$0.019 |
| Every later turn in the session (prefix cached at ~99% off) | ~$0.003–0.013 |

The per-turn spread is mostly **output audio** ($64/1M tokens — the priciest
meter): a one-word reply is ~$0.003, a long spoken answer several cents. Input
speech is nearly free (~10 audio tokens/second ≈ $0.0003/s). A busy day of a
few dozen exchanges lands around **$0.30–0.60**; check
`sensor.voicepe_<instance>_openai_cost_today` (built-in, per-response accounting
from the API's own usage reports) or your OpenAI dashboard for your number.

Cost levers, in measured order of impact: cap `max_output_tokens` (~1200 bounds
runaway monologues, normal replies unaffected); `gpt-realtime-mini` (~⅓ the
price, noticeable quality drop in tool routing); a trimmed `mcp_tool_allowlist`
(shrinks the uncached first-turn prefix — worth ~$1/month at typical usage, so
do it for latency, not money); each web search adds a few cents.

### What about privacy — what leaves my network?

- **Wake-word detection runs on the device.** No audio leaves the Voice PE until
  a wake word or a button press starts a turn.
- **Sent to OpenAI**, because the conversation needs it:
  - your microphone audio after a wake and during follow-up windows;
  - the session instructions: your `instructions`, **your memory notes** (that is
    how the assistant knows them) and the list of tools;
  - results of the tools the assistant uses: Home Assistant states and answers,
    web-search answers, your agent's answers;
  - the recognized speaker's name, as a short note, when speaker recognition is on;
  - the text of timer, announcement, acknowledgement and enrollment-coach
    messages, for speech synthesis (each phrase is synthesized once and cached
    on your box).

  OpenAI's API data policies apply to all of it.
- **Stays on your Home Assistant host**, never uploaded by this add-on:
  enrollment recordings (`/share/voice-enrollment/`), voice prints
  (`/share/voice-prints/`), the memory file (`/share/voice-memory/`), wake data
  (`/share/voice-probes/`: counters and metadata by default; audio clips only
  with `wake_capture: audio`, and they expire), latency metrics (timings only),
  and debug recordings (only with `enable_recording`). Speaker identification
  runs locally. During voice enrollment OpenAI hears nothing: mic audio goes
  only to the local recorder.
- **The add-on log** contains what you said only with `log_transcripts` on (off
  by default).
- **Your agent**, if you connect one, receives the questions the assistant
  escalates and the id of the device that asked.

### Can someone else on my network use it?

Not if you set a device token. Without one, any host on your network can open
a voice session on the add-on's port — spending your OpenAI credit and using
the tools you exposed — and the add-on logs a warning at startup. Set
`device_token` in the add-on and the same `va_token` in each device stub
([step by step](getting-started.md#25-lock-the-device-connection-recommended)),
or restrict addresses with `device_allowlist`. The announce endpoint has its own
bearer token.

### Why does it ask me to confirm before unlocking?

Unlocking, opening garage doors, gates and doors, and alarm-panel actions run
only after a spoken yes (`confirm_actions`). The add-on holds the action until
you answer on the same device within 30 seconds, so a misheard phrase or a TV in
the background can't open the house. Remove categories from `confirm_actions`
if you prefer.

### What are the secrets in the firmware config?

Neither is a cloud credential — both are device-local, and you generate your own:

- **`api_key`** is an **ESPHome Noise/API encryption key** — it encrypts the
  device↔Home Assistant connection. NOT a Home Assistant token, NOT your OpenAI
  key. 32 random bytes, base64: `openssl rand -base64 32` (or let ESPHome
  Builder generate it).
- **`ota_password`** protects over-the-air firmware flashes. Any password you
  choose; keep it matching what the device was last flashed with.

Your OpenAI key lives only in the add-on configuration, never on the device.
The optional `va_token` is the third device-side secret: the same value as the
add-on's `device_token`.

### Which wake word do new installs get?

**"Hey Leonard"** — this project's custom-trained model. Prefer a standard one?
Switch to **Hey Jarvis** or **Okay Nabu** in the device's "Wake word" dropdown in
Home Assistant — no reflash. Or [train your own](features.md#wake-words).

### Does it speak other languages?

The Realtime model is multilingual. Set `transcription_language` to your ISO code
and write your `instructions` in your language, keeping the LANGUAGE / STYLE /
BEHAVIOR structure of the default prompt. The configuration UI even ships a full
Dutch translation. The shipped instructions are English-tuned, and the enrollment
coach prompts are English (PRs welcome).

### Do I need the agent integration?

No. Everything except deep recall and long-running task delegation works
standalone: conversation, smart-home control, web search, timers, memory notes,
speaker recognition, enrollment, sensors. When you want those superpowers,
[OpenClaw](https://openclaw.ai) is what this project is built around and pairs
with best — but the contract is agent-agnostic:
[one URL and two POST shapes](agent-integration.md).

### I trained my voice but it doesn't recognize me / never says my name

Three things must line up — check `sensor.voicepe_<instance>_voice_prints`:

1. The print exists (the enrollment coach says *"your voice print is ready"*
   when it built — since 0.16.5 this is automatic; older versions needed a
   manual build command).
2. The **same name** is in `speaker_male_name` / `speaker_female_name` in the
   add-on configuration (the sensor's `active` attribute shows this) — then
   restart the add-on.
3. If you replaced the default instructions (e.g. for another language), keep
   the SPEAKERS section: identity arrives as a `[voice check]` system note and
   the model only uses it if your instructions say to.

Also: the check needs ~3 seconds of voiced speech, so ask "who am I?" as a
follow-up rather than the very first words after the wake.

### I exposed a new entity or script to Assist but the assistant can't use it

The tool list is fetched from Home Assistant **when a session is created** —
newly exposed scripts (which arrive as new *tools*) aren't picked up by
already-running sessions. **Restart the add-on** after exposing new scripts
and they'll appear. (State of newly exposed *entities* is read live via
GetLiveContext, so those usually work without a restart — it's script tools
that need one.)

### It replied to the TV / a conversation it overheard — how do I stop that?

The mic stays open briefly after each reply (the follow-up window, so you can
answer back without re-waking it). Background speech caught in that window can
be transcribed and answered — and each answer reopens the window, which is how
one stray fragment becomes a rambling exchange. Two levers:

1. **Instructions** (the big one): tell it silence is allowed. Add something
   like: *"You will often overhear speech not addressed to you (TV, people
   talking to each other, fragments). When that happens, produce no spoken
   output at all — remain silent. Only respond when you are reasonably sure
   you were addressed."* Silent responses also break the reply→window→reply
   cascade.
2. **Shorten the window**: lower `follow_up_listen_seconds` (less open-mic
   exposure, at the cost of tighter follow-up timing), and keep
   `vad_eagerness: low` so fragments are less likely to commit as turns.

### Why does it say "still working on that"?

You asked for something long-running (research, a multi-step agent task). Rather
than keeping you waiting at the speaker — or failing when the voice turn times
out — the assistant ends the turn and lets the agent keep working. When the task
finishes, the result is **announced out loud in the room you asked from** (or
sent to you by text if the device is unreachable). See
[Long-running task delegation](features.md#long-running-task-delegation).

### Does it need Home Assistant OS?

Yes — the add-on assumes HAOS/Supervised (add-on installs, supervisor APIs for
tools/sensors/timers). Container/Core installs would need to run the backend
manually and lose the supervisor integrations — not a supported path today.

### It answers itself / answers nobody / crackles — help?

Quick hits:

- Crackle at reply start → raise `playback_prebuffer_ms` to ~250.
- Ghost turns (it answers its own echo) → raise `wake_open_delay_ms` /
  `follow_up_open_delay_ms`.
- Mishears in noise → try `noise_reduction: far_field` (default off; the
  device's XMOS already filters).
- Wake word too eager or too deaf → the device's "Wake word sensitivity" select
  in HA. "Slightly sensitive" is the calibrated default; the other two wake more
  easily from a distance and false-wake more often. Flag false wakes with a
  double-press so you can see the rate per device.
- Rarely, the assistant may stop itself on a word in its own reply that sounds
  like "stop" — just ask again.

### How fast is it?

Measure it on your own setup rather than trusting a number in a README: every
turn's timeline is published as `sensor.voicepe_<instance>_latency` (end of
speech to first reply audio, the server's end-of-turn decision, tool time,
and rolling p50/p90), and the device reports its own share (wake to mic open,
reply audio to speaker). The biggest levers are the network, the model,
`vad_eagerness` (how long the server waits before deciding you've finished)
and slow tools such as web search. The [demo](../demo/README.md) shows how to
measure a scripted run.

### Why does it briefly reconnect about once an hour?

OpenAI caps a Realtime session at 60 minutes. The add-on refreshes proactively
during a quiet moment, so you'll rarely notice — at worst an occasional 1–2 s
pause. Conversation context survives the refresh.
