"""Voice memory: remember, forget (bounded, backed up, read back) and list."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import voice_memory


class FakeLLM:
    def __init__(self):
        self.functions = {}

    def register_function(self, name, handler):
        self.functions[name] = handler


class MemoryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "voice-memory", "memory.md")
        patcher = patch.object(voice_memory, "MEM_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        self.speaker = "Alex"
        self.llm = FakeLLM()
        voice_memory.register_memory_tools(self.llm, lambda: self.speaker)

    async def call(self, name, **arguments):
        results = []

        async def result_callback(result):
            results.append(result)
        await self.llm.functions[name](SimpleNamespace(arguments=arguments, result_callback=result_callback))
        return results[-1]

    async def seed(self, *notes):
        for note in notes:
            self.assertEqual((await self.call("remember", note=note))["status"], "remembered")

    def stored(self):
        return [voice_memory._note_text(l) for l in voice_memory._load()]


class Remember(MemoryCase):
    async def test_remember_is_attributed_and_listed(self):
        await self.seed("The bins go out on Tuesday")
        lines = voice_memory._load()
        self.assertEqual(len(lines), 1)
        self.assertIn("(from Alex, ", lines[0])
        self.assertIn("The bins go out on Tuesday", voice_memory.memory_instructions())
        self.assertEqual((await self.call("list_memories"))["memories"], lines)

    async def test_unknown_voice_cannot_change_memory(self):
        self.speaker = None
        self.assertIn("error", await self.call("remember", note="x y z"))
        self.assertIn("error", await self.call("forget", matching="bins"))

    async def test_note_text_is_not_logged_without_log_transcripts(self):
        with patch.dict(os.environ, {"LOG_TRANSCRIPTS": "false"}), \
                self.assertLogs("app.voice_memory", "INFO") as logs:
            await self.seed("The secret word is pineapple")
            await self.call("forget", matching="pineapple")
        self.assertNotIn("pineapple", "\n".join(logs.output))
        with patch.dict(os.environ, {"LOG_TRANSCRIPTS": "true"}), \
                self.assertLogs("app.voice_memory", "INFO") as logs:
            await self.seed("The secret word is mango")
        self.assertIn("mango", "\n".join(logs.output))


class Forget(MemoryCase):
    async def test_empty_or_short_phrase_removes_nothing(self):
        await self.seed("The bins go out on Tuesday", "Dinner is at six")
        for phrase in ("", " ", "a", "on", "?!"):
            result = await self.call("forget", matching=phrase)
            self.assertIn("error", result, phrase)
        self.assertEqual(len(self.stored()), 2)

    async def test_whole_word_match_and_removed_notes_returned(self):
        await self.seed("The bins go out on Tuesday", "Cabins by the lake are booked", "Dinner is at six")
        result = await self.call("forget", matching="bins")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["removed_notes"], ["The bins go out on Tuesday"])
        self.assertEqual(self.stored(), ["Cabins by the lake are booked", "Dinner is at six"])

    async def test_phrase_match_is_case_and_space_insensitive(self):
        await self.seed("The Bins  go out on Tuesday", "Dinner is at six")
        result = await self.call("forget", matching="bins go")
        self.assertEqual(result["removed"], 1)

    async def test_attribution_is_not_searched(self):
        await self.seed("Dinner is at six", "The bins go out on Tuesday")
        result = await self.call("forget", matching="Alex")
        self.assertEqual(result["removed"], 0)
        self.assertEqual(len(self.stored()), 2)

    async def test_no_match_removes_nothing(self):
        await self.seed("Dinner is at six")
        result = await self.call("forget", matching="bins")
        self.assertEqual(result["removed"], 0)
        self.assertFalse(os.path.exists(self.path + ".bak"))

    async def test_many_or_all_notes_need_a_confirmation(self):
        await self.seed("Tea at four", "Tea is green", "Tea has no sugar", "Dinner is at six")
        result = await self.call("forget", matching="tea")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(len(result["would_remove"]), 3)
        self.assertEqual(len(self.stored()), 4, "nothing removed before the yes")
        result = await self.call("forget", matching="tea", confirm=True)
        self.assertEqual(result["removed"], 3)
        self.assertEqual(self.stored(), ["Dinner is at six"])

    async def test_removing_every_note_needs_a_confirmation(self):
        await self.seed("Tea at four", "Tea is green")
        result = await self.call("forget", matching="tea")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(len(self.stored()), 2)

    async def test_a_single_note_can_be_forgotten(self):
        await self.seed("Tea at four")
        self.assertEqual((await self.call("forget", matching="tea"))["removed"], 1)
        self.assertEqual(self.stored(), [])

    async def test_backup_holds_the_file_before_the_removal(self):
        await self.seed("The bins go out on Tuesday", "Dinner is at six")
        with open(self.path) as f:
            before = f.read()
        await self.call("forget", matching="dinner")
        with open(self.path + ".bak") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
