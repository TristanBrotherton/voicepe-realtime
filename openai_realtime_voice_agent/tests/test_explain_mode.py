"""Explain mode (0.18.36): "explain how our X supports Y" is minutes of
read-only research by the household's agent, answered by VOICE on the device
that asked, or by a text when the user says so after the acknowledgement.

What is pinned here:

* the backend chooses the mode (``ask_openclaw`` ``mode: "explain"``), no
  regex: the request to the bridge carries the research brief instead of
  the answer-only prefix, ``mode``, a budget, and a session key that a
  follow-up within half an hour reuses (the agent keeps its context);
* the explain brief is read-only and asks for a spoken summary plus a
  write-up; the bridge's "one at a time" refusal (409) is a plain error;
* the acknowledgement is the owner's wording, spoken at once with the
  identified speaker's first name (none when unknown), and offers a text;
* the task holds no session and is not narrated; its result is read in full
  (it was written for the ear); ``detail`` (the write-up) rides along from
  ``/announce`` and ``/task-result``;
* "text would be great" (the ``deliver_by_text`` tool) switches that task
  to a text through the bridge's verified sender, with a short spoken
  confirmation; a failed text falls back to the voice;
* the chat fast path is vetoed while an explanation is pending, so the
  user's answer reaches the router.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession

import app.live_service as live_service_module
from app import announce_http, live_tasks, openclaw_tool
from app.live_router import LOOKUP, PENDING, ROUTER_BACKEND_INSTRUCTIONS, RouteResult, ToolRun
from app.live_tasks import (
    IMESSAGE, SESSION, TaskPolicy, explain_ack_commentary, result_commentary, spoken_result, text_message_body,
)
from app.openclaw_tool import (
    EXPLAIN_BUDGET_S, EXPLAIN_MODE, EXPLAIN_PREFIX, bridge_request_fields, explain_session_key,
    get_openclaw_tool_definition, get_text_delivery_tool_definition, pending_result, split_explain_reply,
)
from app.tool_results import classify_result
from tests.test_live_router import TOOLS, FakeBackend, Patched, call, message, response
from tests.test_live_tasks import ASK, AskHarness, acked, commentaries, until

TEXT_TOOL = get_text_delivery_tool_definition()


class FakeLLM:
    def __init__(self):
        self.handlers = {}

    def register_function(self, name, handler):
        self.handlers[name] = handler


class Params:
    def __init__(self, arguments, call_id="rcall_7"):
        self.arguments = arguments
        self.tool_call_id = call_id
        self.results = []

    async def result_callback(self, result):
        self.results.append(result)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeClient:
    posted = []
    reply = {}
    status = 200

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        FakeClient.posted.append(json)
        return FakeResponse(FakeClient.reply, FakeClient.status)


ENV = {"OPENCLAW_URL": "http://agent.invalid/ask", "INSTANCE_NAME": "workshop"}


# ---------------------------------------------------------------------------
# the request to the bridge
# ---------------------------------------------------------------------------

class ExplainRequest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        openclaw_tool.forget_explain_sessions()
        FakeClient.posted = []
        FakeClient.reply = {}
        FakeClient.status = 200

    def test_the_tool_offers_the_mode_and_the_backend_is_told_when_to_use_it(self):
        tool = get_openclaw_tool_definition()
        self.assertEqual(tool["parameters"]["properties"]["mode"]["enum"], ["answer", "explain"])
        self.assertIn("explain", tool["description"])
        self.assertNotIn("mode", tool["parameters"]["required"], "the default stays the plain answer")
        self.assertIn('mode "explain"', ROUTER_BACKEND_INSTRUCTIONS)
        self.assertIn("deliver_by_text", ROUTER_BACKEND_INSTRUCTIONS)
        self.assertEqual(TEXT_TOOL["name"], "deliver_by_text")

    def test_the_explain_request_carries_the_brief_the_mode_and_the_budget(self):
        fields = bridge_request_fields("How does our bridge authenticate?", "workshop", "rcall_1", EXPLAIN_MODE)
        self.assertTrue(fields["question"].startswith(EXPLAIN_PREFIX))
        self.assertTrue(fields["question"].endswith("How does our bridge authenticate?"))
        self.assertEqual(fields["mode"], "explain")
        self.assertEqual(fields["budget_s"], EXPLAIN_BUDGET_S)
        self.assertFalse(fields["answer_only"], "no answer-only limit on research")
        self.assertRegex(fields["session_key"], r"^voicepe-workshop-explain-[0-9a-f]{10}$")
        plain = bridge_request_fields("What's the time in Tokyo?", "workshop", "rcall_2")
        self.assertNotIn("mode", plain, "a request without a mode is exactly what it was")
        self.assertTrue(plain["answer_only"])

    def test_the_brief_is_read_only_and_asks_for_a_spoken_summary_and_a_short_text(self):
        for phrase in ("READ-ONLY", "Do not change code", "do not send messages", "SPOKEN:", "TEXT:",
                       "twenty to forty seconds", "at most three plain sentences", "under 300",
                       "no file paths, function names or code", "not a system instruction"):
            self.assertIn(phrase, EXPLAIN_PREFIX)
        self.assertNotIn("WRITEUP", EXPLAIN_PREFIX, "the long write-up is gone (owner: no verbose texts)")

    def test_a_follow_up_within_the_window_continues_the_same_session(self):
        first = explain_session_key("workshop", now=1000.0)
        self.assertEqual(explain_session_key("workshop", now=1000.0 + 600), first, "a follow-up")
        self.assertNotEqual(explain_session_key("kitchen", now=1000.0), first, "another room is another session")
        later = explain_session_key("workshop", now=1000.0 + 600 + openclaw_tool.EXPLAIN_FOLLOWUP_S + 1)
        self.assertNotEqual(later, first, "a cold start after the window")
        self.assertLessEqual(len(later), openclaw_tool.SESSION_KEY_MAX_CHARS)

    def test_the_pending_result_names_the_mode_and_offers_the_text(self):
        result = pending_result("rcall_7", eta_s=180, mode=EXPLAIN_MODE)
        self.assertTrue(result["pending"])
        self.assertEqual(result["mode"], "explain")
        self.assertIn("deliver_by_text", result["what_to_do"])
        self.assertIn("text", result["what_to_do"])
        outcome = classify_result(result)
        self.assertTrue(outcome.pending and outcome.explain and not outcome.deferred)
        self.assertFalse(classify_result(pending_result("rcall_8")).explain)

    async def test_the_handler_sends_the_mode_and_treats_the_bridge_reply_as_pending(self):
        llm = FakeLLM()
        FakeClient.reply = {"accepted": True, "task_id": "rcall_7", "eta_s": 180, "mode": "explain"}
        with patch.dict("os.environ", ENV), patch.object(openclaw_tool.httpx, "AsyncClient", FakeClient):
            openclaw_tool.register_openclaw_tool(llm, "workshop-device")
            params = Params({"question": "Explain how remote access works.", "mode": "explain"})
            await llm.handlers["ask_openclaw"](params)
        payload = FakeClient.posted[-1]
        self.assertEqual(payload["mode"], "explain")
        self.assertEqual(payload["task_id"], "rcall_7")
        self.assertEqual(payload["room"], "workshop")
        self.assertIn("SPOKEN:", payload["question"])
        result = params.results[-1]
        self.assertTrue(result["pending"])
        self.assertEqual(result["mode"], "explain")

    async def test_a_second_explanation_while_one_runs_is_refused_as_busy(self):
        llm = FakeLLM()
        FakeClient.reply = {"error": "busy"}
        FakeClient.status = 409
        with patch.dict("os.environ", ENV), patch.object(openclaw_tool.httpx, "AsyncClient", FakeClient):
            openclaw_tool.register_openclaw_tool(llm, "workshop-device")
            params = Params({"question": "And the auth part?", "mode": "explain"})
            await llm.handlers["ask_openclaw"](params)
        result = params.results[-1]
        self.assertTrue(result["busy"])
        self.assertIn("still on the earlier one", result["error"])
        self.assertFalse(classify_result(result).ok)

    async def test_a_legacy_bridge_answering_inline_has_its_summary_spoken(self):
        llm = FakeLLM()
        FakeClient.reply = {"answer": "SPOKEN: It uses a tunnel.\nWRITEUP: Details in bridge.mjs."}
        with patch.dict("os.environ", ENV), patch.object(openclaw_tool.httpx, "AsyncClient", FakeClient):
            openclaw_tool.register_openclaw_tool(llm, "workshop-device")
            params = Params({"question": "Explain remote access.", "mode": "explain"})
            await llm.handlers["ask_openclaw"](params)
        self.assertEqual(params.results[-1], {"answer": "It uses a tunnel."})

    async def test_the_text_names_the_speaker_unless_an_override_route_is_set(self):
        from app.openclaw_tool import send_task_message
        FakeClient.reply = {"delivered": True}
        with patch.dict("os.environ", ENV), patch.object(openclaw_tool.httpx, "AsyncClient", FakeClient):
            self.assertTrue(await send_task_message("Body.", "", "Sam"))
            self.assertEqual(FakeClient.posted[-1], {"notification_message": "Body.", "notification_recipient": "Sam"})
            self.assertTrue(await send_task_message("Body.", "", ""))
            self.assertEqual(FakeClient.posted[-1], {"notification_message": "Body."}, "the bridge picks its default")
            self.assertTrue(await send_task_message("Body.", "owner", "Sam"))
            self.assertEqual(FakeClient.posted[-1], {"notification_message": "Body.", "notification_destination": "owner"},
                             "the configured override wins")
            self.assertFalse(await send_task_message("", "", "Sam"), "no message, no text")

    def test_split_explain_reply(self):
        self.assertEqual(split_explain_reply("SPOKEN: A.\nTEXT: B."), ("A.", "B."))
        self.assertEqual(split_explain_reply("SPOKEN: A.\nWRITEUP: B."), ("A.", "B."), "the older marker still splits")
        self.assertEqual(split_explain_reply("Just text."), ("Just text.", ""))
        self.assertEqual(split_explain_reply("SPOKEN: only spoken"), ("only spoken", ""))
        self.assertEqual(split_explain_reply("TEXT: only text"), ("", "only text"))
        self.assertEqual(split_explain_reply("SPOKEN: say it by text: often.\nTEXT: Short."),
                         ("say it by text: often.", "Short."), "a marker counts only at the start of a line")

    async def test_deliver_by_text_needs_a_pending_explanation(self):
        llm = FakeLLM()
        with patch.dict("os.environ", ENV):
            openclaw_tool.register_openclaw_tool(llm, "workshop-device")
        params = Params({})
        await llm.handlers["deliver_by_text"](params)
        self.assertIn("error", params.results[-1], "no service, nothing pending")
        llm.request_text_delivery = lambda: "explain remote access"
        params = Params({})
        await llm.handlers["deliver_by_text"](params)
        self.assertTrue(params.results[-1]["success"])
        self.assertTrue(classify_result(params.results[-1]).ok)


# ---------------------------------------------------------------------------
# what the voice is told, and the text body
# ---------------------------------------------------------------------------

class ExplainWording(unittest.TestCase):
    def test_the_acknowledgement_rotates_natural_phrasings_with_the_name_when_known(self):
        from app.live_tasks import EXPLAIN_ACK_PHRASES
        self.assertGreaterEqual(len(EXPLAIN_ACK_PHRASES), 3)
        seen = set()
        for i in range(len(EXPLAIN_ACK_PHRASES)):
            named = explain_ack_commentary("Sam", pick=i)
            line = named.split('"')[1]
            seen.add(line)
            self.assertIn(", Sam", line, "the identified first name is used")
            self.assertNotIn("moment", line.lower(), "the clip already said 'one moment'")
            self.assertRegex(line.lower(), r"minute|while|shortly", "it takes a while")
            self.assertRegex(line.lower(), r"back to you|report back", "I'll get back to you")
            self.assertIn("text", line.lower(), "a text summary is on offer")
            self.assertIn("Do not say \"one moment\"", named)
            self.assertIn("do not guess the answer", named)
        self.assertEqual(len(seen), len(EXPLAIN_ACK_PHRASES), "distinct phrasings")
        plain = explain_ack_commentary("", pick=0)
        self.assertNotIn(", Sam", plain)
        self.assertIn("Hold on. ", plain, "no name: no dangling comma")
        a, b = explain_ack_commentary("Sam"), explain_ack_commentary("Sam")
        self.assertNotEqual(a, b, "consecutive acknowledgements rotate")
        run = ToolRun(name="ask_openclaw", ok=True, kind="pending", pending=True, task_id="t", explain=True)
        self.assertIn("few minutes of research", RouteResult(PENDING, "", "backend", [run]).commentary())
        watch = ToolRun(name="ask_openclaw", ok=True, kind="pending", pending=True, task_id="t", deferred=True)
        self.assertNotIn("research", RouteResult(PENDING, "", "backend", [watch]).commentary())

    def test_the_result_is_read_in_full_not_briefly(self):
        text = result_commentary(LOOKUP, "It tunnels through a relay.", "explain remote access", "openclaw",
                                 explain=True)
        self.assertIn("in full", text)
        self.assertIn("It tunnels through a relay.", text)
        self.assertIn("explain remote access", text)
        self.assertNotIn("briefly", text)
        plain = result_commentary(LOOKUP, "Flights from 300.", "flights", "openclaw")
        self.assertIn("briefly", plain)
        spoken = spoken_result(LOOKUP, "It tunnels through a relay.", "explain remote access", explain=True)
        self.assertEqual(spoken, "Here is what I found out about explain remote access: It tunnels through a relay.")

    def test_the_confirmation_is_conversational_not_a_delivery_report(self):
        from app.live_tasks import text_delivered_commentary
        text = text_delivered_commentary("explain remote access")
        self.assertIn("in your messages", text)
        self.assertIn("Do not say \"sent\"", text)

    def test_the_text_is_short_plain_and_without_a_header(self):
        from app.live_tasks import trim_text_message
        body = text_message_body("explain remote access", "Spoken summary.", "First point. Second point.")
        self.assertEqual(body, "First point. Second point.", "the agent's TEXT part, no 'You asked' header")
        self.assertNotIn("You asked", body)
        self.assertEqual(text_message_body("q", "Spoken summary.", ""), "Spoken summary.", "falls back to the summary")
        verbose = " ".join(f"Sentence number {i} explains a detail of the bridge." for i in range(1, 12))
        trimmed = text_message_body("q", "s", verbose)
        self.assertLessEqual(trimmed.count("."), 3, "at most three sentences")
        self.assertLessEqual(len(trimmed), 330)
        self.assertTrue(trimmed.endswith("."), "cut at a sentence end")
        long_one = "word " * 120
        self.assertLessEqual(len(trim_text_message(long_one)), 325)
        self.assertTrue(trim_text_message(long_one).endswith("…"), "a single oversized sentence is cut at a word")
        self.assertEqual(trim_text_message(""), "")


class DetailReachesTheSink(unittest.IsolatedAsyncioTestCase):
    async def test_a_three_argument_sink_still_works_and_a_four_argument_one_gets_the_detail(self):
        seen = []

        async def old(task_id, text, ok):
            seen.append(("old", task_id, text, ok))
            return task_id == "old"

        async def new(task_id, text, ok, detail=""):
            seen.append(("new", task_id, text, ok, detail))
            return task_id == "new"
        live_tasks.register_sink(old)
        live_tasks.register_sink(new)
        try:
            self.assertTrue(await live_tasks.deliver_external_result("old", "x", True))
            self.assertTrue(await live_tasks.deliver_external_result("new", "spoken", True, "write-up"))
        finally:
            live_tasks.unregister_sink(old)
            live_tasks.unregister_sink(new)
        self.assertIn(("new", "new", "spoken", True, "write-up"), seen)
        self.assertIn(("old", "old", "x", True), seen)

    async def test_announce_and_task_result_pass_the_detail_and_a_longer_text(self):
        announce_http._recent.clear()
        announce_http._pending.clear()
        claimed = []

        async def sink(task_id, text, ok, detail=""):
            claimed.append((task_id, text, ok, detail))
            return task_id.startswith("known")
        live_tasks.register_sink(sink)
        runner = await announce_http.start_announce_server(
            0, "tok", lambda m, d: True, lambda d: True, task_sink=live_tasks.deliver_external_result)
        port = next(iter(runner.sites))._server.sockets[0].getsockname()[1]
        long_spoken = "word " * 180  # 900 chars: past the bare lane's 600
        try:
            async with ClientSession() as session:
                async with session.post(f"http://127.0.0.1:{port}/announce",
                                        json={"message": long_spoken, "announcement_id": "known-1",
                                              "ok": True, "detail": "The write-up."},
                                        headers={"Authorization": "Bearer tok"}) as r:
                    self.assertEqual((await r.json())["status"], "accepted")
                async with session.post(f"http://127.0.0.1:{port}/task-result",
                                        json={"task_id": "known-2", "answer": "Spoken.", "ok": True,
                                              "detail": "Write-up two."},
                                        headers={"Authorization": "Bearer tok"}) as r:
                    self.assertEqual((await r.json())["status"], "accepted")
                async with session.post(f"http://127.0.0.1:{port}/announce",
                                        json={"message": "Failed.", "announcement_id": "known-3",
                                              "ok": False, "detail": "ignored"},
                                        headers={"Authorization": "Bearer tok"}) as r:
                    self.assertEqual((await r.json())["status"], "accepted")
        finally:
            live_tasks.unregister_sink(sink)
            await runner.cleanup()
        self.assertEqual(claimed[0][0], "known-1")
        self.assertEqual(claimed[0][1], long_spoken.strip(), "the task text is not cut at the lane's limit")
        self.assertEqual(claimed[0][2:], (True, "The write-up."))
        self.assertEqual(claimed[1], ("known-2", "Spoken.", True, "Write-up two."))
        self.assertEqual(claimed[2], ("known-3", "Failed.", False, ""), "a failure carries no write-up")


# ---------------------------------------------------------------------------
# the service: acknowledgement, no hold, delivery by voice or text
# ---------------------------------------------------------------------------

EXPLAIN_REPLY = {"accepted": True, "task_id": "task-x", "eta_s": 180, "mode": "explain"}
SPOKEN = "OpenClaw's remote access goes through an authenticated tunnel to the gateway."


class ExplainHarness(AskHarness):
    """``ask_openclaw`` answers with the bridge's explain acceptance;
    ``deliver_by_text`` is the real handler bound to this service."""

    ask_delay = 0.0

    async def __aenter__(self):
        await super().__aenter__()

        async def slow_ask(params):
            self.executed.append(("ask_openclaw", dict(params.arguments or {})))
            if self.ask_delay:
                await asyncio.sleep(self.ask_delay)
            await params.result_callback(self.reply)
        self.service.register_function("ask_openclaw", slow_ask)
        shim = FakeLLM()
        shim.request_text_delivery = self.service.request_text_delivery
        with patch.dict("os.environ", ENV):
            openclaw_tool.register_openclaw_tool(shim, "workshop-device")
        self.service.register_function("deliver_by_text", shim.handlers["deliver_by_text"])
        self.texted = []
        self.text_recipients = []

        async def text(body, recipient=""):
            self.texted.append(body)
            self.text_recipients.append(recipient)
            return self.text_ok
        self.text_ok = True
        self.service.on_task_message = text
        return self


class ExplainFlow(Patched):
    def setUp(self):
        super().setUp()
        self._tools = patch("tests.test_live_router.TOOLS", TOOLS + [ASK, TEXT_TOOL])
        self._tools.start()

    def tearDown(self):
        self._tools.stop()
        super().tearDown()

    async def test_the_voice_acknowledges_at_once_and_the_answer_is_read_in_full_later(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain how OpenClaw supports remote access",
                                           "mode": "explain"}, "c1")),
            response(message("Hold on. It's going to take me a moment.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            h.service.speaker_probe = SimpleNamespace(gate_speaker=lambda: "male", name_for=lambda label: "Sam")
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            ack = commentaries(h)[0]
            self.assertIn(", Sam", ack, "the identified speaker's first name")
            self.assertIn("text", ack.split('"')[1].lower(), "the text offer")
            self.assertNotIn("moment", ack.split('"')[1].lower())
            self.assertEqual(h.executed[0][1]["mode"], "explain")
            task = next(t for t in h.service._tasks.values() if t.external_id == "task-x")
            self.assertTrue(task.explain)
            await acked(h)
            self.assertFalse(h.service.router_pending, "an explanation holds no session")
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "The write-up."))
            await until(lambda: any(SPOKEN in c for c in commentaries(h)))
            landed = next(c for c in commentaries(h) if SPOKEN in c)
            self.assertIn("in full", landed)
            self.assertIn("explain how OpenClaw supports remote access", landed)
            self.assertEqual(h.texted, [], "voice is the default; no text unless asked")
            self.assertEqual(h.service.router_stats["delivered"], {}, "not finished until it was heard")
            await acked(h)
            self.assertTrue(h.service.router_pending, "a result being spoken holds the session")
            with patch.object(live_service_module, "TASK_HEARD_MIN_S", 0.3):
                h.voice_says(SPOKEN, ms=400)
                await asyncio.sleep(0.35)
                h.voice_says(" More.", ms=400)
                await asyncio.sleep(0.5)
                self.assertEqual(h.service.router_stats["delivered"], {}, "still speaking: not finished yet")
                self.assertTrue(h.service.router_pending)
                # Quiet past the speech hangover: spoken to the end.
                await until(lambda: h.service.router_stats["delivered"] == {SESSION: 1}, timeout=5.0)
            self.assertEqual(h.service.tasks_pending, 0)
            self.assertFalse(h.service.router_pending)

    async def test_a_session_closing_under_the_explanation_hands_it_to_the_next_route(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            announced = []

            async def announce(text, device_id):
                announced.append(text)
                return True
            h.service.on_task_announce = announce
            h.service.on_task_reopen = None
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            await acked(h)
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "Write-up."))
            await until(lambda: any(SPOKEN in c for c in commentaries(h)))
            # The device's follow-up window closes the session before the
            # voice says a word of it (rig 2026-10-14).
            await asyncio.wait_for(h.service.close_session("follow-up cut-off"), 10)
            await until(lambda: announced, timeout=5.0)
            self.assertIn(SPOKEN, announced[0])
            self.assertTrue(announced[0].startswith("Here is what I found out about"))
            self.assertEqual(h.service.router_stats["delivered"], {"announce": 1})
            self.assertEqual(h.service.tasks_pending, 0)

    async def test_an_acceptance_after_the_soft_budget_still_makes_the_text_offer(self):
        # Rig 2026-10-14: the route took 4.4 s, the 3.5 s promotion line played
        # ("taking me a little longer") and the acknowledgement with the text
        # offer never came. The offer is owed whenever the acceptance lands.
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            h.service.task_policy = TaskPolicy(promote_s=1.0)
            h.ask_delay = 1.6
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=6.0)
            first = commentaries(h)[0]
            self.assertNotIn("still working on it", first, "the generic promotion line is replaced")
            self.assertIn("few minutes of research", first, "the explain acknowledgement, at promotion")
            self.assertIn("text", first.split('"')[1].lower())
            await acked(h)
            await until(lambda: h.service.tasks_pending == 1, timeout=3.0)
            await asyncio.sleep(0.6)
            self.assertEqual(len(commentaries(h)), 1, "the acceptance adds no second acknowledgement")

    async def test_no_name_when_the_speaker_is_unknown(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain the bridge", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            await h.user_says("Can you explain how the bridge works?")
            await h.wait_commentary(count=1, timeout=3.0)
            line = commentaries(h)[0].split('"')[1]
            self.assertNotIn(",", line.split(".")[0], "no name when the speaker is unknown")

    async def test_text_would_be_great_switches_that_task_to_a_text(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")),
            response(call("deliver_by_text", {}, "c2")),
            response(message("Okay, I'll text it to you.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            h.service.speaker_probe = SimpleNamespace(gate_speaker=lambda: "male", name_for=lambda label: "Sam")
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            await acked(h)
            await asyncio.sleep(live_service_module.USER_FRAGMENT_MERGE_S + 0.2)
            await h.user_says("Text would be great.", start_ms=9000)
            await h.wait_commentary(count=2, timeout=3.0)
            self.assertIn("Okay, I'll text it to you.", commentaries(h)[1])
            self.assertIn("Verified", commentaries(h)[1])
            self.assertEqual([name for name, _ in h.executed], ["ask_openclaw"], "nothing re-asked")
            task = next(t for t in h.service._tasks.values() if t.external_id == "task-x")
            self.assertTrue(task.text_requested, "deliver_by_text switched the pending task to a text")
            await acked(h)
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "Two short sentences. That is all."))
            await until(lambda: h.texted, timeout=3.0)
            self.assertEqual(h.texted[0], "Two short sentences. That is all.", "the short text part, no header")
            self.assertEqual(h.text_recipients, ["Sam"], "the identified speaker is the recipient")
            await until(lambda: any("in your messages" in c for c in commentaries(h)))
            self.assertFalse(any(SPOKEN in c for c in commentaries(h)), "texted, not read aloud")
            self.assertEqual(h.service.router_stats["delivered"], {IMESSAGE: 1})

    async def test_a_failed_text_falls_back_to_the_voice(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")),
            response(call("deliver_by_text", {}, "c2")),
            response(message("Okay, I'll text it.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            h.text_ok = False
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            await acked(h)
            await asyncio.sleep(live_service_module.USER_FRAGMENT_MERGE_S + 0.2)
            await h.user_says("Text would be great.", start_ms=9000)
            await h.wait_commentary(count=2, timeout=3.0)
            await acked(h)
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "Write-up."))
            await until(lambda: any(SPOKEN in c for c in commentaries(h)))
            self.assertEqual(len(h.texted), 1, "the text was tried once")
            task = next(t for t in h.service._tasks.values() if t.external_id == "task-x")
            self.assertIsNotNone(task.session_sent_mono, "spoken into the session (finished once heard)")
            self.assertFalse(task.text_requested)

    async def test_deliver_by_text_with_nothing_pending_changes_nothing(self):
        backend = FakeBackend(response(call("deliver_by_text", {}, "c1")),
                              response(message("There is nothing to text.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            await h.user_says("Text would be great.")
            await h.wait_commentary(count=1, timeout=3.0)
            self.assertIn("not completed", commentaries(h)[0])
            self.assertEqual(h.texted, [])

    async def test_an_explanation_spoken_in_the_conversation_is_never_also_texted(self):
        # Owner 2026-10-14: no verbose text after the conversation already had
        # the answer. Even with a text destination configured and a text FIRST
        # in the fallback order, a spoken explanation stays spoken-only.
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            h.service.task_policy = TaskPolicy(order=(IMESSAGE, SESSION, "reopen", "announce"), imessage_to="owner")
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            await acked(h)
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "Short text."))
            await until(lambda: any(SPOKEN in c for c in commentaries(h)))
            await acked(h)
            with patch.object(live_service_module, "TASK_HEARD_MIN_S", 0.3):
                h.voice_says(SPOKEN, ms=400)
                await asyncio.sleep(0.35)
                h.voice_says(" More.", ms=400)
                await until(lambda: h.service.router_stats["delivered"] == {SESSION: 1}, timeout=5.0)
            await asyncio.sleep(0.5)
            self.assertEqual(h.texted, [], "spoken in the conversation: never texted as well")
            self.assertEqual(h.service.tasks_pending, 0)

    async def test_a_text_is_the_last_route_for_an_explanation_nobody_asked_to_have_texted(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            announced = []

            async def announce(text, device_id):
                announced.append(text)
                return True
            h.service.on_task_announce = announce
            h.service.on_task_reopen = None
            h.service.task_policy = TaskPolicy(order=(IMESSAGE, "announce"), imessage_to="owner")
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            await acked(h)
            await asyncio.wait_for(h.service.close_session("idle"), 10)
            self.assertTrue(await live_tasks.deliver_external_result("task-x", SPOKEN, True, "Short text."))
            await until(lambda: announced, timeout=5.0)
            self.assertIn(SPOKEN, announced[0], "spoken by the announcement lane, although a text came first in the order")
            self.assertEqual(h.texted, [])

    async def test_the_chat_fast_path_is_vetoed_while_an_explanation_is_pending(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
            await h.user_says("Can you explain how OpenClaw supports remote access?")
            await h.wait_commentary(count=1, timeout=3.0)
            op = live_service_module.RouterOp(op_id="probe", seq=99, text="Text would be great.",
                                              started_mono=0.0)
            h.service._op = op
            self.assertEqual(h.service._chat_fast_path_veto(op), "an explanation is pending")

    async def test_an_explanation_that_never_reports_back_is_closed_within_its_own_bound(self):
        backend = FakeBackend(
            response(call("ask_openclaw", {"question": "Explain remote access", "mode": "explain"}, "c1")),
            response(message("Hold on.")))
        with patch.object(live_service_module, "EXPLAIN_TASK_MAX_S", 0.5), \
                patch.object(live_service_module, "EXTERNAL_TASK_MAX_S", 600.0):
            async with ExplainHarness(self, backend, EXPLAIN_REPLY) as h:
                await h.user_says("Can you explain how OpenClaw supports remote access?")
                await h.wait_commentary(count=1, timeout=3.0)
                await acked(h)
                await until(lambda: any("not completed" in c for c in commentaries(h)), timeout=4.0)
                self.assertIn("did not report back", commentaries(h)[-1])
