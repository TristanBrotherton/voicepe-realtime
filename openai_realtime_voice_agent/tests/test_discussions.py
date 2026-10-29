"""Discussions by voice (option ``discussions_enabled``, off by default).

Three generic tools talk to the agent bridge, which owns the items and the
confirmation: list the open items, load one, propose a decision. A decision
from voice is only ever staged; the owner confirms it by text. The tool
definitions carry no item text, project names or paths.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import openclaw_tool


class FakeProbe:
    def __init__(self, label="male", names=("Sam", "Alex")):
        self.label, self.names = label, names

    def gate_speaker(self):
        return self.label

    def name_for(self, label):
        return {"male": self.names[0], "female": self.names[1]}.get(label)


class FakeLLM:
    def __init__(self):
        self.handlers = {}
        self.started = 0
        self.action_gate = SimpleNamespace(enabled=True)
        self.speaker_probe = FakeProbe()

    def register_function(self, name, handler):
        self.handlers[name] = handler

    def on_discussion_started(self):
        self.started += 1


class Bridge:
    """Captures the posts and answers like the bridge."""
    posts = []
    replies = {}
    status = 200

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        Bridge.posts.append(json)
        body = Bridge.replies.get(json["discuss"]["action"], {})
        return SimpleNamespace(status_code=Bridge.status, json=lambda: body,
                               raise_for_status=lambda: None)


class Calls:
    def __init__(self):
        self.results = []

    def params(self, **arguments):
        async def cb(result):
            self.results.append(result)
        return SimpleNamespace(arguments=arguments, result_callback=cb, tool_call_id="t1")


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Bridge.posts, Bridge.status = [], 200
        Bridge.replies = {
            "list": {"count": 1, "items": [{"id": "p-1", "topic": "T", "question": "Q?"}]},
            "brief": {"found": True, "id": "p-1", "topic": "T", "question": "Q?",
                      "details": {"recommendation": "R", "context": "C"}},
            "stage": {"staged": True, "proposal": "voicepe-x"},
            "status": {"found": True, "project": "P", "now": "N", "nothing_to_share": False},
            "message": {"sent": True, "project": "P", "slug": "p"},
        }
        self.llm, self.calls = FakeLLM(), Calls()
        self.env = patch.dict(os.environ, {"OPENCLAW_URL": "http://bridge/secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = patch.object(openclaw_tool.httpx, "AsyncClient", Bridge)
        self.client.start()
        self.addCleanup(self.client.stop)
        openclaw_tool.register_discussion_tools(self.llm)


class DiscussionTools(BridgeCase):
    async def test_list_posts_a_discuss_list_and_marks_the_discussion(self):
        await self.llm.handlers["list_open_discussions"](self.calls.params())
        self.assertEqual(Bridge.posts, [{"discuss": {"action": "list"}}])
        self.assertEqual(self.calls.results[0]["count"], 1)
        self.assertEqual(self.llm.started, 1)

    async def test_open_posts_the_query(self):
        await self.llm.handlers["open_discussion"](self.calls.params(query="project 1"))
        self.assertEqual(Bridge.posts[0]["discuss"], {"action": "brief", "query": "project 1"})
        self.assertEqual(self.llm.started, 1)

    async def test_propose_only_stages(self):
        await self.llm.handlers["propose_decision"](self.calls.params(item_id="p-1", decision="Go with A"))
        self.assertEqual(Bridge.posts[0]["discuss"], {"action": "stage", "item": "p-1", "decision": "Go with A"})
        self.assertTrue(self.calls.results[0]["staged"])
        self.assertEqual(self.llm.started, 0, "staging does not open a discussion")

    async def test_propose_needs_both_fields(self):
        await self.llm.handlers["propose_decision"](self.calls.params(item_id="p-1", decision=" "))
        self.assertEqual(Bridge.posts, [])
        self.assertFalse(self.calls.results[0]["staged"])

    async def test_a_bridge_with_discussions_off_says_so(self):
        Bridge.status = 403
        await self.llm.handlers["list_open_discussions"](self.calls.params())
        self.assertIn("not switched on", self.calls.results[0]["error"])
        self.assertEqual(self.llm.started, 0)


class TheToolsAreGenericAndOffByDefault(unittest.TestCase):
    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCUSSIONS_ENABLED", None)
            self.assertFalse(openclaw_tool.discussions_enabled())
        with patch.dict(os.environ, {"DISCUSSIONS_ENABLED": "true"}):
            self.assertTrue(openclaw_tool.discussions_enabled())

    def test_definitions_carry_no_paths_or_urls(self):
        text = json.dumps(openclaw_tool.get_discussion_tool_definitions())
        self.assertEqual([t["name"] for t in openclaw_tool.get_discussion_tool_definitions()],
                         ["list_open_discussions", "open_discussion", "propose_decision",
                          "project_status", "message_project"])
        for word in ("/", "http", "~"):
            self.assertNotIn(word, text)

    def test_propose_requires_a_read_back_and_a_yes(self):
        propose = openclaw_tool.get_discussion_tool_definitions()[2]["description"]
        self.assertIn("read the decision back", propose)
        self.assertIn("clearly said yes", propose)
        self.assertIn("thumbs-up", propose)


class ProjectStatusAndMessages(BridgeCase):
    """project_status reads one project's status; message_project passes the
    owner's words to a project, only for the owner's voice and only through
    the confirmation gate (the handler runs after the spoken yes)."""

    async def test_status_posts_the_project(self):
        await self.llm.handlers["project_status"](self.calls.params(project="the project"))
        self.assertEqual(Bridge.posts[0]["discuss"], {"action": "status", "project": "the project"})
        self.assertEqual(self.calls.results[0]["now"], "N")
        self.assertEqual(self.llm.started, 0, "a status does not open a discussion")

    async def test_status_needs_a_project(self):
        await self.llm.handlers["project_status"](self.calls.params(project=" "))
        self.assertEqual(Bridge.posts, [])
        self.assertFalse(self.calls.results[0]["found"])

    async def test_message_carries_the_speaker_and_the_confirmation(self):
        await self.llm.handlers["message_project"](self.calls.params(project="P", message="  look at  it "))
        self.assertEqual(Bridge.posts[0]["discuss"], {"action": "message", "project": "P", "text": "look at it",
                                                      "speaker": "Sam", "confirmed": True})
        self.assertTrue(self.calls.results[0]["sent"])

    async def test_message_without_a_speaker_verdict_is_not_sent(self):
        for probe in (None, FakeProbe(label="uncertain"), FakeProbe(label="unknown")):
            self.llm.speaker_probe = probe
            await self.llm.handlers["message_project"](self.calls.params(project="P", message="hi"))
        self.assertEqual(Bridge.posts, [])
        for result in self.calls.results:
            self.assertFalse(result["sent"])
            self.assertIn("offer to send the owner a text", result["error"])

    async def test_message_without_the_confirmation_gate_is_not_sent(self):
        for gate in (None, SimpleNamespace(enabled=False)):
            self.llm.action_gate = gate
            await self.llm.handlers["message_project"](self.calls.params(project="P", message="hi"))
        self.assertEqual(Bridge.posts, [])
        self.assertFalse(any(r["sent"] for r in self.calls.results))

    async def test_a_bridge_refusal_for_another_voice_is_said_politely(self):
        Bridge.replies["message"] = {"sent": False, "not_owner": True, "error": "only the owner"}
        await self.llm.handlers["message_project"](self.calls.params(project="P", message="hi"))
        self.assertIn("only the owner can", self.calls.results[0]["error"])
        Bridge.replies["message"] = {"sent": False, "error": "busy"}
        await self.llm.handlers["message_project"](self.calls.params(project="P", message="hi"))
        self.assertIn("text", self.calls.results[1]["offer"])

    async def test_message_needs_both_fields(self):
        await self.llm.handlers["message_project"](self.calls.params(project="P", message=" "))
        self.assertEqual(Bridge.posts, [])
        self.assertFalse(self.calls.results[0]["sent"])


class TheGateHoldsEveryProjectMessage(unittest.IsolatedAsyncioTestCase):
    async def test_message_project_always_needs_a_spoken_yes(self):
        from app.action_gate import ActionGate
        gate = ActionGate(rules=("lock",))
        decision = await gate.check("message_project", {"project": "P", "message": "please look at the bug"})
        self.assertTrue(decision.requires_confirmation)
        self.assertEqual(decision.summary, 'pass this message to P: "please look at the bug"')
        long = await gate.check("message_project", {"project": "P", "message": "word " * 200})
        self.assertLess(len(long.summary), 300)

    def test_descriptions_route_status_and_messages(self):
        defs = {t["name"]: t["description"] for t in openclaw_tool.get_discussion_tool_definitions()}
        self.assertIn("what's the status of X?", defs["project_status"])
        self.assertIn("message_project", defs["project_status"])
        self.assertIn("project_status", defs["list_open_discussions"])
        self.assertIn("never open another project's item", defs["open_discussion"])
        self.assertIn("confirm_action", defs["message_project"])
        self.assertIn("text instead", defs["message_project"])


class TheServiceDuringADiscussion(unittest.TestCase):
    def test_room_talk_does_not_close_a_discussion(self):
        from app.live_service import OpenAILiveLLMService
        svc = OpenAILiveLLMService.__new__(OpenAILiveLLMService)
        svc._discussion_active = False
        svc._background_closing = False
        svc._not_caught = __import__("collections").deque([1.0])
        svc.on_discussion = None
        svc.on_background_conversation = object()
        svc.on_discussion_started()
        self.assertTrue(svc._discussion_active)
        self.assertEqual(len(svc._not_caught), 0)
        self.assertFalse(svc._close_for_background("test"))


if __name__ == "__main__":
    unittest.main()
