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

{"question": "Research flight prices to London for October", "room": "kitchen", "device_id": "kitchen-voice-pe"}
```

```
200 OK
{"answer": "Direct flights in October start around ..."}
```

- `question` — the user's request, with context the model added.
- `room` — the add-on's `instance_name`, lowercased.
- `device_id` — the device that asked. Your bridge should remember it: if the
  work outlasts the voice turn, deliver the eventual answer through the
  [announce endpoint](#the-announce-endpoint) with that `device_id`, so it plays
  on the device that asked (one add-on instance can serve several devices).
- The add-on waits up to **145 seconds** for the answer. If your agent needs
  longer, have the bridge reply at ~120 s with a "still working on it — I'll
  report back" style `answer`, keep the task running, and deliver the real result
  via announce when it's done.
- The model is instructed to use this only for things Home Assistant can't do
  itself (memory, messaging, calendar, research, cross-app tasks) — never for
  smart-home control.

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
 "device_id": "kitchen-voice-pe"}
```

`device_id` is optional. With it, the message plays on that device (or fails
with `503` if it is offline — it never falls back to another room). Without it,
the device used most recently speaks.

Responses:

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status": "announced", "device_id": ...}` | Spoken on the device |
| `200` | `{"status": "duplicate_suppressed"}` | A near-identical message was already spoken in the last 10 minutes — do not retry |
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

## The full delegation loop

Putting it together — what a long-running task looks like end to end:

```
You (kitchen): "Research flight prices to London for October."
  └─ add-on → bridge: {"question": "...", "room": "kitchen", "device_id": "..."}  (ask_openclaw)
       └─ agent starts working…
  ┌─ bridge → add-on at ~120 s: {"answer": "Still working on it — I'll report back."}
Assistant: "I'm still looking into it — I'll let you know."
       └─ agent finishes (5 minutes later, or 50)
  ┌─ agent → announce endpoint: {"message": "Flights to London in October…", "device_id": "..."}
Device (kitchen): speaks the result out loud.
       └─ (503? The agent texts you the details instead.)
```

Fast tasks never touch the announce path — the answer just comes back inside the
voice turn.

## Checklist

1. Stand up a bridge endpoint that answers the two POST shapes above.
2. Set `openclaw_url` to it in the add-on configuration.
3. Set `announce_port` + `announce_token`; give your agent the URL and token
   (one pair per room/instance).
4. Optionally gate escalation to a specific speaker via `male_only_tools`.
5. Say something only your agent would know — then delegate something slow and
   walk away.
