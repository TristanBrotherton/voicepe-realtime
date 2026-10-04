"""PhaseEmitter: turn observers, silent turns, debounce, watchdog interplay."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.phase_emitter import PhaseEmitter


class EmitterTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = patch.object(FrameProcessor, "process_frame", new=AsyncMock())
        self.base.start()
        self.phases = []

        async def send(value, **extra):
            self.phases.append((value, extra))

        self.emitter = PhaseEmitter(send, idle_debounce_s=0.01)
        self.emitter.push_frame = AsyncMock()
        self.emitter.SILENT_GRACE_S = 0.02
        self.outcomes = []
        self.emitter.on_turn_end = self.outcomes.append

    async def asyncTearDown(self):
        await self.emitter.close()
        self.base.stop()

    async def frame(self, frame):
        await self.emitter.process_frame(frame, FrameDirection.DOWNSTREAM)


class TestSilentTurn(EmitterTestCase):
    async def test_silent_response_closes_turn_without_follow_up(self):
        await self.frame(UserStartedSpeakingFrame())
        await self.frame(UserStoppedSpeakingFrame())
        self.emitter.end_silent_turn()
        await asyncio.sleep(0.05)
        self.assertEqual(self.phases[-1], ("idle", {"followup": False}))
        self.assertEqual(self.outcomes, ["silent"])

    async def test_new_speech_within_grace_keeps_the_turn(self):
        await self.frame(UserStartedSpeakingFrame())
        await self.frame(UserStoppedSpeakingFrame())
        self.emitter.end_silent_turn()
        await self.frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        self.assertEqual(self.phases[-1][0], "listening")
        self.assertEqual(self.outcomes, [])

    async def test_running_tool_blocks_silent_close(self):
        await self.frame(UserStartedSpeakingFrame())
        await self.frame(UserStoppedSpeakingFrame())
        self.emitter._liveness.tool_started()
        self.emitter.end_silent_turn()
        await asyncio.sleep(0.05)
        self.assertNotIn(("idle", {"followup": False}), self.phases)


class TestTurnObservers(EmitterTestCase):
    async def test_reply_ends_turn_as_replied_and_bot_callbacks_fire(self):
        started, stopped = [], []
        self.emitter.on_bot_started = lambda: started.append(1)
        self.emitter.on_bot_stopped = lambda: stopped.append(1)
        await self.frame(UserStartedSpeakingFrame())
        await self.frame(UserStoppedSpeakingFrame())
        await self.frame(BotStartedSpeakingFrame())
        await self.frame(BotStoppedSpeakingFrame())
        await asyncio.sleep(0.05)
        self.assertEqual([p[0] for p in self.phases], ["listening", "thinking", "replying", "idle"])
        self.assertEqual(self.outcomes, ["replied"])
        self.assertEqual((len(started), len(stopped)), (1, 1))

    async def test_force_idle_ends_turn_as_error(self):
        await self.frame(UserStartedSpeakingFrame())
        await self.emitter.force_idle("test")
        self.assertEqual(self.outcomes, ["error"])
        self.assertEqual(self.phases[-1], ("idle", {}))

    async def test_listening_is_never_deduplicated(self):
        await self.frame(UserStartedSpeakingFrame())
        await self.frame(UserStartedSpeakingFrame())
        self.assertEqual([p[0] for p in self.phases], ["listening", "listening"])

    async def test_dangling_vad_stop_after_wake_is_suppressed(self):
        armed = []
        self.emitter.set_kill_window_handlers(on_dangling=lambda: armed.append(1))
        self.emitter.note_wake()
        await self.frame(UserStoppedSpeakingFrame())
        self.assertEqual(self.phases, [])
        self.assertEqual(armed, [1])


if __name__ == "__main__":
    unittest.main()
