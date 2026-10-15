# Agent Integration

Connect an external agent to the assistant for three superpowers:

1. **Memory recall** — answers from your agent's long-term memory without a full agent turn.
2. **Deep escalation** — questions and tasks the smart home can't handle itself.
3. **Voice report-back** — long-running work that announces its result in the room
   that asked.

**This project is built around [OpenClaw](https://openclaw.ai)** — the
open-source personal agent platform — and OpenClaw is what these features were
designed and tested against. OpenClaw brings the pieces a voice assistant can't:
months of long-term memory in plain markdown (which the recall path greps
directly), channels (iMessage, Telegram, WhatsApp, Discord, …) for
delivering details, scheduled jobs that can speak through the announce endpoint,
a browser for real research, and — with the
[openclaw-voice-call-realtime](https://github.com/TristanBrotherton/openclaw-voice-call-realtime)
plugin — a real phone.

**You don't need an agent.** Everything else — conversation, smart-home control,
timers, memory notes, speaker recognition, web search — works standalone. And the
contracts below are **agent-agnostic**: the option is named `openclaw_url` after
the agent this project runs, but anything that answers two simple POST shapes
works.

## OpenClaw in five minutes

**A ready-to-run reference bridge ships in this repo:**
[`examples/openclaw-bridge/`](../examples/openclaw-bridge/) — one dependency-free
Node file plus a setup README (secrets, service files for macOS/Linux, the
TOOLS.md snippet that teaches OpenClaw to announce, smoke tests). What it does:

- `{"question", "room", "device_id"}` → spawn `openclaw agent --agent main
  --session-key voicepe-<unique> --message "<voice directive> <question>"` and
  return its stdout as `answer`. The directive tells the agent to act on lookups
  and easily reversible tasks, to ask for confirmation before anything
  irreversible or security-sensitive (unlocking, payments, messaging someone
  new, deleting data), to reply in one spoken sentence, and to report long work
  back via the announce endpoint for the asking device.
- `{"recall"}` → grep OpenClaw's own memory files (`MEMORY.md`, recent
  `memory/*.md` dailies and person-files) and return matching lines. No agent
  turn — that's why recall is fast.
- If a turn outlives ~120 s, reply `{"answer": "Still working on that — I'll
  tell you when it's ready."}`, let the turn finish, and POST the eventual
  answer to the announce endpoint yourself, with the asking `device_id` —
  report-back becomes a guarantee, not a hope.

Teach OpenClaw the announce endpoint once (a short note in its workspace
`TOOLS.md` with the curl command and the per-room ports) and its own scheduled
jobs and long tasks can speak in the house too.

## The bridge contract

Set `openclaw_url` to an HTTP endpoint you control (a small "bridge" in front of
your agent). The add-on POSTs JSON to that one URL; the body shape selects the
operation:

### Escalation — `ask_openclaw`

```
POST <openclaw_url>
Content-Type: application/json

{"question": "Research flight prices to London for October", "room": "kitchen",
 "device_id": "kitchen-voice-pe", "request_id": "call_abc123"}
```

```
200 OK
{"answer": "Direct flights in October start around ..."}
```

- `question` — the user's request, with context the model added. Since
  0.19.0 it starts with a short answer-only prefix (fewest, most direct
  lookups; one to three spoken sentences; claim success only when it
  succeeded), then `Request:` and the request. `answer_only: true` says the
  prefix is there, so a bridge can drop any "be thorough" directive of its own.
- `session_key` — the device's conversation key, `voicepe-<device>-<random>`
  (0.19.2; 0.19.0 sent a fresh key per request). The add-on reuses it for
  follow-ups within 30 minutes from the same device, so "and tomorrow?"
  continues the agent session, and mints a fresh key only while a request is
  still in flight on it (no two runs on one session key, which would queue).
  Use it as the agent's session key. Accept only the shape
  `^voicepe-[A-Za-z0-9_-]{1,112}$`; if the key is already running, fall back
  to a session of your own. With `answer_only: true` drop any "be thorough"
  directive of yours; it conflicts with the add-on's answer-only brief.
- `room` — the add-on's `instance_name`, lowercased.
- `device_id` — the device that asked. Your bridge should remember it: if the
  work outlasts the voice turn, deliver the eventual answer through the
  [announce endpoint](#the-announce-endpoint) with that `device_id`, so it plays
  on the device that asked (one add-on instance can serve several devices).
- `request_id` — the current tool-call correlation id. Treat it as an
  idempotency key: matching retries must join or replay the original work, and
  reusing it with different content must fail closed.
- The add-on waits up to **145 seconds** for the answer. If your agent needs
  longer, have the bridge reply inside a short foreground budget with
  `{"accepted": true, "task_id": "<task_id>", "eta_s": 60}` (or the older
  `{"answer":"Still working…","background":true,"retry":false}`), keep the
  task running, and deliver the real result through
  [`/task-result`](#task-results) when it is done (an `/announce` carrying
  `announcement_id` = the request id works too). The voice tells the user it
  is still working and will report back; the conversation goes on meanwhile.
- `task_id` — equal to `request_id`; the id a late result must carry.
  `callback_url` (when the add-on has `OPENCLAW_CALLBACK_URL`) names the
  add-on's `/task-result` route; otherwise derive it from the announce host and
  port.
- A tool that *arms a watch* ("tell me when X leaves work") should answer
  `{"deferred": true, "task_id": "<task_id>"}` at once and post the event to
  `/task-result` when it fires. The add-on closes the voice session while it
  waits (no billed idle time) and tells the user by voice when it happens,
  outside quiet hours.
- The model is instructed to use this only for things Home Assistant can't do
  itself (memory, messaging, calendar, research, cross-app tasks) — never for
  smart-home control.

#### Explain mode (0.19.1, router mode)

A question about how the owner's own software, projects or systems work, are
designed or stand ("explain how our bridge supports remote access", "what's
the status of project X") is research, not a lookup. The backend calls
`ask_openclaw` with `mode: "explain"`, and the request to the bridge changes:

```
{"question": "<explain brief>\n\nRequest: Can you explain how ...", "mode": "explain",
 "budget_s": 600, "answer_only": false, "session_key": "voicepe-<device>-explain-<random>",
 "room": "kitchen", "device_id": "...", "request_id": "call_abc123", "task_id": "call_abc123"}
```

- The brief replaces the answer-only prefix. It is **read-only** (investigate
  and explain; change no code, files, settings or devices; send nothing) and
  asks for a reply in two parts: `SPOKEN:` a summary of 20-40 seconds of
  speech, written for the ear, and `TEXT:` the same answer as a text message
  (at most three plain sentences, under 300 characters, no file paths or
  function names unless asked). Each marker starts a line. The add-on trims
  the text part to a few sentences anyway, and still reads the older
  `WRITEUP:` marker.
- `budget_s` is how long the add-on expects the agent may take (600). The
  bridge should accept the request **at once** with `{"accepted": true,
  "task_id": "<task_id>", "eta_s": 180}` and run the turn in the background;
  the add-on's voice has already told the user it will take a moment.
- `session_key` is **reused** by a follow-up within 30 minutes from the same
  device ("what about the auth part?"), so the agent keeps the context it has
  read. Run one explanation at a time: answer `409` while one is running and
  the voice says it is still on the earlier one.
- Deliver the result with `/task-result` or `/announce` (`announcement_id` =
  the request id) carrying `"ok": true`, the `SPOKEN:` part as the message
  and the `TEXT:` part as `"detail"`. A task result may be up to 1500
  characters (`detail` 4000); the bare announcement lane keeps its 600. On
  failure post `"ok": false` with a short message.
- Delivery is the user's choice: by default the summary is read in full by
  voice on the device that asked; if the user answers the acknowledgement
  with "text would be great", the add-on texts the short `TEXT:` part (never
  an explanation that was already spoken in the conversation) through the
  bridge's notification contract with `notification_recipient` = the
  identified speaker's first name (no destination). The bridge matches the
  name against its own iMessage routes and otherwise uses its default owner
  route (`IMESSAGE_DEFAULT_DESTINATION`, else the first route in its routes
  file). `LIVE_TASK_IMESSAGE_TO` is only an optional override naming one route
  exactly. If the conversation is still open the voice says something like
  "Done, it's in your messages"; an idle device is not reopened for that.
- A bridge without explain support may ignore `mode` and answer inline: the
  add-on speaks the `SPOKEN:` part of such a reply.

When `openclaw_url` is set, the add-on registers `ask_openclaw` natively and
skips any same-named tool from the MCP server, so the model sees exactly one.
This matters: Home Assistant core hard-caps every MCP request at **60 seconds**,
which kills longer agent turns — the direct route is what gives you the
2.5-minute budget. Speaker gating (`male_only_tools`) applies to the direct
route exactly as it does to MCP tools.

### Memory recall — `recall_memory`

```
POST <openclaw_url>
Content-Type: application/json

{"recall": "Grandma phone number"}
```

```
200 OK
{"matches": ["Grandma: +1 555 0123 (landline, prefers calls after 10am)"]}
```

- Return an array of matching lines from your agent's memory/notes files —
  a plain deterministic text search is exactly right. Speed is the point:
  this is the assistant's **first stop** for "what's X's number" / "when is Y's
  birthday" / "what did we decide about Z", so keep it a search, not an agent
  turn.
- Return `{"matches": []}` when nothing matches — the assistant then falls back
  to a full `ask_openclaw` turn.

## The announce endpoint

The reverse direction: your agent (or anything on your LAN) speaks in the room.

Enable it by setting **both** `announce_port` and `announce_token` in the add-on.

```
POST http://<ha-host>:<announce_port>/announce
Authorization: Bearer <announce_token>
Content-Type: application/json

{"message": "Your London flight research is done: direct flights start at ...",
 "device_id": "kitchen-voice-pe", "announcement_id": "call_abc123"}
```

`device_id` is optional. With it, the message plays on that device (or fails
with `503` if it is offline — it never falls back to another room). Without it,
the device used most recently speaks.

`announcement_id` is the originating `request_id`. Retries with the same id
are accepted but spoken once. Distinct ids are distinct results even when their
wording is similar; never use fuzzy text matching to suppress announcements.

Responses:

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status": "announced", "device_id": ...}` | Spoken on the device |
| `200` | `{"status": "duplicate_suppressed"}` | This exact announcement id (or exact legacy text) was already spoken — do not retry |
| `400` | `{"error": ...}` | Invalid JSON or empty message |
| `401` | `{"error": "unauthorized"}` | Bad/missing bearer token |
| `503` | `{"error": "no device connected"}` / `"device not connected"` | Device offline — **fall back to a text channel** |
| `503` | `{"error": "announcement failed"}` | The device did not play it |
| `500` | `{"error": "announcement failed"}` | Internal error while playing |

Details:

- Messages are capped at **600 characters** — keep report-backs summary-sized.
- Playback uses the device's **guarded TTS lane** (the same path timers use), so
  the assistant can't hear its own announcement and reply to it.
- The add-on runs on the **host network** — the bearer token is the only lock.
  Generate a long random one and treat it as a secret.
- One endpoint per add-on instance. When one instance serves several devices,
  pass the `device_id` from the escalation; with one instance per room, the
  `room` field tells your agent which URL to use.

### Task results

```
POST http://<ha-host>:<announce_port>/task-result
Authorization: Bearer <announce_token>

{"task_id": "call_abc123", "answer": "Flights to London in October start around…",
 "ok": true, "device_id": "kitchen-voice-pe"}
```

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status": "accepted", "task_id": ...}` | Delivered into the conversation, or by voice on the device when no session is open |
| `200` | `{"status": "duplicate_suppressed"}` | Already delivered — do not retry |
| `400` | `{"error": ...}` | Missing `task_id` or empty `answer` |
| `404` | `{"error": "unknown task"}` | Nothing waits for that id (delivered, expired, or the add-on restarted): fall back to `/announce` |

A failure: `{"task_id": "...", "ok": false, "error": "short reason"}`; the user is
told the request was not completed, never that it was done. Retries with the
same `task_id` are idempotent.

Delivery order when no session is open is configurable (`LIVE_TASK_DELIVERY`,
default `session,reopen,announce,imessage`; `LIVE_TASK_DEFERRED_DELIVERY`,
default `session,announce,reopen,imessage`): a short new voice session opened
through the spend gate, the announcement lane, then a text through the bridge's
notification contract (in the fallback chain only when `LIVE_TASK_IMESSAGE_TO`
names a route; an explanation the user asked to have texted needs no option) —
the text comes first only when the user asked for one. `LIVE_TASK_QUIET_HOURS` (`22:00-07:00`)
holds a deferred result until the morning.

## The full delegation loop

Putting it together — what a long-running task looks like end to end:

```
You (kitchen): "Research flight prices to London for October."
  └─ add-on → bridge: {"question": "...", "room": "kitchen", "device_id": "...", "request_id": "..."}  (ask_openclaw)
       └─ agent starts working…
  ┌─ bridge → add-on at its short deadline: {"accepted": true, "task_id": "..."}
Assistant: "I'm still looking into it — I'll let you know." (you can keep talking meanwhile)
       └─ agent finishes (90 seconds later, or 50 minutes)
  ┌─ agent → /task-result: {"task_id": "...", "answer": "Flights to London in October…"}
Device (kitchen): the open conversation hears it; or a short new voice session /
                  an announcement says it; or (device unreachable, or you asked
                  for a text) the bridge texts you.
```

Fast tasks never touch the announce path — the answer just comes back inside the
voice turn.

## Checklist

1. Stand up a bridge endpoint that answers the two POST shapes above.
2. Set `openclaw_url` to it in the add-on configuration.
3. Set `announce_port` + `announce_token`; give your agent the URL and token
   (one pair per room/instance).
4. Optionally gate escalation to a specific speaker via `male_only_tools`.
5. Prove the direct bridge contract with a bounded POST and record its latency.
   This validates HTTP/auth/process behavior, but is not end-to-end acceptance.
6. Before a release or deployment, run one authorized real-audio test: an
   external speaker says the wake phrase and an `ask_openclaw` request into a
   real Voice PE microphone, and the final agent answer must be audible on that
   device's physical speaker.
7. Correlate that physical turn end to end. Require exactly one tool call, a
   successful bridge result, an admitted final reply after any acknowledgement,
   the exact current-turn output-batch drain acknowledgement, return to idle,
   no active paid session, and restoration of the injection host's output,
   volume, and mute state.
8. Keep the detailed evidence private if it identifies a home, person, room,
   device, host, or transcript. The release record should contain only a
   redacted evidence id, candidate hashes, timing, and pass/fail outcome.
9. Finally, say something only your agent would know — then delegate something
   slow and walk away to verify report-back.

A bridge returning `200` proves only the middle of the path. It does not prove
that the model invoked the tool, that the result produced a final reply, or that
the device played and drained it. Those claims require the real-audio test.

## Latency architecture

`ask_openclaw` is intentionally the deep path, not the fast smart-home path.
Transport startup may be measurable, but agent state loading, model scheduling,
tool work, and a second voice-model pass can dominate the spoken critical path.
Measure each stage instead of assuming the CLI process is the bottleneck. Keep
ordinary home control in Home Assistant and personal fact lookup in
`recall_memory`.

For a lower-latency production bridge:

- keep one authenticated bridge process connected to the agent runtime instead
  of spawning a CLI process per request;
- reuse the transport and a bounded per-room agent session, rotating it by age
  and turn count, while preserving a correlation/idempotency id per voice turn;
- stream structured progress so the device can acknowledge work locally;
- give synchronous voice work a short deadline (roughly 5–8 seconds), then
  return a deterministic "I'll report back" response, close the metered voice
  session, and deliver the eventual result through `/announce`; and
- keep deterministic operations (memory search, status reads, routing) outside
  the general agent turn entirely.

Do not reduce latency by weakening final-reply admission, device-drain proof,
tool confirmation, or session/spend bounds.
