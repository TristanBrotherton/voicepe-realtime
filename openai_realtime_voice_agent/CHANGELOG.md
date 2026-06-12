# Changelog

All notable changes to this add-on. Newest first.

## 0.5.6 (dev channel)

- **The "Follow-up mic delay" default went from 200 to 700 ms.** At 200–300 ms
  the reply's own speaker tail could leak into the freshly opened follow-up
  microphone and be mistaken for a question — the assistant would then answer
  "nobody" or repeat its previous answer (observed live). 700 ms covers the
  tail safely; existing installs keep their saved value, so raise yours
  manually if you see the same symptom.

## 0.5.5 (dev channel)

- **The assistant no longer spontaneously answers an old, half-finished
  sentence the moment you say the wake word.** When the microphone closed
  mid-sentence (for example because the follow-up window expired), the cut-off
  audio stayed behind in OpenAI's input buffer and was "completed" minutes
  later by the next wake — the assistant then immediately replied to it. The
  buffer is now cleared whenever the microphone resumes after a pause, and on
  every device (re)connect. Your conversation memory is unaffected.

## 0.5.4 (dev channel)

- **Long web searches now get all the time they need.** While a tool call is
  running (a web search on a hard question can legitimately take a while) the
  stuck-"thinking" watchdog from 0.5.3 waits indefinitely instead of cutting
  in after 60 seconds. The 15-second dead-turn detection still applies the
  moment nothing is running anymore.

## 0.5.3 (dev channel)

- **The device can no longer get stuck "thinking".** When a turn died without
  an answer (for example on an OpenAI rate limit during rapid-fire commands),
  the LED could keep blinking "thinking" with the microphone left open — until
  you woke the device again. A watchdog now declares such a turn dead after
  15 seconds of model silence and returns the device to rest, and the
  rate-limit recovery itself no longer loses the race against late
  voice-detection events (the cause of one observed 44-second hang). Slow
  tools such as web search are explicitly exempt and keep their time.

## 0.5.2 (dev channel)

- **No more deaf sessions.** If the connection to OpenAI silently died (network
  blip, quiet server-side close), the assistant could sit unresponsive for
  hours and your first question after that was lost. The connection's death is
  now detected the moment it happens and repaired within seconds.
- **The 60-minute reconnect now happens proactively while everything is
  quiet** (session older than 55 minutes + a minute of silence), so it
  practically never interrupts a conversation anymore.
- **Smart-home commands are no longer cancelled by your own voice.** Continuing
  to talk while a command was executing made the assistant abort the call and
  claim the action failed — while it actually succeeded. Tool calls now always
  finish and report the real result.

## 0.5.1 (dev channel)

- Version sync after the stable **0.5.0** release: the dev channel contains the
  same content plus the dev-only diagnostics (log_level/DebugFrameLogger).
  No functional changes.

## 0.4.32 (dev channel)

- The add-on now has its own icon and logo (a stylised Voice PE), shown in
  the add-on store and on the add-on page.

## 0.4.31 (dev channel)

- **Fixed: an answer could cut off mid-sentence and the assistant then went
  deaf.** When your sentence was split into two turns (a pause at the wrong
  moment), OpenAI rejected the second answer attempt and the session's reader
  stopped — killing the answer that was playing and ignoring everything after
  it. Such harmless protocol races are now ignored and the playing answer
  simply continues.

## 0.4.30 (dev channel)

- The four "custom" text fields (model / voice / search model / transcript
  model) no longer clutter the page: they stay hidden until you need them
  (pick "custom" in the dropdown, then enable "Show unused optional
  configuration options" at the bottom).
- The system-prompt field now explains how to edit it comfortably: the ⋮ menu
  → "Edit in YAML" gives a full text editor (the add-on UI itself only
  supports single-line fields).

## 0.4.29 (dev channel)

- **Cleaner Configuration tab.** Options are now grouped and ordered (Basics →
  Model & voice → Conversation → Web search → Audio → Home Assistant →
  Advanced → Debug) and every description is rewritten in plain, practical
  language. A full Dutch translation is included (shown automatically when
  your HA is set to Dutch).
- Confusing or broken switches were removed (they are now fixed at their only
  sane value); the legacy server_vad fields moved to Advanced and stay hidden
  until you enable "Show unused optional configuration options".
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms (existing installs keep their own values).

## 0.4.28 (dev channel)

- **"Stop" during the after-reply listening window now actually stops.** The
  open mic had already streamed the word to OpenAI, which answered it as a
  question. The bridge now treats the device's stop as authoritative: it
  discards the in-flight audio and instantly cancels any answer OpenAI
  started for it. (Pair with the dev firmware update, which also makes the
  local "stop" detection more sensitive.)

## 0.4.27 (dev channel)

- **Dev diagnostics:** new `log_level` option (the dev add-on defaults to
  `DEBUG`). At DEBUG the add-on logs the full turn / audio / interruption
  lifecycle (`🎚️` markers), the OpenAI session age at a drop + the reconnect gap
  duration, and surfaces the `websockets` connection events. Stable stays at INFO.
  Helps diagnose connection blips, stop-word timing, and stale-audio-after-stop.

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
