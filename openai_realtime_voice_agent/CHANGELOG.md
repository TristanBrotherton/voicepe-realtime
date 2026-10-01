# Changelog

All notable changes to this add-on. Newest first.

## 0.20.0 (fork)

- **Tillägget håller ingen Home Assistant-nyckel längre** (raawr US-011,
  beslut 56). `homeassistant_api` är av, `longlived_token` och `ha_mcp_url`
  är borta. Varje HA-anrop — MCP-verktygen, `search_home`, `play_media`,
  sensorerna, timerns ringbrytare och inspelningens väckljud — går till
  `ha_api_url` (raawr-comms) med `comms_nyckel`, i `app/ha_api.py`. Comms
  håller HA:s token och släpper bara de former tillägget faktiskt använder.
  Återvägen: `homeassistant_api: true` och 0.19.5.

## 0.19.5 (fork)

- **Timern ringer, och gör inget annat.** En 30-sekunderstimer var en röst vid
  30 s och en klocka vid 50 s: ett talat utrop först, sedan 20 sekunders
  respit, sedan bjällran om ingen väckt enheten. Rösten kom dessutom från en
  annan motor än björnen. Efterfrågat och borttaget 2026-09-09: "det räcker
  med chime på rätt tid". En timer är det enda i huset som måste vara exakt.
  Utropet, respiten, ägaren och väck-kvittensen är borta — inte avstängda,
  borta, med sin inkoppling i main.py. TTS-banan finns kvar för
  inspelningscoachen och announce-endpointen.

## 0.19.4 (fork)

- **En främling i huset läste upp timern.** Utropet vid utgången går genom
  TTS-banan, inte genom modellens röst — och den banan var skriven för
  engelska inspelningsuppmaningar: rösten `fable` med instruktionen "Calm,
  composed British butler." Hört live 2026-09-09 23:19, mellan två repliker
  från en djup svensk björn. Nu `onyx` som förval, och instruktionen beskriver
  björnen i stället för en betjänt. Detta gör inte banan till hans röst —
  det är en annan motor — men den låter inte längre som en annan person.
  Riktig lagning, senare: låt modellen själv säga meningen.

## 0.19.3 (fork)

- **Timern talade engelska i ett svenskt hus.** Hört live 2026-09-09 22:58:
  "Henrik, your timer is done." Utropet vid utgången är den enda mening
  add-onet säger utan att modellen skrivit den, och den var kvar på engelska
  sedan uppströms. Nu: "Henrik, din timer är klar." — och med etikett "Henrik,
  din timer för pasta är klar." Utan namn får meningen den stora bokstaven
  namnet annars bär. Ingen inställning: prompten, transkriberingsspråket och
  huset är svenska, en språkknapp här vore bara ett andra ställe att glömma.

## 0.19.2 (fork)

- **Tools died 81 ms after they started, and the assistant said they had
  worked.** Measured live 2026-09-09 22:23: `play_media` was called, the
  handler logged `🎵 play_media: 'chill' type=playlist player=kontoret`, and
  81 ms later pipecat cancelled it — while the reply said "jajemän, fixar
  det" and nothing played. Same for `GetLiveContext` and `vaderprognos`, on
  every turn all evening. The cause is Gemini's late input transcript: it
  arrives AFTER the model has already called the tool, and pipecat's user
  aggregator turns that late transcript into an emulated "user started
  speaking" — so the very sentence that asked for the tool interrupts it, and
  the interruption cancels it. The rule that prevents this (`register_function`
  with `cancel_on_interruption=False`) was written for the OpenAI service and
  stayed there when the house moved to Gemini. It now lives in one place,
  `ToolRegistrationMixin`, that both engines mix in, so it cannot protect one
  engine and forget the other again. The speaker gate and the liveness
  tracking moved with it and now cover Gemini too — they never did before.

## 0.19.1 (fork)

- **Fixes a regression 0.18.3 shipped straight into the house.** Waiting for
  the engine's end-of-turn assumed `LLMFullResponseEndFrame` reaches
  PhaseEmitter. It does not: `LLMAssistantAggregator` sits between the engine
  and the phase machine and consumes it. So the wait was spent in full on
  EVERY Gemini turn — the log said `no end-of-turn from the engine after 8.1s`
  each time, the device stayed shut for eight seconds after each answer, and
  the user had to repeat himself. Two changes: the Gemini service now calls
  the phase machine directly when Google reports `turn_complete` (the path
  that actually works), and the wait is only ever entered on a connection that
  has genuinely seen an end-of-turn — so an engine that never signals behaves
  exactly as it did before 0.18.3, rather than paying the cap every turn.

## 0.19.0 (fork)

- **The microphone stays open because the model asked, not because a timer
  said so.** New tool `request_follow_up`. The device has accepted
  `{"type":"request_follow_up"}` all along — `va_client.cpp`'s own comment
  names the tool that was supposed to send it, and it never existed on this
  side. So the window was opened on `follow_up_ms` instead, sent once at
  connect and applied by the device after EVERY reply: say "that was all",
  get "Bra. Hörs." back, and the mic still opened for another eight seconds,
  with a chime, listening to an empty room. The system prompt has always
  promised the opposite — "the house keeps the microphone open exactly as long
  as your reply ends with a question mark" — and nothing implemented it.
  Now the model asks in the same turn as a real question, and says nothing
  after a finished answer.
  - Reading the reply for a question mark would have been the obvious fix and
    is the wrong one: on Gemini the assistant transcript does not reliably
    reach this pipeline (measured 2026-09-09 — every user line logged, not one
    assistant line), so that rule would have silently never fired.
  - The request is recorded at tool-call time but sent at the engine's
    end-of-turn. The device opens the mic as soon as its speaker drains, which
    at tool-call time it usually has — sending immediately would open the mic
    before the question was spoken.
  - **Set `follow_up_listen_seconds` to 0** to get the new behaviour; anything
    higher keeps the old unconditional window on top of it.
- 7 new tests (194 total).

## 0.18.3 (fork)

- **No more start chime in the middle of an answer.** The phase machine ended
  a reply on a timer: 1.5 s of silence after the last audio and the device was
  told the turn was over. That holds for OpenAI, whose reply arrives in
  sentence-sized pieces. It does not hold for Gemini — measured live
  2026-09-09, one answer came in three bursts with 6.7 s and 4.7 s of silence
  between them, so the phase went replying → idle → replying twice inside a
  single answer and the device chimed on every way back in. The engine says
  when it is finished (`LLMFullResponseEndFrame`, from Gemini's own
  `turn_complete`), which a timer can only guess at, so the debounce now waits
  for that before releasing the device. Capped by `PHASE_MID_TURN_GRACE_MS`
  (8 s) so an engine that never sends one — or a reply that dies half-way —
  costs a slow idle rather than a device stuck in "replying".

## 0.18.2 (fork)

- **The native-audio model can actually be used in a Swedish house.** It
  refuses an explicit language code this house needs — probed live:
  `sv` and `sv-SE` both come back `1007 Unsupported language code`, while
  `en-US`, `de-DE` and *no code at all* open fine. So on these models the code
  is dropped and the (entirely Swedish) system instruction steers the
  language. There is no clean way to ask pipecat for that: `language=None`
  becomes the string `"en-US"` before it reaches the wire, which would have
  pinned the house to English *silently*, since en-US is a code the model
  accepts. The setting is cleared on the built service instead, and logged.
- **The wedge warning stopped crying wolf on Gemini.** `force_reconnect` has
  stood back from a self-healing engine since 0.17.3, but the alarming
  "presuming a half-open OpenAI socket" warning was logged before the guard
  was reached, so the log kept reporting a repair that never happened.

## 0.18.1 (fork)

- **Proactive audio actually reaches the session now.** 0.18.0 followed
  Google's guide, which says these features need API version `v1beta`. They do
  not, and the mistake is invisible: google-genai already defaults to v1beta,
  so setting it changes nothing, and the session is refused with
  `1007 ... Unknown name "proactivity" at 'setup': Cannot find field`. Probed
  all six combinations against the live account: v1beta takes affective dialog
  but not proactivity; **v1alpha takes both**. pipecat's own docstring said
  v1alpha all along.

## 0.18.0 (fork)

Towards a Gemini session you can hold an ordinary conversation with.

- **The engine is finally told when the microphone stops.** Gemini Live has
  `audioStreamEnd` — Google's guide calls it the way to "flush any cached
  audio" when an audio stream pauses, after which the client "can resume
  sending audio data at any time without reconnecting". This add-on never sent
  it. Two consequences: half an utterance, left behind when the follow-up
  window closed mid-sentence, stayed cached on Google's side and could be
  completed into a stale answer on the next wake (the OpenAI path has cleared
  exactly this since 2026-06-12); and a pause was indistinguishable from a
  dead client, which is what the ~152 s idle hang-ups were. It is now sent on
  the device's stop button and on the follow-up cut-off, through one shared
  `drop_pending_input_audio()` that speaks each engine's own dialect —
  `input_audio_buffer.clear` for OpenAI, `audioStreamEnd` for Gemini.
- **`gemini_affective_dialog`**: the model matches the expression and tone it
  hears instead of reading every answer flat. Shares proactive audio's gate —
  a native-audio model on `v1beta` — and the same refusal to be switched on
  with a model that cannot carry it.
- 6 new tests (181 total).

**Not changed, and deliberately**: handsfree barge-in stays off. It was tried
on this hardware and measured: the ~10x speaker→mic leak defeated the XMOS
AEC, the VAD flapped listening↔thinking, and it once built into an acoustic
feedback squeal. See `barge_in: false` in the firmware for the full note.

## 0.17.5 (fork)

- **A quiet house no longer kills the Gemini engine.** The Voice PE is
  push-to-talk: between conversations the add-on sends Google nothing, and
  Google hangs up on a session it hears nothing from — measured live at a very
  regular ~152 s of silence. pipecat reconnects in about half a second, so the
  hang-up itself is invisible. But pipecat only forgives a failure from inside
  its receive loop, when a message ARRIVES; a silent connection delivers none,
  so the stable-connection rule never ran and the count never cleared. Three
  idle hang-ups in a row — about seven and a half quiet minutes — were pushed
  as a fatal error. Live 2026-09-09 17:05:41: the engine died and the house
  had no voice until the add-on was restarted 45 minutes later. The service is
  now a subclass that runs pipecat's own rule at the moment of failure, when
  the connection's lifetime is known. Three failures inside the threshold
  still go fatal — that is the case the counter is for.

## 0.17.4 (fork)

- **A false wake can be reported again.** Both ways of flagging one -- saying
  so ("that was a false alarm") and the button-cancel shortly after a wake --
  listed `/share/voice-probes` directly. That directory is only written while
  `ENABLE_RECORDING` is on, so on a normal install it does not exist and every
  report ended in `FileNotFoundError`: the assistant apologised, and the
  counter behind `sensor.voicepe_<instance>_false_wakes_today` never moved.
  The count needs no audio, so it is now published either way, and a missing
  recording is reported as "nothing kept" rather than as an error. One shared
  helper replaces the path literal that had been copied into three files.

## 0.17.3 (fork)

First run in a real house, 2026-09-09, found the Gemini engine had been given
a session but not a room. Three fixes, all Gemini-only — OpenAI is untouched.

- **Turn detection is now configured, not left to Google.** The session sent
  no `realtime_input_config` at all, so the API ran its automatic activity
  detection at its own `START_SENSITIVITY_HIGH`. On a speaker that hears its
  own voice that means answering room noise, the tail of its own reply, and
  half-words nobody said — the log has it answering `Och?`, `Ja.`, `Né?` and
  one whole sentence in Portuguese. Four new settings, defaulting to the
  equivalent of the OpenAI side's `vad_eagerness: low`:
  `gemini_vad_start_sensitivity` (low), `gemini_vad_end_sensitivity` (low),
  `gemini_vad_prefix_padding_ms` (300), `gemini_vad_silence_duration_ms`
  (800 — Google's own recommended range is 500–800 ms).
- **The wedge repair no longer fights an engine that heals itself.** Twelve
  seconds after any quiet wake, `force_reconnect` ran the full OpenAI repair
  on Gemini: it forced an idle phase at the device first — which the user
  hears as the end-of-turn chime and sees as the LED, mid-conversation — and
  only then discovered `service has no reset_conversation()` and gave up,
  leaving a session pipecat was already reconnecting on its own. Observed
  three times in ten minutes. `handle_error` had stood back from a
  self-healing engine since the provider work; `force_reconnect` and the
  proactive 60-minute-cap refresh now read the same table.
- **Optional: `gemini_proactive_audio`.** Google's own "was that meant for
  me?" judgement — the model hears the room but stays silent when it was not
  addressed, and silence is not billed. This is what the Gemini app does. It
  needs a native-audio model (`models/gemini-2.5-flash-native-audio-latest`)
  on API version `v1beta`; Gemini 3.1 Flash Live does not support it, so with
  that model the setting is ignored and warned about rather than allowed to
  get the session refused.
- 12 new tests (170 total). All six of the new behaviour tests were confirmed
  to fail against 0.17.2 before the fixes landed.

## 0.17.0 (fork)

- **Second voice engine: Google Gemini Live**, as an alternative to OpenAI
  Realtime. `voice_provider` picks the primary engine (`openai` default);
  `voice_provider_backup` names the *other* engine, which takes over
  automatically when the primary runs out of money, has its key rejected, or
  its socket dies and a retry doesn't help. A backup equal to the primary
  means no failover, the same as `none`. New options: `gemini_api_key`,
  `gemini_model`, `gemini_voice`, `provider_cooldown_minutes` (default 30 —
  how long the backup runs before the primary is tried again).
- **What changes if you only update, without touching any setting**: the
  engine and the audio path are the same (OpenAI, no failover, same voice and
  latency), but the assistant now remembers across reconnects where it
  previously forgot. The cached conversation is finally delivered to the
  engine on reconnect — it provably never was before, on either engine — and
  the session is re-seeded every hour instead of starting blank, so each turn
  is billed with that history as its prefix (bounded by
  `max_context_messages`, default 12). A new entity,
  `sensor.voicepe_<instance>_motor`, also appears.
- Failure is classified before any switch is made: quota/billing and
  auth/model errors switch immediately; a transient error (timeout, dead
  socket, 5xx) gets one retry on the same engine first; a tool error never
  triggers a switch.
- New `sensor.voicepe_<instance>_motor`: which engine is running, with
  `reason`, `switched_at`, and `retry_primary_in_s` attributes, so a switch is
  visible instead of only appearing in the log.
- Both engines get the same tools, system prompt, memory, and duck/interrupt
  behaviour. What's genuinely different between them (voice names, no
  `openai_speed` or noise reduction on Gemini, its own VAD tuning, cost
  observability only implemented for OpenAI so far) is documented in
  `Docs/superpowers/specs/2026-09-08-gemini-live-provider-design.md` in the
  main Raawr repo, not hidden behind a claim of parity.
- 158 unit tests cover the router, failure classification, and both provider
  modules (no API keys required to run them). **Not yet exercised against a
  live house** — that verification is a separate, manual step.

## 0.16.11 (fork)

- Fixed announcements immediately after a single Voice PE reconnect. The sole
  connected device is now addressable before its first wake/audio activity;
  multi-device instances still require activity or an explicit target when
  more than one idle device is connected.

## 0.16.10 (fork)

- Added an opt-in relay-side output lead buffer for the measured Voice PE
  resampler cold-start defect. It holds the first part of a reply and releases
  it as a burst, giving the device a playout lead before normal streaming.
- The buffer is safe across interruption, connection recovery, short replies,
  and mid-reply pauses, with a bounded watchdog for a stalled source. It is
  disabled by default; our two-device deployment enables 400 ms while the
  existing device playback prebuffer remains 250 ms.

## 0.16.9 (fork)

- Added selectable OpenAI transcription models, including `gpt-live-transcribe`
  and `gpt-transcribe`. Both now receive their required `languages` array when
  a transcription language is configured.

## 0.16.8 (fork)

- **Multiple Voice PE devices on one add-on instance**: every connected device
  now has its own OpenAI session, conversation history, audio pipeline, phase
  updates, speaker-recognition state, and enrollment flow. Devices can talk at
  the same time without interrupting or receiving audio from one another.
- Reconnecting a device replaces only its own stale connection; other active
  devices keep their conversations intact.
- Timer announcements and acknowledgements stay with the device that created
  the timer. Targeted announce requests now return an error when that device
  is offline rather than reporting a false success.

## 0.16.7 (fork)

- **Wedge watchdog**: a half-open OpenAI socket (dies silently during an idle
  gap — no close frame, no error) used to swallow the next request entirely:
  audio streamed out, nothing came back, no reply. Now every wake arms a 12 s
  liveness check; if the server VAD shows no activity, the session reconnects
  in place (~3 s). A silent wake triggers a harmless idle-time reconnect.

## 0.16.6 (fork)

- **Fixed: direct `ask_openclaw` silently rebinding to the HA MCP path.**
  pipecat registers a handler for every MCP tool during session creation,
  which overwrote the native direct-path handler — resurrecting the 60-second
  MCP cap ("it failed" while the task actually succeeded). Native registration
  now happens after MCP registration and wins.
- **Announce endpoint repeat guard**: near-duplicate messages within 10
  minutes are accepted but not spoken (`duplicate_suppressed`), so an agent
  monitoring for a result can't re-announce the same news every poll cycle.

## 0.16.5 (fork)

- **Voice prints now build automatically** when enrollment completes — the
  coach confirms out loud, warns when the enrolled name isn't in
  `speaker_male_name`/`speaker_female_name` (recognition stays inactive until
  it is), and asks for a retry when there wasn't enough clear speech.
  Previously this required a manual `python3 -m app.build_voiceprint` step
  that was easy to miss, leaving enrollments silently ineffective.
- New `sensor.voicepe_<instance>_voice_prints`: enrolled prints, with an
  `active` attribute showing which are enrolled *and* configured.

## 0.16.4 (fork)

- **Cost observability**: every response's exact token usage (from the API's
  `response.done`) is logged with an estimated cost, and a
  `sensor.voicepe_<instance>_openai_cost_today` sensor tracks daily spend in
  Home Assistant. Rates auto-switch for mini models.
- Recommended default applied to our install: `max_output_tokens: 1200` —
  output audio is the dominant per-turn meter ($64/1M tokens, measured); a cap
  bounds runaway monologues without touching normal replies.

## 0.16.3 (fork)

- Documentation overhaul: marketing README, `docs/` (getting started,
  configuration reference, features, agent integration, FAQ); repository
  renamed to `voicepe-realtime` (old URLs redirect). `repository.json` now
  carries this project's identity (was still the upstream fork's).
- `enrollment_phrase` default is now "hey leonard" (matches the shipped
  default wake word); HA UI help text added for all fork options.

## 0.16.2 (fork)

- **Guaranteed report-back on long delegations**: ask_openclaw now sends the
  instance name as `room`; the bridge answers "still working" at 120s instead
  of killing the turn, and delivers the agent's eventual answer to that room's
  announce endpoint itself. Previously a >145s research task was reported as
  a failure by voice while the agent kept working with nowhere to deliver.

## 0.16.1 (fork)

- **`recall_memory` tool** (with `openclaw_url`): instant deterministic search
  of the agent's memory files via the bridge (`{"recall": query}` →
  `{"matches": [...]}`). Registered as the FIRST stop for personal/household
  recall; `ask_openclaw` becomes the deep fallback. Fixes recall being a
  40-80s agent turn that found or missed facts depending on phrasing.

## 0.16.0 (fork)

- **Announce endpoint** (`announce_port` + `announce_token` options): a LAN
  route back to the device for the household's external agent. POST
  `/announce {"message": "..."}` (bearer-authed) speaks the message through
  the device's guarded TTS lane — the same path timers use — so a delegated
  task ("research X") can report back by voice minutes later. Disabled unless
  both options are set; 503 when no device is connected.

## 0.15.1 (fork)

- **Direct OpenClaw escalation** (`openclaw_url` option): `ask_openclaw` now
  calls the bridge endpoint directly instead of going through HA's MCP server,
  whose hardcoded 60-second request timeout killed longer agent turns (deep
  memory recall, contact lookups). Direct calls get ~2.5 minutes. Unset, the
  MCP-script path is used unchanged. The speaker gate applies either way.
- (0.10–0.15.0 entries — speaker voice-prints, timers, enrollment v2, HA
  sensors, false-wake flagging, voice-instructed memory — are in git history.)

## 0.9.0 (fork)

- **Firmware-backed voice enrollment** (pairs with firmware commit 5095ed0+):
  the device enters a true enrollment mode — mic pinned open, wake/stop models
  disarmed, cyan breathing LED, 10-minute hard cap, center button as physical
  escape — while an automated audio coach (gpt-4o-mini-tts prompts, cached,
  pushed down the speaker lane on a fixed schedule) guides 25 varied wake-phrase
  repetitions plus 90 s of natural speech. Mic audio flows ONLY to the recorder
  during enrollment: OpenAI hears nothing, so no VAD commits, no forced
  responses, no cost, no conversation mechanics to fight. New options:
  `enrollment_phrase`, `enrollment_tts_voice`.

## 0.8.0 (fork)

- **Voice enrollment**: say "I want to teach you my voice" — the assistant runs
  a guided recording session (varied wake-phrase repetitions + natural speech)
  via the new `voice_enrollment` tool, capturing the raw device mic stream to
  `/share/voice-enrollment/<person>_<timestamp>.wav` (16 kHz mono, 15-minute
  safety cap, persists across rebuilds). One session yields wake-word training
  positives AND voice-print enrollment audio. Recordings are personal data and
  are not managed by the add-on beyond writing the file.

## 0.7.1 (fork)

- Speaker probe tuned for real device audio (live test found 3-7 voiced frames
  in actual speech vs 100+ on synthetic bench audio): YIN threshold 0.15 → 0.20
  with a moderate-periodicity argmin fallback, energy gate 0.15 → 0.08 of peak
  RMS, minimum voiced frames 12 → 8, capture window 2.5 s → 3.0 s. Synthetic
  bench unchanged (0% wrong on typical voices).
- Debug: when `enable_recording` is on, each probe capture is saved to
  `recordings/probe_*.wav` for offline threshold calibration.

## 0.7.0 (fork)

- **Speaker context v1**: optional voice-type (male/female) detection for a
  two-person household. On every wake the first ~2.5 s of command audio is
  classified by median pitch (pure numpy YIN, in-process, off the event loop;
  benched at 98.6% right / 0% wrong across 11 typical synthetic voices) and the
  verdict is injected into the Realtime session as a system item, so the
  assistant can address the speaker by name ("sir"/"ma'am") and hedge when
  uncertain. New options: `speaker_male_name`, `speaker_female_name` (both
  empty = feature off).
- **Speaker-gated tools**: `male_only_tools` (comma-separated tool names) are
  enforced below the model — gated tools return a polite refusal unless the
  last voice verdict is the male speaker. Fails closed on uncertain/stale
  verdicts. Convenience gating, not biometric auth.

## 0.6.0

> ⚠️ **This update has two parts — please update both:**
> 1. **This add-on** (the update you're installing now).
> 2. **The Voice PE firmware** — open **ESPHome Device Builder** and click **Update** (or **Install**) on your device.
>
> The device and the add-on use one shared protocol; updating only one half can cause odd behaviour.

A reliability and voice-control polish release.

**Stop word**

- **Saying "stop" now usually works on the first try.** The spoken "stop" could
  previously be answered by the assistant a moment later, so you sometimes had to
  repeat it; that follow-on reply is now cancelled, so a single "stop" is
  typically enough.
- **Saying "stop" during a web search returns the device to rest promptly** — the
  light ring no longer keeps showing the "replying" animation for several seconds.
- **Fewer accidental stops** on the assistant's own speech.
- The light ring briefly flashes **red** to confirm your "stop" was registered. *(firmware)*

**Reliability**

- **No more unresponsive sessions.** A silently dropped connection to OpenAI is
  now detected and repaired within seconds, instead of leaving the assistant deaf
  until a restart.
- **The roughly hourly reconnect now happens proactively during a quiet moment**,
  so it practically never interrupts a conversation.
- **Smart-home commands are no longer cancelled** if you keep talking while they run.
- The light can no longer get **stuck on "thinking"**, and long web searches get
  all the time they need.

**No more "answers out of nowhere"**

- The assistant no longer occasionally replies — or repeats its previous answer —
  right after the wake word when you said nothing.
- A sentence that got cut off is no longer answered minutes later on your next wake.

**Settings**

- New **"Wake mic delay"** setting: a short pause after the wake chime before the
  mic opens, so the chime can't be mistaken for speech (default 700 ms).
- The **"Follow-up mic delay"** default is now **700 ms**. Existing installs keep
  their saved value — raise yours if the assistant ever answers right after its
  own reply.

## 0.5.0

A big stable release: everything built and tested on the dev channel over the
past days. **Also update the Voice PE firmware** (v1.1.0 — one click in ESPHome
Builder) to get the full effect of the "stop" improvements; the two halves
work best together.

- **"Stop" now works through the whole reply AND the after-reply listening
  window.** The device detects the word more reliably, and the bridge treats
  it as authoritative: in-flight audio is discarded and an answer OpenAI had
  already started for the stop word itself is cancelled on arrival — no more
  "Okay, I'll be quiet" replies to your "stop".
- **Fixed: an answer could cut off mid-sentence, after which the assistant
  went deaf** until the next reconnect. Harmless protocol races (e.g. your
  sentence being split into two turns by a pause) no longer kill the session.
- **Fixed an audio race that could inject noise/hiss into replies** (firmware,
  paired with this release).
- **Mute behaves properly now** (firmware): the ring goes dark with red
  markers by the microphones, and muting also ends an open listening window
  immediately — both from Home Assistant and with the physical side switch.
- **The LED Ring switch in Home Assistant works again** (firmware): entity off
  = device dark at rest; entity on = the gentle "ready" pulse.
- **Completely reworked Configuration tab**: options grouped logically
  (Basics → Model & voice → Conversation → Web search → Audio →
  Home Assistant → Advanced), every description rewritten in plain practical
  language, and a full Dutch translation included (shown automatically when
  your HA is set to Dutch). Confusing or broken switches were removed; rarely
  needed expert fields stay hidden until you need them.
- **The add-on now has its own icon.**
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms. **Existing installs keep their saved values** — if
  yours still say 0, consider setting 200/150 manually (Conversation / Audio
  groups) for fewer ghost triggers and less crackle.

### Heads-up: the firmware stub template was improved

The per-device stub in ESPHome Builder used to reference the firmware in a
form that lets ESPHome **cache the downloaded YAML for a day** — clicking
Update shortly after a release could then silently rebuild yesterday's code.
The stub templates in the firmware repo are fixed; existing users can apply
the same fix once by replacing **only the `packages:` block** in their
device's YAML in ESPHome Builder (everything else — your name, secrets,
`dashboard_import` — stays exactly the same):

```yaml
packages:
  realtime:
    url: https://github.com/TristanBrotherton/voicepe-realtime-firmware
    ref: main
    files: [home-assistant-voice.realtime.yaml]
    refresh: 0s
```

Current templates for reference:
[esphome-builder.dhcp.yaml](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.dhcp.yaml) ·
[esphome-builder.static-ip.yaml](https://github.com/TristanBrotherton/voicepe-realtime-firmware/blob/main/esphome-builder.static-ip.yaml)

## 0.4.26

- **Web search is now ON by default**, using **gpt-5.5** (the best-quality search
  model), so the assistant can look things up online — weather, news, facts — out
  of the box. **Existing installs keep their saved setting**: if you had it off,
  switch `enable_web_search` on (and set `web_search_model` to `gpt-5.5`) in the
  add-on Configuration. The cheaper mini/nano models stay available.

## 0.4.25

- **Fix:** the first thing you said in the few seconds right after an automatic
  reconnect (e.g. after the 60-minute session cap) could be ignored
  (`conversation_already_has_active_response`). The reconnected session no longer
  creates a duplicate response, so that turn answers normally.

## 0.4.24

- **Renamed** to **OpenAI Realtime 2 Voice Agent**.
- Rewrote the store/info description and added a full **Documentation** tab
  (install steps, OpenAI key, Home Assistant MCP setup, recommended settings, web
  search, credits). Removed stale text from the original upstream client.
- Default system prompt is now an English, voice-tuned prompt (silent tool calls,
  varied confirmations, language pinning). Your own saved prompt is not changed.
- Default `follow_up_open_delay_ms` and `playback_prebuffer_ms` set to `0` (raise
  them if the device hears its own tail or you hear crackle).

## 0.4.23

- **Fix:** the 60-minute session cap sometimes left the session dead until a
  restart. It now reconnects automatically in all cases (both the keepalive-drop
  and the `session_expired` forms).

## 0.4.22

- **New options:** voice **speed** (0.25–1.5), **max reply length**
  (`max_output_tokens`), and **input noise reduction** (off / near-field /
  far-field). All default to current behaviour.

## 0.4.21

- Model, voice, web-search-model and transcription-model options are now
  **dropdowns** with the known-good values, each with a **custom** entry if you
  need a value not in the list.

## 0.4.20

- **New:** optional **web search**. Turn on `enable_web_search` to let the
  assistant look things up online (weather, news, facts). Uses your OpenAI key;
  off by default. Model configurable via `web_search_model` (default gpt-5.4-mini).

## 0.4.19

- Clarified the MCP option help text for both the built-in HA MCP Server and the
  unofficial ha-mcp add-on.

## 0.4.18

- **Fix:** removed a meaningless filler reply ("I'm ready to continue…") that could
  appear on the first turn of a session.

## 0.4.17

- **Fix:** cap restored conversation history (`max_context_messages`, default 12) to
  bound per-turn token cost and avoid hitting OpenAI's rate limit.

## 0.4.16

- **Fix:** the device no longer gets stuck blinking "thinking" after a turn-ending
  error (e.g. a rate limit) — it returns to idle so you can retry.

## 0.4.14

- **New:** `playback_prebuffer_ms` jitter buffer to reduce occasional crackle at the
  start of replies.

## 0.4.12 – 0.4.13

- **Fix:** "say stop, then immediately ask again → silence". Disabled the broken
  server-side audio truncation that wedged the next turn.

## 0.4.9 – 0.4.11

- **New:** auto-reconnect the OpenAI Realtime session when its connection drops
  (keepalive timeout / 60-minute cap), instead of going dead until a restart.
  Refined so a normal device disconnect doesn't trigger an unnecessary reconnect.

## 0.4.6 – 0.4.8

- **New:** configurable post-reply **follow-up listening window** (answer back
  without re-saying the wake word) + its open-delay, and per-option help text in the
  UI.
- **New:** the assistant's and user's transcripts are logged to the add-on log
  (`🤖 assistant:` / `🗣️ user:`).

## 0.4.0 – 0.4.4

- **Fix:** resample the device's 16 kHz mic to the 24 kHz OpenAI requires (garbled
  speech), and drop empty audio chunks.
- **New:** device **"stop"** interrupt now actually cancels the reply and clears
  buffered audio.

## 0.3.x

- Switched the target to **gpt-realtime-2**, pinned pipecat-ai 0.0.97, and tuned
  turn detection (semantic VAD), phase delivery to the device, and the startup
  sequence to stop double-responses. Made the disconnect tool and transcription
  model configurable.

## Earlier

- Initial pipecat + WebSocket implementation (forked from
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)).
