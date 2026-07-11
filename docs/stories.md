# Stories

What households actually do with Voice PE Realtime — and, for each one, exactly
which documented feature makes it work. Nothing here is aspirational: every
story traces to a feature guide or an endpoint contract.

Stories marked **needs an agent** use the optional
[agent integration](agent-integration.md) — one URL and two POST shapes, any
agent. Everything else works out of the box.

---

## Instant household knowledge

> "What's the wifi password?"
> "What size shoes does Sam wear?"
> "Where did we put the spare key?"

Anything the household has taught it by voice — *"remember the wifi password
is…"*, said once — is folded into its standing instructions, so the answer comes
back instantly, with no lookup at all. Notes live in a plain markdown file on
your box, and only identified household voices can add or remove them.

*Built in — [Voice-instructed memory](features.md#voice-instructed-memory).*

> "When's Grandma's birthday?"
> "What's the plumber's number?"

Facts nobody explicitly taught the assistant — but your agent knows — come back
in **under a second** via `recall_memory`: a deterministic search of the agent's
memory files, tried first for every personal-recall question, read straight back.

*Needs an agent — [Instant recall & agent escalation](features.md#instant-recall--agent-escalation).*

## Decisions and history, not just facts

> "What did we decide about the fence contractor?"
> "What was that restaurant we liked in Lisbon?"

Household history is recall too. The fast path searches the agent's memory
lines; if nothing relevant comes back, the assistant automatically escalates the
full question to the agent (`ask_openclaw`), which can dig through months of
context — with a 2.5-minute budget instead of Home Assistant's 60-second MCP
cap.

*Needs an agent — [Agent Integration](agent-integration.md#the-bridge-contract).*

## Hands-free errands

> "Text Sam we're running ten minutes late."

Hands covered in flour, kid on hip — the request goes to your agent as an
escalation, and the agent sends the message through whatever channels it has.
The assistant confirms out loud when it's done.

*Needs an agent (with a messaging channel) — escalation is explicitly for
"things Home Assistant cannot do itself: calendar, messaging, phone calls,
web knowledge, cross-app tasks".*

> "Call the pharmacy and ask if my prescription is ready, then tell me what
> they say."

With [OpenClaw](https://openclaw.ai) and its
[voice-call plugin](https://github.com/TristanBrotherton/openclaw-voice-call-realtime)
— real outbound phone calls via Twilio + OpenAI Realtime, with in-call tools
and transcripts — the delegation loop closes this entirely by voice: the
assistant acknowledges, OpenClaw places the call and has the conversation, and
the answer is **announced in the room you asked from** when the call ends. If
the device is offline at that moment, OpenClaw gets a `503` from the announce
endpoint and falls back to texting you.

*Needs OpenClaw (or any agent that can place calls) with the
[voice-call plugin](https://github.com/TristanBrotherton/openclaw-voice-call-realtime) —
[Long-running task delegation](features.md#long-running-task-delegation).*

## Work that takes as long as it takes

> "Research flights to Tokyo in October and text me the three best options."

The assistant hands the task over and tells you it's on it. If the work outruns
the voice turn (~2 minutes), the bridge answers *"still working"* and the turn
ends gracefully — no timeout error, no standing at the speaker. The agent
browses for as long as it takes, then delivers: spoken in your room via the
announce endpoint, or — as asked here — texted. The request carries the room
name, so the report-back finds the device you asked from.

*Needs an agent — [the full delegation loop](agent-integration.md#the-full-delegation-loop), step by step.*

## A voice for your automations

> 7:45 am: "Leave in fifteen minutes for the school run."
> "Your package was delivered."

The announce endpoint isn't reserved for task report-backs — it accepts **any
authorized POST** on your LAN. That makes it a general "speak in this room" API:
an agent's scheduled morning briefing, a Home Assistant automation, a cron job —
anything that can send one bearer-authed JSON request can speak through the
device's guarded TTS lane (the assistant can't hear itself and reply). To be
clear: the add-on doesn't schedule or originate these announcements itself —
your agent or automation does.

*Needs an agent or an automation you write — [the announce endpoint](agent-integration.md#the-announce-endpoint).*

## Kitchen flow

> "Add everything for lasagna to the shopping list."
> "Set a pasta timer for 9 minutes."

Lists are native Home Assistant tools through the MCP Server integration —
instant, no agent involved. Timers are personal: nine minutes later it says
*"Alex, your pasta timer is done"* to whoever set it (speaker recognition),
waits 20 seconds for any sign of life, and only then rings a gentle bell —
dismissed with "stop" or the button.

*Built in — [Getting Started §1.3](getting-started.md#13-let-it-control-your-home-home-assistant-mcp),
[Voice timers](features.md#voice-timers),
[Speaker recognition](features.md#speaker-recognition--voice-enrollment).*

## Teaching it, one sentence at a time

> "Remember the pool gate code is on the whiteboard."
> "From now on, use Celsius."
> "Remember that we park at the north lot."

One sentence, permanent. Notes take effect from the next session, are attributed
to whoever said them, and are listed back on request ("what do you remember?").
Guests can't rewrite your house rules — memory writes are speaker-gated below
the model.

*Built in — [Voice-instructed memory](features.md#voice-instructed-memory).*
