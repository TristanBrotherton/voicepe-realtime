"""Small audit fixes: the router's cancelled tool, the session context cache."""
import asyncio
import time
import unittest

from app.live_router import BackendRouter
from app.session_manager import ContextCacheEntry, SessionManager


class CancelledToolCall(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_tool_is_reported_not_raised(self):
        router = BackendRouter("sk-test", model="m", instructions="", tools=[])

        async def execute(name, arguments):
            if name == "slow":
                raise asyncio.CancelledError
            return {"ok": True}, "ok"
        runs = []
        outputs = await router._execute_step([
            {"type": "function_call", "name": "slow", "call_id": "c1", "arguments": "{}"},
            {"type": "function_call", "name": "fast", "call_id": "c2", "arguments": "{}"},
        ], execute, runs)
        self.assertEqual([o["call_id"] for o in outputs], ["c1", "c2"])
        self.assertIn("could not be run", outputs[0]["output"])
        self.assertNotIn("could not be run", outputs[1]["output"])


class ContextCachePrune(unittest.TestCase):
    def test_expired_contexts_are_dropped_on_insert(self):
        class Context:
            def get_messages(self):
                return [{"role": "user", "content": "x"}]

        class Service:
            _context = Context()
        manager = SessionManager(reuse_timeout=60)
        manager.context_caches["gone"] = ContextCacheEntry(Context(), time.time() - 61)
        manager.context_caches["recent"] = ContextCacheEntry(Context(), time.time() - 5)
        manager.cache_context_from_service("new", Service())
        self.assertEqual(sorted(manager.context_caches), ["new", "recent"])


if __name__ == "__main__":
    unittest.main()
