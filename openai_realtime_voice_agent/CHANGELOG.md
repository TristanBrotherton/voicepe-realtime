# Changelog

All notable changes to this add-on. Newest first.

## 0.19.24 (fork)

### Fixed: one device's failed Home Assistant call could cut off another's

- Every device shares one Home Assistant MCP session. When a call failed
  after it was sent, the session was closed at once, also under calls other
  devices still had running on it; those then reported "may or may not have
  completed" even when their action had worked. A failed session is now
  retired: new calls get a fresh session, and the old one closes once the
  calls still running on it have finished.
- A call whose session had already closed before it was sent was reported as
  "may or may not have completed", although nothing had reached Home
  Assistant. It is now retried once on a fresh session, and reported as "not
  reachable" if that fails too.

## 0.19.23 (fork)

### Fixed: a follow-up question went unanswered

- On GPT-Live, the follow-up window (when you can ask again without the wake
  word) started when the reply's transcript ended, not when the device
  finished playing it. On a slow Wi-Fi link the device was still speaking for
  several seconds, so the window had almost run out by the time you could
  answer, and a question asked after it went to a closed session. The window
  now waits for the reply's audio to finish playing, and restarts when the
  device reports that it has, so you always get the full window after hearing
  the end. A wrong estimate holds the session at most 15 s longer.

## 0.19.22 (fork)

### Fixed: an acknowledgement was cut off mid-word

- On GPT-Live, "One moment, <name>." could stop after "One mo-" when the
  result of a quick Home Assistant action came in, followed by a pause before
  the confirmation. Audio that arrived while the voice's words were still a
  complete acknowledgement now plays to the end; only what the voice says
  after it is held back or dropped.
- A household name still streaming in (half of it after "One moment,") no longer counts as
  a claim, so it no longer cuts the acknowledgement off.
- An acknowledgement the voice is in the middle of when the request is
  routed again is judged as one phrase, not from its last words alone.

## 0.19.21 (fork)

### Fixed: a new session restored from the cache resent old tool results

- On the Realtime runtime, a session started from the cached conversation
  resent every cached tool result with a call id the new session never
  issued, and the server answered `invalid_tool_call_id`. Tool calls and
  their results are now left out of the restored context; what the
  assistant said about them is kept. Reported by @novastate (PR #6).

## 0.19.20 (fork)

### Fixed: a false wake into room talk kept answering

- A transcript longer than 12 words in which the backend finds no request
  closes the session silently, right after a wake too and even after an
  acknowledgement played. Before, Home Assistant's parser could pick an
  intent out of the conversation, which overruled the backend and the device
  said "I couldn't tell which device you meant", then listened again.
- A short phrase the parser reads as a command still hears that it was not
  done, so a misheard command is not met with silence.
- Once the session is closing quietly, the fallback acknowledgement clip no
  longer plays.

## 0.19.19 (fork)

### Fixed: GPT-Live's native-language voices fell back to marin

- `nuri`, `juni`, `flitz` (German), `brise`, `sillage` (French), `nira`
  (Hindi), `harema`, `shida` (Japanese) and `noeul` (Korean) are in OpenAI's
  GPT-Live voice table but were refused as undocumented, so the session spoke
  with the server default. They are now accepted (#15, change by @GManzato).

## 0.19.18 (fork)

### Fixed: "thanks" after an answer kept the device listening

- Once a request has been answered, a follow-up that only thanks or signs
  off ("thanks", "okay, thank you", "that's all") closes the session
  silently instead of being routed. Before, the device kept listening, routed
  whatever the room said next and answered it with "Sorry, I didn't catch
  that."
- A follow-up after an answer in which the router finds no request also
  closes silently (it is the room talking), instead of "I didn't catch that".
  Before any answer, a request with no request found still gets "I didn't
  catch that", so a misheard command is not met with silence.

## 0.19.17 (fork)

### Fixed: a device asked for by a near-miss name or the wrong kind was "not found"

- Home Assistant's tools match a device by exact name and filter by the
  domain and room the caller passes. The router's backend guesses those from
  what it heard, so "Is the test switch on?" asked for domain `switch` when
  the entity is an `input_boolean` helper, matched nothing, and the user was
  told the switch was not exposed. Spelled-out letters ("P.E."), plurals,
  misspellings and a dropped word missed the same way.
- Before any Home Assistant tool call that names a device, the add-on now
  matches the spoken name against the exposed entities it already reads from
  Home Assistant (names, aliases, domains, rooms), by spelling, by a rough
  sound-alike key and by the spoken words being part of one entity's name.
  When one entity is clearly meant it passes that entity's own name and
  domain, and drops a room or floor the entity is not in. Logged as
  `🎯 <tool>: name ... -> ...`.
- Left alone: an exact name or alias that already fits, a room asked for with
  a kind of device ("kitchen lights" is still the kitchen's lights), a word
  several entities share, a lookalike of another kind of device unless it is
  a near-exact match (a switch and a toggle helper count as the same kind),
  and tools whose `name` is a list, timer or broadcast.

## 0.19.16 (fork)

### Fixed: a click at the start of every session counted as a reply

- GPT-Live opens every session's output stream with a lone click (peak about
  170 of 32767, then digital silence), whatever it hears. The add-on counted
  anything above near-zero as speech, so every wake logged "GPT-Live is
  replying without having transcribed a request" 0.4 s after the session
  opened. That one-per-session signal is also what tells the wake watch the
  model answered a request it never transcribed (0.19.14), so the click used
  it up before a real untranscribed request could. Outside a reply, audio now
  has to peak above 300 (about -41 dBFS) to start one; quieter audio is held
  and released ahead of the first word if a reply follows, or dropped as
  idle. Inside a reply nothing changes.

## 0.19.15 (fork)

### Fixed: an old conversation answered a new question

- Every GPT-Live session was seeded with the device's whole earlier
  conversation, however old. Asked "What time is it?" in the morning, the
  model repeated the previous evening's answer instead of the time the tool
  had just returned. A session opened more than 5 minutes after the last one
  closed now starts a new conversation; the system prompt is kept, and a
  follow-up within 5 minutes still carries over.

## 0.19.14 (fork)

### Fixed: a quiet or distant request got silence

- GPT-Live answers a quiet or distant request (a soft voice, a person a few
  metres away, a loudspeaker across the room) without ever transcribing it.
  The add-on opens a turn only on the transcript, so the reply was dropped and
  the user heard nothing until the turn closed 12 s after the wake. Two
  changes:
  - The add-on hears the request itself: when speech-level sound follows the
    wake and no transcript has arrived 4 s after it ends (or the model starts
    replying to it without one), the turn is closed and the device says
    "Sorry, I didn't catch that. Could you say it again?" instead of staying
    silent. Logged with a running count per device. The first second after
    the wake never counts as the request (the wake chime's leak). The watch
    listens at a lower bar than the follow-up hold (400 RMS, 3x the room,
    against 1500): the requests GPT-Live drops are the quiet ones.
  - Optional: `live_input_gain_max` (new, default 1 = off) brings the mic
    stream up to a transcribable level before it is sent, with a slow gain
    that follows the recent speech level, never attenuates and never exceeds
    the cap. Replaying device audio that was answered but not transcribed
    showed the same request four times louder transcribed every time. Off
    until it has been proven on a device; set it to 4 to try it.

### Fixed: a device disconnect mid-session was logged as a handshake timeout

- When a device's connection dropped during a GPT-Live session, cancelling its
  pipeline dropped the Live socket before the teardown closed the session, and
  the session supervisor saw a session that was not ready and called it
  `handshake timeout`, in the log line and the close counters. The gate now
  learns of the disconnect before the pipeline is cancelled and closes with
  `device disconnected`; a session that had started and then lost its socket
  for any other reason closes as `connection lost`. `handshake timeout` now
  means only that `session.started` never arrived.

### Fixed: the output recording did not hold what the device played

- With `enable_recording` on, output audio was recorded by a pipeline stage,
  so everything sent to the device around the pipeline was missing: spoken
  prompts, the fallback acknowledgement clip, announcements and timer lines.
  Output is now recorded at the device socket, for every audio frame that was
  actually sent, whichever path sent it. A frame for a device that has gone
  away is not recorded.
- The WAV header is kept current on every write, so a recording read while
  the add-on is still running shows everything recorded so far instead of
  claiming to be empty.


## 0.19.13 (fork)

### Fixed: GPT-Live rejected the opening audio of a session (turn ended on an error)

- The device streams its mic while no session exists (after a
  background-conversation close it waits for a wake word with the mic open).
  The add-on held that audio in the pre-roll buffer meant for the handshake
  and flushed up to 5 s of it at `session.started`, in one burst, ahead of the
  request itself. GPT-Live accepts input at 1.2x real time in bursts of at
  most 5 s, so it answered `input_audio_rate_limit_exceeded`, the turn ended
  on an error and the user was told the request failed (about every second
  wake on a device without a wake chime). Audio that arrives with no session
  open or opening is now dropped, the pre-roll holds only what arrives after
  the open (4 s at most), and every input append (pre-roll flush, reconnect
  replay, live stream) goes through one pacer that stays under the server's
  intake rate instead of being rejected.

## 0.19.12 (fork)

### Fixed: a late result for a closed session was sent into the next one

- A router result that landed after its GPT-Live session had closed (a slow
  tool, a turn the user walked away from) was queued and became the first
  event of the next wake's session, carrying a delegation id that session
  had never seen and text about a conversation that was over. Delegation-bound
  context is now dropped once its session has ended; session-wide context
  (the voice check) is still carried over.

### Changed: the log names a reply without a transcript

- When GPT-Live replies although it has transcribed no request in the session,
  the dropped audio is logged as a warning that says so. Measured by replaying
  device audio: a quiet or thin voice right after the loud wake chime is
  heard and answered but never transcribed, so the add-on's turn never opens
  and the user hears nothing; the old "no speech 12s after wake" was the only
  trace.

## 0.19.11 (fork)

### Fixed: a new question was sometimes never heard after a wake

- A session opened by a wake is seeded with the recent conversation. When
  that history ended on something other than the assistant's reply (a
  request the last session never answered, or a tool result read in after
  the reply), GPT-Live acted on it as soon as the session started: it re-ran
  the old request and spoke an answer nobody asked for (dropped by the
  add-on, so nothing was heard), and the new question was never transcribed.
  The history for a new session now ends on the last reply. Reconnects in
  the middle of a turn still carry the pending request.

## 0.19.10 (fork)

### Fixed: a follow-up spoken late in the window was cut off

- GPT-Live reports what you said only once you stop talking, and the device
  closes its follow-up window on a fixed timer that only a `listening` phase
  cancels. A follow-up begun late in the window was therefore cut
  mid-sentence, the half utterance dropped, and the session closed on the
  cut-off. The add-on now hears speech begin in the window itself (an energy
  onset over the mic stream, judged against the room's own floor), sends
  `listening` at once so the device keeps the mic open, and keeps the session
  under its ordinary idle bounds until the transcript arrives. If nothing is
  transcribed within 12 s the mic is released and the session closes a few
  seconds later, so background noise cannot keep a session running. New
  option `follow_up_hold_on_speech` (default on); `false` restores the fixed
  window. No firmware change: 1.3.1 already honours the phase.
- A follow-up cut-off that lands after speech was heard no longer closes the
  session; what was heard is still routed.

## 0.19.9 (fork)

### The bridge learns who spoke

- `ask_openclaw` requests carry `speaker` when an enrolled voiceprint matched
  on this wake (never the pitch-only guess). A bridge can use it to treat the
  turn as that person's; without it, the turn is the owner's.

## 0.19.8 (fork)

### Faster web lookups, shorter "still working" line

- A web search that answers is now the result: the backend no longer makes a
  second call only to rephrase it (about 2 s per lookup).
- Web search runs with low reasoning effort by default (`WEB_SEARCH_EFFORT`;
  "default" restores the model's own setting). It answered as well and up to
  1.5 s sooner in a comparison.
- When a request runs past the soft budget, the voice now says a few words
  ("Still checking.") instead of a long line offering to take other questions.
  The answer often arrived while that line was still playing and waited
  behind it.

## 0.19.7 (fork)

### Fixed: adding to a list whose name was misheard

- To-do lists are matched by exact name, but the name arrives through speech
  recognition: a short, unusual list name heard with a different spelling
  failed to match and the item was not added. List tools now use the one
  real list that is clearly closest, by spelling and by sound; curly
  apostrophes, a trailing "list" and plurals match too. When the backend
  passes several guesses as one name, they resolve only if they all point to
  the same list. Anything ambiguous is still left to Home Assistant.

## 0.19.6 (fork)

### Fixed: voice training

- Under the Live voice every guidance line was rendered when it was spoken
  (10-40 s, retried each time a render was not verbatim), so "Next, please"
  came minutes apart and a session could not finish. All lines are rendered
  at startup (retried until every line is ready) and checked again before a
  session records anything; a session never starts with a line missing.
- 30 repetitions from several spots: close up, across the room at a normal
  volume, where the speaker usually stands, turned away, quickly in passing.
- A name the user says ("voice training for Sam") is passed to the tool;
  voice identification is only used when nobody is named.
- Rebuilding a voice print keeps the previous one as a .bak file.

## 0.19.5 (fork)

### Fixed: wake trigger capture

- Accepts the 5 s pre-wake clip that firmware 36678d9 and later sends (the
  limit was 3 s, so the clip was discarded). The longer clip also holds an
  earlier attempt at the wake word that the model missed, which is the most
  useful audio for retraining it. Earlier firmware never sent a trigger clip
  at all (it announced the wake before copying the audio).
- Every way a wake's turn ends is now written to the wake log (answered,
  declined as not addressed to the assistant, no speech, interrupted,
  disconnected). Only the normal end of a turn was recorded, so a wake that
  nobody spoke after (the usual false wake) had no outcome, and wake-word
  training could not label it without someone saying "that was a false alarm".

## 0.19.4 (fork)

### Fixed: the speaker's name in router mode

- The fallback acknowledgement (played when the voice is silent 1.2 s after
  speech end) replaced the voice's own named acknowledgement, so an
  identified speaker no longer heard their name. Each configured speaker name
  now has its own pre-rendered clips ("Sure, one moment, <name>."), played
  when the voice check identified the speaker this wake.
- The router backend is told who the voice check identified, and whether by
  voice print or by pitch only, so "do you know who I am?" and requests that
  depend on the person are answered instead of refused as unverified.

## 0.19.3 (fork)

### Added: discussions by voice (off by default)

- `discussions_enabled` adds three generic tools (`list_open_discussions`,
  `open_discussion`, `propose_decision`) that talk to the agent bridge's
  `discuss` contract. Only an item's topic and question are said unless the
  owner asks for details; a decision is read back, staged after a clear yes,
  and confirmed by the owner by text, never final from voice.
- While a discussion is open the session gets discussion bounds
  (`discussion_max_session_s`, `discussion_quiet_s`), its spend reservation
  grows within the daily caps, the device listens longer after each reply
  (`discussion_follow_up_ms`), and the room-talk closes stand down.

### Fixed

- **Spoken prompts keep their first word.** The Live voice renderer placed
  each cached line by the transcript's clock, which drifts from the received
  audio; cached clips began mid-word ("Sure, one moment." was only "...ment",
  heard as a short hiss before an answer). The cut is now calibrated to the
  attempt's own audio and walked back to the speech's onset, and every cached
  Live line is rendered again.
- **A wake just as a session closes is no longer lost.** The close's forced
  idle followed the new wake and stopped the device's mic for the new turn.

## 0.19.2 (fork)

### Changed: answers are heard to the end; the agent keeps a conversation

- **A result spoken from the task list holds the session until it has been
  heard.** 0.19.1 did this for explanations only. Now every background result
  the voice is given (a slow lookup that outlived the soft budget, a late
  action report, an agent answer) keeps the session open while it is being
  spoken, bounded: only while audio is actually playing, never past the
  session's own maximum, and no idle time is added once the voice stops. A
  one-word answer counts as heard too (the floor is 0.6 s of audio, then
  quiet), so it is not re-delivered into the next conversation. A
  session closing under it (the device's follow-up cut-off) hands the result to
  the next route instead of losing it.
- **The agent bridge now honours the add-on's session key for ordinary
  questions.** The add-on sends `voicepe-<device>-<random>` and reuses it for
  follow-ups within 30 minutes from the same device (a fresh key only when a
  request is still in flight on it), so "and what about tomorrow?" continues
  the same agent session; before, the reference bridge kept one long-lived
  session per room. With `answer_only: true` the bridge drops its own
  "be thorough" lookup directive, which conflicted with the add-on's
  answer-only brief. Requests without a `session_key` behave as before.
  See `docs/agent-integration.md`.
- **Reference bridge: a late answer is a completed result.** Its announce of
  an answer that outlived the sync window now carries `ok: true` (and its
  error announce `ok: false`); without `ok` the add-on framed the answer as a
  request that was not completed (correct per contract, wrong for a bridge
  that had the answer).

### Fixed

- **One acknowledgement, not two.** When the static fallback clip ("One
  moment.") was still being sent and the result arrived, the voice's own
  acknowledgement was released after the clip: two in a row, the second
  starting as the first ended. The voice's pre-result speech is now held from
  the moment the clip starts.
- **Local Home Assistant recognition no longer stays "busy".** The intent
  socket's lock could stay taken (a timeout landing as the acquire completed),
  after which every turn waited 1.2 s and took the slower backend path. The
  lock is taken through a wait that releases a late acquire, and a lock held
  with no holder or past any real holder's bound is replaced with the socket.
- **A device class is dropped for an entity that has none.** "Turn off the
  test switch" (an `input_boolean`) failed when the model also sent
  `device_class: switch`.
- **Room conversation after a false wake closes the session silently.** People
  talking near the device kept a falsely woken session open until its 90 s cap
  while the voice answered the room. A session now closes without a reply when
  one unanswered request has been extended four times past 160 characters, or
  when a second utterance with no request in it arrives within 90 s (across
  sessions); after such a close only a wake word opens the next session.

## 0.19.1 (fork)


### Added: explain mode for the household agent (router mode)

"Can you explain how our bridge supports remote access?" is research, not a
lookup (builds on the 0.19.0 router mode). The backend now calls `ask_openclaw` with `mode: "explain"` for a
question about how the owner's own software, projects or systems work, are
designed or stand. The request to the bridge carries a read-only research
brief instead of the answer-only prefix (read the code and documents, change
nothing, return a spoken summary of 20-40 seconds plus a short text version), a
10-minute budget, and a session key that a follow-up within half an hour
reuses, so "what about the auth part?" continues the same agent session.

- The voice acknowledges at once, by name when the speaker is identified, in
  one of several rotating phrasings (it takes a while, I'll get back to you,
  a text summary is yours for the asking). When the route outlives the
  soft budget the acknowledgement replaces the generic "still working" line,
  so at most the fallback clip and one acknowledgement are heard.
- The answer comes back by voice (the open conversation, a short new session
  or an announcement in the Live voice) and is read in full, not briefly. The
  task holds no session and is not narrated while it runs.
- "Text would be great" after the acknowledgement (the new `deliver_by_text`
  tool, chosen by the backend) switches that task to a text through the
  bridge's verified iMessage sender. The add-on names the recognised speaker
  (`notification_recipient`); the bridge resolves it to one of its routes or
  its default owner route, so no option is needed (`live_task_imessage_to`
  is an optional override). An open conversation hears "Done, it's in your
  messages"; an idle device is not reopened for that. A text that cannot
  be delivered falls back to voice. The text is the agent's short `TEXT:`
  part (at most three plain sentences, trimmed by the add-on as well, no
  header); an explanation that was spoken in the conversation is never
  texted as well, and a text is the last route for one that could not be
  spoken.
- One explanation runs at a time (a second is refused by the bridge and the
  voice says so). An explanation that never reports back is closed after
  15 minutes.
- An explanation spoken into an open session counts as delivered only once
  it has been heard; a session closing under it (the device's follow-up
  cut-off) hands the result to the next route (a reopened session, an
  announcement) instead of losing it.
- A result being spoken into a session holds that session until it has been
  heard to the end (the device's follow-up cut-off and the add-on's own
  follow-up-quiet close both used to cut it one sentence in).
- Bridge contract: `mode`, `budget_s`; `/announce` and `/task-result` accept
  `ok` and `detail` (the write-up) for a task result, and a task's text is
  not cut at the bare lane's 600 characters. See `docs/agent-integration.md`.

## 0.19.0 (fork)

### Added: an experimental GPT-Live voice runtime (opt-in)

Nothing changes for existing installs: `voice_runtime` defaults to the
Realtime runtime. Setting `voice_runtime: live` (with
`live_acknowledge_gaps`) runs the device on OpenAI's GPT-Live voice instead.
See DOCS section 12, `docs/configuration.md` for every `live_*` option, and
`docs/releases/0.19.0.md` for the release notes.

- **Router mode (`live_router`, on by default inside Live).** The add-on, not
  the voice model, decides and runs every tool. Home Assistant's own intent
  engine is tried first (recognition only, safe device domains only); anything
  else goes to a Responses backend called directly with the full tool set.
  The voice speaks only results a tool verified; a success claim with no tool
  behind it is never played.
- **The backend knows the home.** Home Assistant's MCP `Assist` overview of
  exposed entities, plus the floor and area registries (with aliases), sits in
  the backend's cached prefix and is refreshed every 5 minutes and at wakes.
  Loose wording ("upstairs bedroom", "the kitchen") is matched to real names.
- **Room context from the satellite.** "Turn on the lights" acts on the room
  of the satellite that heard it, found from its network address and Home
  Assistant's device registry. A room Home Assistant fills in on its own is
  not trusted: the request is re-asked with the satellite's room, or handed
  to the backend.
- **Optional: expose new entities automatically (`live_auto_expose`, off by
  default).** Controllable entities Assist cannot see yet are exposed;
  helpers and scripts only when they are on a dashboard; locks never.
- **A parsed command is never ignored.** When Home Assistant recognised a
  command, the backend is told what it heard, and a "no request" verdict
  becomes a spoken "I couldn't tell which device you meant" instead of
  silence.
- **Confirmation for risky actions is unchanged:** locks, garages, gates,
  doors and alarms still need a spoken yes, and a state question never runs
  a control tool, including a yes/no question about a device whose name
  holds a household word ("Is boost heat downstairs on?"). What Home
  Assistant's parser heard reaches the backend as a hint, never as an
  instruction.
- **Long tasks.** A request that needs a slow tool (the OpenClaw agent, a web
  search) becomes a background task after a few seconds: the voice says so,
  the conversation can go on, and the answer is delivered when it lands (in
  the session, as an announcement, or through the OpenClaw bridge). House
  commands are not promoted at the short budget; they get up to 8 s first.
- **Cost controls.** GPT-Live bills per connected second, so a session opens
  only on a wake and closes after idle, follow-up and hard bounds. Daily
  session-minute and dollar caps refuse new sessions
  (`live_max_session_minutes_per_day`, `live_max_usd_per_day`); backend tokens
  count against the same cap.
- **Fast paths.** Plain chat can skip the pre-result hold
  (`live_fast_chat_model`); clock questions and person-location questions go
  straight to the matching tool; an optional household snapshot
  (`live_context_entities`) gives the backend current states.
- **Acknowledgements.** The voice may say only a closed set of waiting
  phrases before a result; a short fallback clip covers silence after 1.2 s.
  Clips start on a short silence with a lead burst, like replies, so a cold
  device no longer drops their first syllable. Once the clip has played, the
  voice's own acknowledgement is not played as a second one.

### Fixed (both runtimes)

- Action-gate and tool-result handling: a confirmation is bound to the exact
  prompt that asked for it; placeholder tool arguments are dropped before a
  call; tool outcomes are classified before anything is spoken.

### Tests

- 1,040 unit tests (the CI floor is now 1,000), including the router,
  household directory, room context, cost controls and long tasks, and a
  privacy check that keeps fixtures fictional.

## 0.17.3 (fork)

- **Fixed: exact named covers failing when the model supplies the wrong Home
  Assistant device class.** A unique entity match now corrects `device_class`
  before execution and before the safety check. A cover named Garage Door that
  Home Assistant exposes as a gate therefore still requires confirmation and
  no longer misses after the user confirms.

## 0.17.2 (fork)

- **Fixed: ordinary Home Assistant actions being mistaken for consequential
  ones when MCP namespaces tool names.** Namespaced intent tools are now
  classified by their canonical intent, so broad tool descriptions do not
  force a confirmation for routine lights, switches, or helpers.
- **Fixed: exact named targets failing when the model supplies the wrong Home
  Assistant domain.** A unique entity match now corrects the domain before
  execution and before the safety check. Ambiguous targets remain unchanged;
  a mislabeled lock is still recognized as a lock and requires confirmation.

## 0.17.1 (fork)

- **Fixed: a device reconnecting over its own half-open session.** When a
  device rebooted (an OTA update, a power blip) and reconnected before its old
  socket was noticed dead, the replacement pipeline reused the audio-recorder
  processors of the pipeline being torn down. With recording on, the old
  pipeline's frames and its CancelFrame then ran into processors that had not
  started: hundreds of errors, a RecursionError, and a cancel that hung for
  20 s. Every pipeline now gets its own recorder processors.
- **Fixed: a replaced connection's session coming back.** A late frame could
  restart the replaced connection's reconnect handler after teardown; at the
  next hourly refresh it re-opened an OpenAI session for the dead connection
  and sent `idle` to its closed socket. The connection's recovery and phase
  emitter are now stopped before its pipeline is cancelled and stay inert
  once closed.

## 0.17.0 (fork)

**Upgrade notes.** New options default to the previous behaviour or to the
conservative choice, with one deliberate exception: unlocking locks, opening
garage doors, gates and doors, and alarm-panel actions now need a spoken yes
(`confirm_actions`; empty it to opt out). Setting a `device_token` is
recommended. Firmware 1.3.0 (wake protocol 2) adds wake metadata and device
timings; older firmware keeps working.

- **Per-turn latency timeline**, correlated between the device and the add-on
  by turn id: wake, first mic audio, the server's end-of-turn decision
  (measured from OpenAI's own audio timestamps), first model audio, first
  audio sent, tools and the device's own timings. One `⏱️ turn` log line per
  turn (no words) and `sensor.voicepe_<instance>_latency` with rolling p50/p90.
- **The wake path no longer waits for Home Assistant.** Sensor updates go
  through one shared client and a background worker; daily counters survive
  restarts and count per device.
- **Wake-word observability**: every wake records the model, SHA-256 prefix,
  cutoff, window and sensitivity tier (`sensor.voicepe_<instance>_wake_word`).
- **False-wake labels you can trust**: flags label the exact device and turn
  (double-press, button during the wake, or voice within 30 s); presses made
  while offline are delivered later; silent wakes are only candidates, never
  training negatives; labeled clips have their own retention pool.
- **Privacy controls**: `wake_capture` (default: counters and metadata only),
  opt-in trigger audio that needs consent on both the add-on and the device,
  guest mode, retention limits, `log_transcripts` (off), and speaker
  identification no longer writes probe audio. A metadata-only weekly report
  (`python3 -m app.wake_events report`) counts wakes, flags and shadow-model
  detections per device.
- **Security**: optional device token with a migration mode and address
  allowlist; `/healthz` reports only counts unless authorized; spoken-yes
  confirmations for risky actions, enforced below the model.
- **Conversation**: a single "One moment." when a slow lookup is still running
  after about a second (fast commands never get one); failed turns you started
  are explained out loud; the speaking style and voice for timers,
  announcements and the enrollment coach are options instead of a hard-coded
  persona; English fallback messages.
- **Recovery**: a silent wake checks the OpenAI socket instead of always
  reconnecting; a request cut off by a dropped connection is replayed once the
  session is back; the Home Assistant MCP session is reused; web search is
  bounded (`web_search_timeout_s`).
- **Routing**: agent escalations carry the asking `device_id`, and
  announcements can target it, so report-backs play on the device that asked.
- **Tests and CI**: every test file is a discoverable unittest suite (194
  tests); CI scans the whole repository for private data and broken doc links
  and refuses a shrinking suite.
- **Docs and demo**: claims corrected (no unmeasured latency figures; memory
  notes are sent to OpenAI as part of the instructions; speaker gating is a
  convenience check), new guides for wake-word learning and every option, and
  a reproducible, measured demo with a simulated device (`demo/`).

## 0.16.12 (fork)

- Fixed missing spoken acknowledgements after a proactive Realtime reconnect.
  Historical tool-result IDs are now preserved as already delivered, preventing
  Pipecat from replaying an old result into the replacement conversation and
  triggering OpenAI's `invalid_tool_call_id` after the new action succeeded.
- Fixed a half-open Realtime recovery edge case where the device could remain
  in its local listening state after the backend reconnected. Forced recovery
  now retransmits `idle` even when the backend already cached that phase.

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
