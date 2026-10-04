# Features

Each section is a self-contained guide: what the feature does, how to use it,
and which options drive it. Option details live in the
[Configuration Reference](configuration.md).

- [Wake words](#wake-words)
- [Speaker recognition & voice enrollment](#speaker-recognition--voice-enrollment)
- [Voice-instructed memory](#voice-instructed-memory)
- [Instant recall & agent escalation](#instant-recall--agent-escalation)
- [Long-running task delegation](#long-running-task-delegation)
- [Voice timers](#voice-timers)
- [False-wake flagging](#false-wake-flagging)
- [Wake-word learning](#wake-word-learning)
- [Web search](#web-search)
- [HA sensors](#ha-sensors)
- [Persona & voices](#persona--voices)
- [On the device](#on-the-device)

---

## Wake words

**"Hey Leonard" ships as the default wake word** — a custom microWakeWord model
trained by this project on real household voices (it's the worked example of
[wake-word learning](#wake-word-learning) below; the model and its metrics-only
evaluation manifest live in the firmware repo's [`models/`](https://github.com/TristanBrotherton/voicepe-realtime-firmware/tree/main/models)
directory).

Detection runs entirely **on-device** — no audio leaves the Voice PE until a wake
fires.

Three ways to pick your wake word:

1. **The "Wake word" dropdown in Home Assistant** (on the device's page). Switch
   between **Hey Leonard**, **Hey Jarvis**, and **Okay Nabu** at runtime — no
   reflash needed. The firmware substitution `default_wake_word` sets which one a
   fresh device starts on.
2. **Any stock microWakeWord model** — point the `wake_word_model` firmware
   substitution at a model URL, e.g. the official
   [hey_jarvis](https://github.com/OHF-Voice/micro-wake-word/releases/download/v2.1_models/hey_jarvis.json)
   or okay_nabu releases.
3. **Train your own** — any phrase, tuned to your voices. The community
   [microWakeWord Trainer for Apple Silicon](https://github.com/TaterTotterson/microWakeWord-Trainer-AppleSilicon)
   is the tool this project's own model was trained with; training runs in ~2 hours
   on an Apple Silicon or NVIDIA machine. Use your
   [enrollment recordings](#speaker-recognition--voice-enrollment) as real positives
   and your [flagged false wakes](#false-wake-flagging) as hard negatives, then set
   `wake_word_model` to your model. See [Wake-word learning](wake-word-learning.md)
   for the full loop and the release gate.

**Sensitivity** is a runtime select in HA too ("Wake word sensitivity"), for every
wake word:

- **Slightly sensitive** (default) runs the model at its own calibrated cutoff —
  the operating point that passed the release gate.
- **Moderately** and **Very sensitive** lower the cutoff by steps derived from
  the model's evaluation manifest. They wake more easily from across the room
  and false-wake more often; both sit outside the validated false-accept budget.

The device reports the model, its SHA-256 prefix, cutoff, window and tier with
every wake, and shows them as the "Wake word operating point" diagnostic. A
numeric `wake_cutoff_*` substitution still overrides a tier; leave them unset
unless you have your own calibration.

---

## Speaker recognition & voice enrollment

The assistant knows who's talking — locally, on your box. It greets people by name,
attributes [memory notes](#voice-instructed-memory) and
[timers](#voice-timers) to the right person, and can restrict chosen tools to a
specific speaker.

Two tiers, from zero-setup to per-person identity:

**Tier 1 — voice-type heuristic.** Set `speaker_male_name` and
`speaker_female_name` for a one-male-one-female household. Each wake's opening
audio is classified by pitch (in-process, off the audio path) and the verdict is
injected into the session, so the assistant can use names or sir/ma'am. It cannot
tell two men apart, and same-voice-type guests match that name. Leave both names
empty to disable.

**Tier 2 — voice prints.** Per-person identification with a neural
speaker-embedding model: each wake's capture is embedded and compared against
enrolled per-person centroids (stored in `/share/voice-prints/<name>.json`), with
a ≥3 s duration guard and the pitch heuristic as fallback. Guests classify as
*unknown* and get neutral handling.

To enroll a voice print — two steps:

1. **Say "train my voice"** (or "teach me my voice" — any similar phrasing). The
   device enters a true enrollment mode: mic pinned open, wake/stop detection
   disarmed, **cyan breathing LED**, a 10-minute hard cap, and the center button as
   a physical escape. An automated audio coach walks you through **25 varied
   repetitions** of the `enrollment_phrase` plus **90 seconds of natural speech**.
   When the session completes, **the voice print builds automatically** and the
   coach confirms out loud: *"Your voice print is ready."* (If there wasn't
   enough clear speech, it says so and you just run the session again.)
2. **Put the same name in the add-on configuration** — recognition is inactive
   until the enrolled name appears here:

   ```yaml
   speaker_male_name: "Alex"
   speaker_female_name: "Sam"
   ```

   The coach reminds you out loud if you enrolled a name that isn't configured
   yet. Restart the add-on after changing it.

**Verify it worked** in Home Assistant: `sensor.voicepe_<instance>_voice_prints`
shows every enrolled print and — in its `active` attribute — which ones are
live (enrolled *and* named in the configuration). Privacy: **OpenAI hears
nothing during enrollment** — mic audio flows only to the local recorder
(`/share/voice-enrollment/`), and prints live in `/share/voice-prints/`.

Rebuilds and multi-recording prints are still available manually from inside
the add-on container:

```
python3 -m app.build_voiceprint <name> <recording.wav> [more.wav ...]
```

Options: `enrollment_phrase` (set it to your actual wake phrase),
`enrollment_tts_voice` (the coach's voice), `wake_sound_entity` (auto-mutes the
wake chime during the session so the coach stays audible).

**Speaker-gated tools**: list tool names in `male_only_tools` and they execute only
for the gated voice — enforced *below* the model, so it can't be talked around.
Convenience gating, not biometric security.

The same enrollment recordings double as wake-word training positives — one
session per person feeds both systems.

---

## Voice-instructed memory

Teach it standing rules by voice; they persist forever.

- **"Remember that we park at the north lot"** / **"From now on, use Celsius"** —
  the note becomes a standing instruction in every future conversation. It takes
  effect at the next session (minutes, at most an hour).
- **"Forget about the north lot"** — removes matching notes.
- **"What do you remember?"** — reads them back.

Notes are stored locally in `/share/voice-memory/memory.md` — plain markdown you
can also edit by hand — capped at 60 notes, each attributed to the household
member whose voice gave it. The file is shared by all device instances and
survives rebuilds.

**Writes are speaker-gated**: only identified household voices can add or remove
notes. Guests and unidentified voices are politely refused.

This is the assistant's *rule* memory. For deep factual recall (contacts, dates,
history), see the next section.

---

## Instant recall & agent escalation

With an agent like [OpenClaw](https://openclaw.ai) connected (`openclaw_url` — see
[Agent Integration](agent-integration.md) for the full contract), the assistant
gets a two-speed memory path:

**`recall_memory` — the fast path.** *"What's Grandma's number?"*, *"When is Sam's
birthday?"*, *"What did we decide about the fence?"* — an instant (sub-second),
deterministic search of your agent's memory files. The bridge answers
`{"recall": "<query>"}` with matching lines and the assistant reads the answer
straight back. The model is instructed to try this **first** for any personal or
household recall question.

**`ask_openclaw` — the deep path.** When recall finds nothing, or the request
needs action (messages, calendar, research, cross-app tasks), the assistant
escalates the full question to your agent and waits for its answer — with a
~2.5-minute budget, bypassing Home Assistant's hard 60-second MCP request cap
that would otherwise kill long agent turns.

Despite the option name, this is **agent-agnostic**: anything that speaks the
simple POST contract works — OpenClaw is just one example. The contract is two
JSON shapes:

```
POST <openclaw_url>  {"question": "...", "room": "kitchen"}  →  {"answer": "..."}
POST <openclaw_url>  {"recall": "..."}                        →  {"matches": ["...", ...]}
```

See [Agent Integration](agent-integration.md) to wire up your own.

---

## Long-running task delegation

*"Research flight prices to London for October."* Some tasks take longer than
anyone wants to stand by a speaker. The delegation flow:

1. The assistant hands the task to your agent (`ask_openclaw`), telling you it's
   looking into it.
2. If the agent is still working at ~2 minutes, the bridge answers **"still
   working"** instead of failing — the assistant tells you it will report back,
   and the voice turn ends.
3. The agent keeps working as long as it takes, then **announces the result out
   loud in the room you asked from** — the request carries the room name
   (`instance_name`), so the report-back finds the right device. If no device is
   reachable, the agent can fall back to a text channel.

The report-back lands through the **announce endpoint**: with `announce_port` and
`announce_token` both set, the add-on exposes

```
POST http://<ha-host>:<announce_port>/announce
Authorization: Bearer <announce_token>
{"message": "Flights to London in October start at ..."}
```

which speaks the message through the device's guarded TTS lane — the same path
timers use, so the assistant can't hear itself and reply. Returns `503` when no
device is connected (the caller should fall back to text). Full endpoint spec in
[Agent Integration](agent-integration.md#the-announce-endpoint).

Anything on your LAN can use the endpoint — it's a general "speak in this room"
API for automations, not just agents. Generate a long random token; the add-on
runs on the host network, so the token is the lock.

---

## Voice timers

*"Set a pasta timer for 9 minutes."* Timers are set, cancelled, and listed by
voice. Up to 10 concurrent, 5 seconds to 24 hours.

Expiry is polite, in three stages:

1. **One personal spoken announcement** — *"Alex, your pasta timer is done"* —
   addressed to whoever set it (via [speaker recognition](#speaker-recognition--voice-enrollment)).
   No nagging repeats.
2. **A 20-second grace period.** A wake from the device that set the timer counts as
   acknowledgement — no bell.
3. **A gentle two-tone bell** only if unacknowledged, auto-stopping after
   2 minutes. Silence it anytime with the **center button** or **"stop"**.

Setup: expose the device's `switch.<device>_timer_ringing` entity and set it as
`timer_ring_entity` in the add-on. Without it, the assistant will say timers are
unavailable rather than pretending. One add-on instance has one bell entity;
with multiple devices, the spoken expiry and acknowledgement return to the
device that set the timer, while the physical bell uses that shared entity.

Timers survive the hourly OpenAI session refresh (they live in the add-on, not
the model) but **not add-on restarts** — fine for kitchen timers, worth knowing.
The bell sound itself is a firmware substitution
(`timer_finished_sound_file`) if you'd like a different one.

---

## False-wake flagging

When the device wakes by mistake, flag it. A flag labels the wake on **the
device you flag it on** — never another room's:

1. **Double-press the center button** — labels the exact wake the device names.
   A press while the add-on is unreachable is queued on the device and
   delivered later (up to 10 minutes).
2. **Press the button during the wake** — silencing a session within 12 s of
   the wake, before any reply, labels it.
3. **By voice** — say *"that was a false alarm"* within 30 s of the wake.

A wake that ends without anyone speaking is only a *candidate* for review,
never a training negative: people often wake the device and change their mind.

What gets stored is your choice (`wake_capture`): by default only counters
and wake metadata (time, device, model, cutoff, window, label), no audio. With
`wake_capture: audio`, a short clip after each wake is kept on your HA host for
review, and `trigger_capture` can add the ~1.5 s before the wake (opt-in on
the add-on *and* on the device). Clips expire automatically (30 days
unlabeled, 180 days labeled), and a guest-mode switch stops all storage. The
`false_wakes_today` [sensor](#ha-sensors) counts flags per device.

---

## Wake-word learning

Labeled false wakes and enrollment recordings can train a better model — but a
new model ships only on evidence:

1. **Train** outside this repository with the community
   [microWakeWord trainer](https://github.com/TaterTotterson/microWakeWord-Trainer-AppleSilicon)
   (about two hours on Apple Silicon or NVIDIA).
2. **Calibrate and gate**: every cutoff/window pair is evaluated, and one exact
   pair must keep recall within 0.01 of the deployed model and false accepts
   within 0.05/hour of it — and actually improve on it.
3. **Shadow, canary, fleet**: the candidate runs log-only next to the live model
   for a week, then live on one device for a week, with an automatic rollback
   trigger on the false-wake flag rate. The previous model stays in the
   firmware repo's `models/previous/` for one-step rollback.

Nothing trains, promotes or flashes automatically. The shipped model's numbers
(recall 0.9749 at 0.83 false accepts/hour on the trainer's validation sets,
up from 0.9669 at the same rate for the previous model) and what has *not*
been measured yet (recall by distance) are in
[Wake-word learning](wake-word-learning.md#8-the-shipped-model).

---

## Web search

On by default (`enable_web_search`). When the assistant needs current or general
info — weather, news, opening hours, facts — it calls its `web_search` tool; the
add-on makes a second, server-side OpenAI call (the Responses API `web_search`
built-in, on `web_search_model`) and reads a short spoken answer back.

- Uses your existing OpenAI key — no extra account.
- Default model `gpt-5.5` (best quality); mini/nano variants are cheaper. A few
  cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- A rejected model name won't crash the session — the assistant just says it
  couldn't search; fix `web_search_model` and retry.

---

## HA sensors

Set `instance_name` (e.g. `kitchen`) and the add-on publishes per-device sensors
for dashboards and automations:

| Entity | State |
|---|---|
| `sensor.voicepe_kitchen_speaker` | who spoke last (name / `unknown` / `none`), with score and method attributes |
| `sensor.voicepe_kitchen_active_timers` | count of running timers, with next-expiry attributes |
| `sensor.voicepe_kitchen_wakes_today` | wakes since midnight |
| `sensor.voicepe_kitchen_false_wakes_today` | flagged false wakes since midnight |
| `sensor.voicepe_kitchen_openai_cost_today` | estimated OpenAI spend today ($, per-response accounting) |
| `sensor.voicepe_kitchen_voice_prints` | enrolled voice prints (attribute `active` = enrolled **and** configured) |
| `binary_sensor.voicepe_kitchen_enrollment_active` | an enrollment session is running |

The firmware separately exposes the device **phase** as a text sensor
(`idle / waiting / listening / thinking / replying / enrolling`) — trigger
automations on it, e.g. pause the kitchen speaker the instant a wake fires.

---

## Persona & voices

The assistant's character lives in the `instructions` option — rewrite it freely:
personality, language, house rules, tone. The shipped default is a practical
English voice-tuned prompt (short spoken replies, silent tool calls, varied
confirmations, strict language pinning); the "Leonard" persona this project runs
is a dry British butler built the same way.

What you can and can't change:

- **The voice timbre is fixed** — you pick one of OpenAI's voices via
  `openai_voice` (`marin`, `cedar`, `alloy`, `ash`, `ballad`, `coral`, `echo`,
  `sage`, `shimmer`, `verse`). You cannot invent or clone arbitrary new voices.
- **Accent, delivery, attitude, and pacing are steerable by instruction** within
  that timbre — the model follows direction. Example: `ballad` instructed into
  understated Received Pronunciation reads as a British butler.
- **Speed** is a separate knob (`openai_speed`, 0.25–1.5).
- **Spoken messages outside the conversation** (the enrollment coach, timer and
  announce messages) use OpenAI's TTS voices and are configurable separately —
  `enrollment_tts_voice` accepts any `/v1/audio/speech` voice.

Language: the Realtime model is multilingual. Set `transcription_language` to your
ISO code and write your `instructions` in your language, keeping the same
LANGUAGE / STYLE / BEHAVIOR structure as the default prompt.

---

## On the device

Firmware niceties worth knowing about (all in the
[firmware repo](https://github.com/TristanBrotherton/voicepe-realtime-firmware)):

- **Thin audio client** (`va_client`): raw 16 kHz mic streaming up, 24 kHz reply
  playback down, jitter buffering, mic pre-roll, and reconnect logic. There is no
  Assist pipeline on the audio path.
- **Phase text sensor** — `idle / waiting / listening / thinking / replying /
  enrolling`, exposed to HA for automations.
- **Per-phase stop-word cutoffs** — "stop" is tuned per phase so the assistant's
  own voice can't false-trigger it; a **red confirmation flash** acknowledges
  your stop. Echo guards at the wake boundary are tunable from the backend
  without reflashing.
- **Proper loudness** — OpenAI's audio is mastered quieter than stock TTS; the
  firmware compensates (single-attenuation volume path) so replies match the
  device's own chimes at every knob position.
- **Silent connection errors** — LED-only (red twinkle), no spoken "cloud
  unavailable" announcements at night; the wake-time error chime (user-initiated
  feedback) is kept.
- **Stock niceties preserved** — LED ring language, volume dial, mute switch
  (ring dark with red markers; muting also ends an open listening window), and
  the media player for Music Assistant.
