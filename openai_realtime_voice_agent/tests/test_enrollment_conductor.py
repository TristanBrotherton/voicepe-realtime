"""The guided voice-training session: pacing never waits on speech synthesis."""
import asyncio
import unittest

from app.enrollment import EnrollmentConductor


class FakeRecorder:
    def __init__(self, events):
        self.events = events
        self.active = False

    def start(self, person, device_id):
        self.events.append("record")
        self.active = True

    def stop(self):
        self.active = False
        return {}


class Conductor(EnrollmentConductor):
    """Real script and run loop; synthesis and timing replaced."""

    def __init__(self, events, fail=()):
        sent = []

        async def send_json(msg, device_id):
            events.append(msg["mode"])

        async def send_bytes(chunk, device_id):
            sent.append(chunk)
            return True

        super().__init__(FakeRecorder(events), send_json, send_bytes, api_key="")
        self.events = events
        self.fail = set(fail)
        self.rendered = []

    async def _tts(self, text):
        self.events.append("render")
        if text in self.fail:
            raise RuntimeError("synthesis unavailable")
        self.rendered.append(text)
        return b"\x00\x00" * 10


class TestEnrollmentConductor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._sleep = asyncio.sleep

        async def no_wait(_s, *a, **k):
            await self._sleep(0)
        asyncio.sleep = no_wait

    async def asyncTearDown(self):
        asyncio.sleep = self._sleep

    def test_script_has_thirty_reps_from_several_spots(self):
        steps = Conductor([]).script()
        self.assertEqual(sum(1 for t, _ in steps if t == "Next, please."), 30)
        self.assertTrue(any("across the room" in t for t, _ in steps))
        self.assertTrue(any("turn away" in t for t, _ in steps))

    async def test_every_line_is_rendered_before_recording_starts(self):
        events = []
        c = Conductor(events)
        c.device_id = "workshop"
        await c._run("sam")
        first_record = events.index("record")
        self.assertNotIn("render", events[first_record:])
        self.assertEqual(len(c.rendered), len({t for t, _ in c.script()}))

    async def test_a_line_that_cannot_be_rendered_means_no_session(self):
        events = []
        c = Conductor(events, fail={"Keep going."})
        c.device_id = "workshop"
        await c._run("sam")
        self.assertNotIn("record", events)
        self.assertNotIn("start", events)

    async def test_prepare_counts_distinct_lines_and_misses(self):
        c = Conductor([], fail={"Keep going."})
        total = len({t for t, _ in c.script()})
        self.assertEqual(await c.prepare(), total - 1)
        c.fail.clear()
        self.assertEqual(await c.prepare(), total)

    async def test_lines_prepared_at_startup_are_not_rendered_again(self):
        events = []
        c = Conductor(events)
        await c.prepare()
        events.clear()
        c.device_id = "workshop"
        await c._run("sam")
        self.assertNotIn("render", events)


if __name__ == "__main__":
    unittest.main()
