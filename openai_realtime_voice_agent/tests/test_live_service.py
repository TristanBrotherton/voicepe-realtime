"""GPT-Live service lifecycle inside a real pipecat pipeline, against a fake socket.

Covers session start, the input audio path, transcript turns -> speaking and
transcription frames, interruption, reconnect with seeded history, server close
and reader death surfacing as ErrorFrames, silent turns, and startup-failure
handling.

Output audio has its own modules: ``test_live_audio.py`` for the assembler and
``test_live_device_audio.py`` for the bytes the device actually receives.
Delegated tools have ``test_live_delegation.py``.
"""
import asyncio
import base64
import unittest
from unittest.mock import patch

from pipecat.frames.frames import (
    ErrorFrame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

import app.live_service as live_service_module
from app.action_gate import ActionGate

from tests import live_fixtures as fx
from tests.live_harness import LiveHarness


def tone(ms, amplitude=3000, rate=24000):
    return fx.tone(ms, freq=220.0, amplitude=amplitude, rate=rate)


def silence(ms, rate=24000):
    return fx.silence(ms, rate=rate)


def b64(pcm):
    return base64.b64encode(pcm).decode("ascii")


class TestSessionStart(unittest.IsolatedAsyncioTestCase):
    async def test_session_start_is_first_and_early_audio_flushes_after_started(self):
        async with LiveHarness(self) as h:
            self.assertEqual(h.socket.types(), ["session.start"])
            start = h.socket.sent[0]["session"]
            self.assertEqual(start["model"], "gpt-live-1")
            self.assertEqual(start["audio"]["output"], {"voice": "marin"})
            self.assertEqual(start["delegation"]["type"], "responses")
            # Mic audio before session.started is held locally because the
            # protocol forbids sending it yet; a short command must not vanish.
            await h.task.queue_frames([InputAudioRawFrame(audio=tone(20), sample_rate=24000, num_channels=1)])
            await asyncio.sleep(0.05)
            self.assertEqual(h.socket.of("session.input_audio.append"), [])
            await h.start_session()
            await h.socket.wait_for_sent("session.input_audio.append")
            self.assertEqual(
                base64.b64decode(h.socket.of("session.input_audio.append")[0]["audio"]),
                tone(20),
            )
            await h.task.queue_frames([InputAudioRawFrame(audio=tone(20), sample_rate=24000, num_channels=1)])
            await h.socket.wait_for_sent("session.input_audio.append")
            appended = h.socket.of("session.input_audio.append")[0]["audio"]
            self.assertEqual(base64.b64decode(appended), tone(20))

    async def test_input_is_resampled_to_24k(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            frames = [InputAudioRawFrame(audio=tone(20, rate=16000), sample_rate=16000, num_channels=1)] * 5
            await h.task.queue_frames(frames)
            await h.socket.wait_for_sent("session.input_audio.append")
            total = sum(len(base64.b64decode(m["audio"])) for m in h.socket.of("session.input_audio.append"))
            self.assertEqual(total % 2, 0)
            self.assertGreater(total, len(tone(20, rate=16000)), "upsampled")

    async def test_context_queued_before_start_is_sent_after_started(self):
        async with LiveHarness(self) as h:
            await h.service.inject_context("[voice check] matches Alex")
            self.assertEqual(h.socket.of("session.thinking.append"), [])
            await h.start_session()
            await h.socket.wait_for_sent("session.thinking.append")
            evt = h.socket.of("session.thinking.append")[0]
            self.assertEqual((evt["delegation_id"], evt["content"]), (None, "[voice check] matches Alex"))


class TestAudioOut(unittest.IsolatedAsyncioTestCase):
    """Only the turn bookkeeping here; the audio itself has its own modules."""

    async def test_the_first_audible_audio_marks_the_turn_and_opens_the_response(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            answered = []
            h.service.on_response_audio = lambda: answered.append(1)
            h.service.turn_timeline.begin("t1")
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(silence(100))})
            await asyncio.sleep(0.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [],
                             "silence before the reply is held, not pushed: it would "
                             "put the device into REPLYING and spend its playout lead")
            self.assertEqual(answered, [], "silence is not a response start")
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
            await h.sink.wait_for(TTSAudioRawFrame, count=1)
            frame = h.sink.of(TTSAudioRawFrame)[0]
            self.assertEqual(frame.sample_rate, 24000)
            self.assertEqual(frame.num_channels, 1)
            self.assertEqual(frame.audio, silence(100) + tone(100),
                             "the held lead-in is released with the first word, "
                             "byte for byte and in order")
            self.assertIn("first_model_audio", h.service.turn_timeline.current.stamps)
            self.assertIn("response_created", h.service.turn_timeline.current.stamps)
            self.assertEqual(answered, [1])
            self.assertTrue(h.service.response_active)

    async def test_a_mismatched_negotiated_audio_format_stops_the_session(self):
        async with LiveHarness(self) as h:
            h.socket.push(fx.session_started(
                "sess_bad", {"format": {"type": "audio/pcmu", "rate": 8000}}
            ))
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            self.assertIn("audio format mismatch", h.source.of(ErrorFrame)[0].error)
            await h.socket.wait_for_sent("session.close")
            self.assertFalse(h.service._session_started)
            self.assertEqual(h.socket.of("session.input_audio.append"), [])
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
            await asyncio.sleep(0.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [],
                             "a refused format must never reach the speaker")

    async def test_response_start_cancellation_drops_the_chunk_already_in_hand(self):
        async with LiveHarness(self) as h:
            await h.start_session()

            async def stop_at_start():
                await h.service.cancel_active_response("racing stop", force=True)

            h.service.on_assistant_response_started(stop_at_start)
            answered = []
            h.service.on_response_audio = lambda: answered.append(1)
            h.service.turn_timeline.begin("t-cancel")
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
            await asyncio.sleep(0.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [],
                             "the first chunk must not leak after its start hook cancels")
            self.assertEqual(answered, [])
            self.assertNotIn("first_model_audio", h.service.turn_timeline.current.stamps)


class TestTranscriptTurns(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_affirmatives_are_explicit_and_multilingual(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            h.service.turn_timeline.user_turn_seq = 1
            h.service._last_user_text_seq = 1
            for text in ("yes", "yes please", "ja", "ja graag", "sí", "oui",
                         "d'accord", "in Ordnung", "certo"):
                h.service._last_user_text = text
                self.assertTrue(h.service.confirmation_reply_is_affirmative(), text)
            for text in ("unlock the door", "yes don't", "ja niet", "no", "maybe"):
                h.service._last_user_text = text
                self.assertFalse(h.service.confirmation_reply_is_affirmative(), text)

    async def test_same_utterance_cannot_confirm_a_held_action(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            h.service.turn_timeline.begin("t1")
            await h.service._open_turn("user")
            seq = h.service.turn_timeline.user_turn_seq

            async def noop(_params):
                return None

            gate = ActionGate()
            pending = gate.request("kitchen", "HassTurnOff", {"name": "Front Door"},
                                   noop, "unlock Front Door", seq,
                                   h.service.turn_timeline.wake_seq)
            gate.fence_after_prompt(pending.confirm_id, seq)
            h.service._user_turn.open = True
            await h.service._end_turn("user")
            self.assertEqual(h.service.turn_timeline.user_turn_seq, seq,
                             "closing the same utterance must not create a confirmation turn")
            held, error = gate.take(pending.confirm_id, "kitchen", seq,
                                    h.service.turn_timeline.wake_seq)
            self.assertIsNone(held)
            self.assertIn("not answered", error)
            await h.service._open_turn("user")
            held, error = gate.take(pending.confirm_id, "kitchen",
                                    h.service.turn_timeline.user_turn_seq,
                                    h.service.turn_timeline.wake_seq)
            self.assertIsNotNone(held)
            self.assertIsNone(error)

    async def test_user_turn_produces_speaking_frames_and_an_upstream_transcription(self):
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.05):
            async with LiveHarness(self) as h:
                await h.start_session()
                h.service.turn_timeline.begin("t1")
                h.socket.push({"type": "session.input_transcript.delta", "delta": "turn the", "start_ms": 0, "end_ms": 200})
                h.socket.push({"type": "session.input_transcript.delta", "delta": " lamp on", "start_ms": 200, "end_ms": 400})
                await h.sink.wait_for(UserStartedSpeakingFrame)
                self.assertEqual(len(h.source.of(UserStartedSpeakingFrame, FrameDirection.UPSTREAM)), 1)
                self.assertEqual(h.service.turn_timeline.user_turn_seq, 1,
                                 "the utterance exists before any delegated tool can run")
                self.assertIn("speech_started", h.service.turn_timeline.current.stamps)
                await h.source.wait_for(TranscriptionFrame, direction=FrameDirection.UPSTREAM)
                self.assertEqual(h.source.of(TranscriptionFrame)[0].text, "turn the lamp on")
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                self.assertEqual(h.service.turn_timeline.user_turn_seq, 1, "confirmation gate sees a new utterance")
                self.assertIn("speech_stopped", h.service.turn_timeline.current.stamps)

    async def test_delayed_contiguous_fragments_are_one_confirmation_sequence(self):
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.02):
            async with LiveHarness(self) as h:
                await h.start_session()
                h.service.turn_timeline.begin("t-split")
                h.socket.push({"type": "session.input_transcript.delta", "delta": "unlock ",
                               "start_ms": 0, "end_ms": 100})
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                self.assertEqual(h.service.turn_timeline.user_turn_seq, 1)
                h.socket.push({"type": "session.input_transcript.delta", "delta": "the door",
                               "start_ms": 100, "end_ms": 200})
                await h.sink.wait_for(UserStartedSpeakingFrame, count=2)
                self.assertEqual(h.service.turn_timeline.user_turn_seq, 1,
                                 "network delay must not invent a confirmation utterance")

    async def test_assistant_transcript_is_bracketed_tts_text(self):
        with patch.object(live_service_module, "ASSISTANT_TURN_GAP_S", 0.05):
            async with LiveHarness(self) as h:
                await h.start_session()
                h.socket.push({"type": "session.output_transcript.delta", "delta": "The lamp", "start_ms": 0, "end_ms": 300})
                h.socket.push({"type": "session.output_transcript.delta", "delta": " is on.", "start_ms": 300, "end_ms": 600})
                await h.sink.wait_for(LLMFullResponseEndFrame)
                texts = [f.text for f in h.sink.of(TTSTextFrame)]
                self.assertEqual("".join(texts), "The lamp is on.")
                self.assertEqual(len(h.sink.of(LLMFullResponseStartFrame)), 1)

    async def test_silent_turn_is_reported(self):
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.02), \
             patch.object(live_service_module, "SILENT_RESPONSE_S", 0.1):
            async with LiveHarness(self) as h:
                await h.start_session()
                silent = []
                h.service.on_silent_response = lambda: silent.append(1)
                h.socket.push({"type": "session.input_transcript.delta", "delta": "uh", "start_ms": 0, "end_ms": 100})
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                await asyncio.sleep(0.2)
                self.assertEqual(silent, [1])

    async def test_a_silent_turn_records_whether_the_model_heard_anything(self):
        """The second canary's unanswered "all of them" could not be diagnosed.

        "GPT-Live never transcribed it" and "GPT-Live heard it and said
        nothing" need different fixes, so the length of what it heard — never
        the words — is recorded when a turn comes back silent.
        """
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.02), \
             patch.object(live_service_module, "SILENT_RESPONSE_S", 0.1):
            async with LiveHarness(self) as h:
                await h.start_session()
                silent = []
                h.service.on_silent_response = lambda: silent.append(1)
                for delta in ("all ", "of ", "them"):
                    h.socket.push({"type": "session.input_transcript.delta",
                                   "delta": delta, "start_ms": 0, "end_ms": 100})
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                with self.assertLogs("app.live_service", level="INFO") as logs:
                    await asyncio.sleep(0.25)
                self.assertEqual(silent, [1])
                line = next(m for m in logs.output if "said nothing" in m)
                self.assertIn("[11 transcript chars heard]", line)
                self.assertNotIn("all of them", line, "never the words")
                self.assertEqual(h.service._last_user_chars, len("all of them"))

    async def test_speech_after_the_user_turn_is_not_silent(self):
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.02), \
             patch.object(live_service_module, "SILENT_RESPONSE_S", 0.1):
            async with LiveHarness(self) as h:
                await h.start_session()
                silent = []
                h.service.on_silent_response = lambda: silent.append(1)
                h.socket.push({"type": "session.input_transcript.delta", "delta": "hello", "start_ms": 0, "end_ms": 100})
                await h.sink.wait_for(UserStoppedSpeakingFrame)
                h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
                await h.sink.wait_for(TTSAudioRawFrame)
                await asyncio.sleep(0.2)
                self.assertEqual(silent, [])


class TestInterruption(unittest.IsolatedAsyncioTestCase):
    async def test_stop_sends_quiet_context_once_and_drops_audio_until_the_user_speaks(self):
        with patch.object(live_service_module, "USER_TURN_GAP_S", 0.05):
            async with LiveHarness(self) as h:
                await h.start_session()
                h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
                await h.sink.wait_for(TTSAudioRawFrame)
                self.assertTrue(await h.service.cancel_active_response("device interrupt"))
                self.assertTrue(await h.service.cancel_active_response("racing", force=True))
                await h.socket.wait_for_sent("session.thinking.append")
                # thinking.append, never instructions.append: session
                # instructions are trusted and cumulative for the life of the
                # session, so one append per interruption would permanently
                # teach the session to stay silent.
                self.assertEqual(h.socket.of("session.instructions.append"), [])
                self.assertEqual(len(h.socket.of("session.thinking.append")), 1, "stop is sent once")
                self.assertIsNone(h.socket.of("session.thinking.append")[0]["delegation_id"])
                h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
                await asyncio.sleep(0.05)
                self.assertEqual(len(h.sink.of(TTSAudioRawFrame)), 1, "post-stop audio is dropped")
                self.assertEqual(h.service._suppressed_deltas, 1)
                self.assertFalse(h.service.audio.speaking,
                                 "suppressed audio cannot reopen the response")
                # Discarding pending input is a documented no-op on Live.
                self.assertFalse(await h.service.discard_pending_input("stop"))
                # The next utterance lifts the suppression.
                h.socket.push({"type": "session.input_transcript.delta", "delta": "and the kitchen", "start_ms": 0, "end_ms": 300})
                await h.sink.wait_for(UserStartedSpeakingFrame)
                h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
                await h.sink.wait_for(TTSAudioRawFrame, count=2)

    async def test_cancel_without_an_active_response_is_a_no_op(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            self.assertFalse(await h.service.cancel_active_response("stop"))
            self.assertEqual(h.socket.of("session.instructions.append"), [])

    async def test_new_speech_segments_notify_the_racing_response_hook(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            fired = []

            async def hook():
                fired.append(1)

            h.service.on_assistant_response_started(hook)
            h.socket.push({"type": "session.output_audio.delta", "delta": b64(tone(100))})
            await h.sink.wait_for(TTSAudioRawFrame)
            self.assertEqual(fired, [1])


class TestReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_reset_conversation_opens_a_new_session_seeded_with_history(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            context = LLMContext(messages=[
                {"role": "user", "content": "turn the lamp on"},
                {"role": "assistant", "content": "The lamp is on."},
                {"role": "tool", "tool_call_id": "c1", "content": "done"},
            ])
            h.service._context = context
            old = h.socket
            await h.service.reset_conversation()
            self.assertTrue(old.closed)
            self.assertEqual(len(h.sockets), 2)
            start = h.socket.sent[0]
            self.assertEqual(start["type"], "session.start")
            self.assertEqual([i["role"] for i in start["session"]["input"]], ["user", "assistant"])
            self.assertFalse(h.service._session_started, "waits for the new session.started")
            await h.start_session("sess_2")
            self.assertEqual(h.service._session_id, "sess_2")

    async def test_tool_dispatch_blocks_audio_replay_across_a_session_reset(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            done = asyncio.Event()

            async def action(params):
                done.set()
                await params.result_callback({"status": "done"})

            h.service.register_function("HassTurnOn", action)
            h.socket.push_all(fx.function_call_sequence(
                "call_action", "HassTurnOn", '{"name":"Atrium Lamp"}'
            ))
            await done.wait()
            self.assertTrue(h.service.unsafe_to_replay_input)
            await h.service.reset_conversation()
            self.assertTrue(h.service.unsafe_to_replay_input,
                            "a reconnect must not make the original command replay-safe")

    async def test_unrequested_server_close_and_reader_death_surface_errors(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            h.socket.push({"type": "session.closed", "reason": "expired", "usage": {"seconds": 42}})
            await h.source.wait_for(ErrorFrame, direction=FrameDirection.UPSTREAM)
            self.assertIn("session_expired", h.source.of(ErrorFrame)[0].error)
            self.assertEqual(h.service.live_seconds, 42.0)
            await h.socket.close()  # the server drops the socket
            await h.source.wait_for(ErrorFrame, count=2, direction=FrameDirection.UPSTREAM)
            self.assertIn("receive loop ended", h.source.of(ErrorFrame)[1].error)

    async def test_startup_failures_block_reconnect_after_three(self):
        async with LiveHarness(self) as h:
            for i in range(3):
                h.socket.push({"type": "error", "error": {"type": "invalid_request_error",
                                                          "code": "model_not_found", "message": "no"}})
                await h.source.wait_for(ErrorFrame, count=i + 1, direction=FrameDirection.UPSTREAM)
            self.assertIn("startup failed", h.source.of(ErrorFrame)[0].error)
            with self.assertRaises(RuntimeError):
                await h.service.reset_conversation()

    async def test_rejected_command_does_not_wake_recovery(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            h.socket.push({"type": "error", "error": {"type": "invalid_request_error", "code": "x",
                                                      "message": "bad append", "client_event_id": "t1"}})
            await asyncio.sleep(0.05)
            self.assertEqual(h.source.of(ErrorFrame), [])

    async def test_graceful_disconnect_sends_session_close(self):
        async with LiveHarness(self) as h:
            await h.start_session()
            socket = h.socket

            async def answer_close():
                await socket.wait_for_sent("session.close")
                socket.push({"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 3}})

            waiter = asyncio.create_task(answer_close())
            await h.service.disconnect()
            await waiter
            self.assertTrue(socket.closed)
            self.assertEqual(h.service.live_seconds, 3.0)
            self.assertEqual(h.source.of(ErrorFrame), [], "a requested close is not an error")


if __name__ == "__main__":
    unittest.main()
