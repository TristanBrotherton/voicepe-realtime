"""Protocol-faithful pre-delegation speech regression; synthetic PCM only."""
import asyncio
import unittest
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from app.live_reply import LiveReplyAdmission
from app.live_audio import LiveOutputAudio
from tests import live_fixtures as fx
from tests.live_harness import LiveHarness

def continuation_complete():
    import copy
    events = [copy.deepcopy(fx.response_created()), copy.deepcopy(fx.response_completed())]
    for e in events:
        e['event']['response']['id'] = 'resp_final_continuation'
    return events

class TestAdmission(unittest.TestCase):
    def feed(self, admission, assembler, pcm, busy=False):
        replies=[]
        for event in fx.output_audio_deltas(pcm):
            reply=admission.feed(assembler.feed(event['delta']), busy)
            if reply: replies.append(reply)
        return replies

    def test_ack_before_delegation_is_discarded_whole_not_spliced(self):
        gate, audio=LiveReplyAdmission(), LiveOutputAudio()
        self.assertEqual(self.feed(gate,audio,fx.tone(200)),[])
        gate.invalidate()
        self.assertEqual(self.feed(gate,audio,fx.tone(500),True),[])
        self.assertEqual(self.feed(gate,audio,fx.silence(1000)),[])
        result=fx.tone(700,amplitude=900)
        gate.transcript('Result.')
        replies=self.feed(gate,audio,result+fx.silence(1000))
        self.assertEqual(len(replies),1)
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),result+fx.silence(800))
        # A subsequent acoustic segment is allowed; a silence run is not
        # semantic completion of the response.
        self.assertEqual(len(self.feed(gate,audio,fx.tone(700)+fx.silence(1000))),1)

    def test_rejected_ack_noise_floor_does_not_swallow_final_answer(self):
        # Captured Vesper order: acknowledgement, backend settles, 1.6s of
        # peak~40–90 background noise, final transcript/audio, digital idle.
        gate,audio=LiveReplyAdmission(),LiveOutputAudio()
        gate.transcript('One moment.')
        self.feed(gate,audio,fx.tone(500),True)
        quiet=fx.tone(1600,amplitude=80)
        self.assertEqual(self.feed(gate,audio,quiet),[])
        gate.transcript('Twelve thirty-four.')
        result=fx.tone(700,amplitude=900)
        replies=self.feed(gate,audio,result+fx.silence(1000))
        self.assertEqual(len(replies),1)
        self.assertEqual(replies[0].text,'Twelve thirty-four.')
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),quiet+result+fx.silence(800))

    def test_final_transcript_leading_recovery_boundary_is_preserved(self):
        gate,audio=LiveReplyAdmission(),LiveOutputAudio()
        self.feed(gate,audio,fx.tone(500),True)
        self.feed(gate,audio,fx.tone(600,amplitude=80))
        gate.transcript('Twelve',busy=False)
        self.feed(gate,audio,fx.tone(200,amplitude=80))
        gate.transcript(' thirty-four.')
        replies=self.feed(gate,audio,fx.tone(500)+fx.silence(1000))
        self.assertEqual([r.text for r in replies],['Twelve thirty-four.'])

    def test_final_transcript_survives_original_idle_boundary_after_rejection(self):
        gate,audio=LiveReplyAdmission(),LiveOutputAudio()
        self.feed(gate,audio,fx.tone(500),True)
        self.feed(gate,audio,fx.silence(700))
        gate.transcript('Twelve',busy=False)
        self.feed(gate,audio,fx.silence(200))
        gate.transcript(' thirty-four.')
        replies=self.feed(gate,audio,fx.tone(500)+fx.silence(1000))
        self.assertEqual([r.text for r in replies],['Twelve thirty-four.'])

    def test_captured_short_quiet_gap_preserves_entire_final_answer(self):
        gate,audio=LiveReplyAdmission(),LiveOutputAudio()
        self.feed(gate,audio,fx.tone(500),True)
        quiet=fx.tone(400,amplitude=80)
        self.feed(gate,audio,quiet[:len(quiet)//2])
        gate.transcript('Twelve',busy=False)
        self.feed(gate,audio,quiet[len(quiet)//2:])
        gate.transcript(' thirty-four.')
        result=fx.tone(700,amplitude=900)
        replies=self.feed(gate,audio,result+fx.silence(1000))
        self.assertEqual([r.text for r in replies],['Twelve thirty-four.'])
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),quiet+result+fx.silence(800))

    def test_late_transcript_recovers_held_audio_without_stale_pairing(self):
        gate, audio = LiveReplyAdmission(), LiveOutputAudio()
        self.assertEqual(self.feed(gate, audio, fx.tone(100)+fx.silence(1000)), [])
        gate.transcript('Result.')
        replies = self.feed(gate, audio, fx.silence(300))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].text, 'Result.')
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),fx.tone(100)+fx.silence(800))

    def test_capacity_fails_closed_and_new_user_recovers(self):
        gate,audio=LiveReplyAdmission(max_seconds=.3),LiveOutputAudio()
        self.assertEqual(self.feed(gate,audio,fx.tone(500)+fx.silence(1000)),[])
        gate.reset(); audio.reset(); gate.limit=100000
        gate.transcript('Result.')
        self.assertEqual(len(self.feed(gate,audio,fx.tone(100)+fx.silence(1000))),1)

    def test_a_segment_transcribed_only_as_a_non_speech_mark_is_not_admitted(self):
        # Canary 2026-10-11: the light-on confirmation was a hum whose
        # transcript was "[hum]", admitted and played as the reply.
        for mark in ('[hum]', ' [laughs] ', '*hums*', '[hum] [breath]'):
            with self.subTest(mark=mark):
                gate, audio = LiveReplyAdmission(), LiveOutputAudio()
                gate.transcript(mark)
                self.assertEqual(self.feed(gate, audio, fx.tone(700) + fx.silence(1000)), [])
                gate.transcript('Done.')
                replies = self.feed(gate, audio, fx.tone(400) + fx.silence(1000))
                self.assertEqual([r.text for r in replies], ['Done.'])

    def test_words_with_a_non_speech_mark_are_still_a_reply(self):
        gate, audio = LiveReplyAdmission(), LiveOutputAudio()
        gate.transcript('[hum] The light is on.')
        replies = self.feed(gate, audio, fx.tone(700) + fx.silence(1000))
        self.assertEqual([r.text for r in replies], ['[hum] The light is on.'])

    def test_natural_pause_kept_and_no_partial_audio_escapes(self):
        gate,audio=LiveReplyAdmission(),LiveOutputAudio()
        gate.transcript('Result.')
        pcm=fx.tone(400)+fx.silence(600)+fx.tone(400)
        self.assertEqual(self.feed(gate,audio,pcm),[])
        replies=self.feed(gate,audio,fx.silence(1000))
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),pcm+fx.silence(800))

class TestReplyPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_post_backend_reply_streams_before_quiet_boundary(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service._requires_backend_for_reply = True
            h.service._backend_answer_ready = True
            h.socket.push({
                'type': 'session.output_transcript.delta',
                'delta': 'Twelve thirty-four.',
            })
            spoken = fx.tone(700, amplitude=900)
            h.socket.push_all(fx.output_audio_deltas(spoken))
            await h.sink.wait_for(TTSAudioRawFrame)
            self.assertEqual(
                b''.join(f.audio for f in h.sink.of(TTSAudioRawFrame)), spoken
            )
            self.assertEqual(h.sink.of(LLMFullResponseEndFrame), [])
            h.socket.push_all(fx.output_audio_deltas(fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertEqual(
                [f.text for f in h.sink.of(TTSTextFrame)],
                ['Twelve thirty-four.'],
            )

    async def test_a_post_backend_non_speech_mark_never_reaches_the_device(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service._requires_backend_for_reply = True
            h.service._backend_answer_ready = True
            h.socket.push({'type': 'session.output_transcript.delta', 'delta': '[hum]'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(700, amplitude=900) + fx.silence(1000)))
            await asyncio.sleep(0.3)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            self.assertEqual(h.sink.of(TTSTextFrame), [])

    async def test_late_same_turn_delegation_is_refused_after_stream_starts(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service._requires_backend_for_reply = True
            h.service._backend_answer_ready = True
            h.socket.push({
                'type': 'session.output_transcript.delta',
                'delta': 'All set.',
            })
            h.socket.push_all(fx.output_audio_deltas(fx.tone(700, amplitude=900)))
            await h.sink.wait_for(TTSAudioRawFrame)
            await h.service._on_delegation_created(
                fx.delegation_created('late_second_tool')
            )
            await h.sink.wait_for(StartInterruptionFrame)
            self.assertNotIn('late_second_tool', h.service._evidence.delegations)
            self.assertNotIn('late_second_tool', h.service._delegations)
            self.assertTrue(h.service._suppress_output)

    async def test_new_wake_scope_can_delegate_after_prior_stream(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service._requires_backend_for_reply = True
            h.service._backend_answer_ready = True
            h.socket.push({
                'type': 'session.output_transcript.delta',
                'delta': 'All set.',
            })
            h.socket.push_all(fx.output_audio_deltas(fx.tone(700, amplitude=900)))
            await h.sink.wait_for(TTSAudioRawFrame)
            h.service.turn_timeline.begin(turn_id='fresh-wake', source='wake_word')
            await h.service._on_delegation_created(
                fx.delegation_created('fresh_turn_tool')
            )
            self.assertIn('fresh_turn_tool', h.service._evidence.delegations)
            self.assertIn('fresh_turn_tool', h.service._delegations)

    async def test_tool_free_reply_stays_fully_buffered_in_stream_mode(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.socket.push({
                'type': 'session.output_transcript.delta',
                'delta': 'Hello.',
            })
            spoken = fx.tone(500, amplitude=900)
            h.socket.push_all(fx.output_audio_deltas(spoken))
            await asyncio.sleep(.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            h.socket.push_all(fx.output_audio_deltas(fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)

    async def test_confirmation_prompt_stays_fully_buffered_in_stream_mode(self):
        async with LiveHarness(
            self, admit_complete_replies=True, stream_replies=True
        ) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service._requires_backend_for_reply = True
            h.service._backend_answer_ready = True
            h.service._confirmation_prompts_pending.add('confirm-1')
            h.socket.push({
                'type': 'session.output_transcript.delta',
                'delta': 'Should I unlock it?',
            })
            spoken = fx.tone(500, amplitude=900)
            h.socket.push_all(fx.output_audio_deltas(spoken))
            await asyncio.sleep(.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            h.socket.push_all(fx.output_audio_deltas(fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)

    async def test_real_event_order_one_tool_continuation_and_device_reply(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            executed=[]
            async def handler(params):
                executed.append(1)
                await params.result_callback({'time':'12:34'})
            h.service.register_function('llm__GetDateTime',handler)
            h.socket.push({'type':'session.input_transcript.delta','delta':'What time is it?', 'start_ms':100,'end_ms':500})
            h.socket.push({'type':'session.output_transcript.delta','delta':'Just a second.'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(200)))
            h.socket.push_all(fx.function_call_sequence('call_time','llm__GetDateTime','{}'))
            await h.socket.wait_for_sent('response.create')
            h.socket.push_all(fx.output_audio_deltas(fx.tone(400)+fx.silence(1000)))
            await asyncio.sleep(.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame),[])
            h.socket.push_all(continuation_complete())
            h.socket.push({'type':'session.output_transcript.delta','delta':'Twelve thirty-four.'})
            result=fx.tone(700,amplitude=900)
            h.socket.push_all(fx.output_audio_deltas(result+fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertEqual(executed,[1])
            self.assertEqual(len(h.socket.of('response.create')),1)
            self.assertEqual(len(h.sink.of(LLMFullResponseEndFrame)),1)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)],['Twelve thirty-four.'])
            self.assertEqual(b''.join(f.audio for f in h.sink.of(TTSAudioRawFrame)),result+fx.silence(800))
            h.socket.push_all(fx.function_call_sequence('call_time','llm__GetDateTime','{}'))
            await asyncio.sleep(.05)
            self.assertEqual(executed,[1])
            self.assertEqual(len(h.socket.of('response.create')),1)

    async def test_time_without_lookup_is_suppressed_and_rescued_once(self):
        from unittest.mock import patch
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            await h.service._on_transcript_delta('user', {'delta':'What time is it?', 'start_ms':100,'end_ms':500})
            await h.service._end_turn('user')
            h.service.reply_admission.transcript('A guessed time.')
            for event in fx.output_audio_deltas(fx.tone(500)+fx.silence(1000)):
                h.service._on_audio_delta(event)
            await asyncio.sleep(.02)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            with patch('app.live_service.SILENT_RESPONSE_S', .01):
                await h.service._silent_response_watch()
            self.assertEqual(len(h.socket.of('response.create')), 1)
            async def handler(params):
                await params.result_callback({'time':'12:34'})
            h.service.register_function('llm__GetDateTime', handler)
            h.socket.push_all(fx.function_call_sequence('time_rescue', 'llm__GetDateTime', '{}'))
            await h.socket.wait_for_sent('response.create', count=2)
            self.assertFalse(h.service._backend_answer_ready)
            h.socket.push_all(continuation_complete())
            for _ in range(100):
                if h.service._backend_answer_ready: break
                await asyncio.sleep(.01)
            self.assertTrue(h.service._backend_answer_ready)
            h.socket.push_all(fx.output_audio_deltas(fx.silence(1000)))
            h.socket.push_all(continuation_complete())
            h.socket.push({'type':'session.output_transcript.delta','delta':'Twelve thirty-four.'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(400)+fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)], ['Twelve thirty-four.'])
            self.assertEqual(len(h.sink.of(LLMFullResponseEndFrame)),1)

    async def test_reopened_history_cannot_speak_without_fresh_user_transcript(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            # Teardown must erase admission facts from the previous session.
            await h.service._disconnect()
            self.assertFalse(h.service._user_transcript_seen_for_response)
            await h.open_session()
            await h.start_session('second')
            h.socket.push({'type':'session.output_transcript.delta','delta':'Already done.'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(500)+fx.silence(1000)))
            await asyncio.sleep(.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            h.socket.push({'type':'session.input_transcript.delta','delta':'Status?', 'start_ms':100,'end_ms':300})
            h.socket.push({'type':'session.output_transcript.delta','delta':'Ready.'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(500)+fx.silence(1000)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)], ['Ready.'])

    async def test_completed_backend_work_is_never_rescued_into_a_second_action(self):
        from unittest.mock import patch
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            h.service._last_user_text='Turn on the test light'
            h.service._backend_answer_ready=True
            with patch('app.live_service.SILENT_RESPONSE_S', .01):
                await h.service._silent_response_watch()
            self.assertEqual(h.socket.of('response.create'), [])
            self.assertEqual(h.socket.of('response.item.create'), [])

    async def test_admissible_buffered_speech_is_not_a_silent_turn(self):
        from unittest.mock import patch
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session()
            h.service._user_transcript_seen_for_response = True
            h.service.reply_admission.transcript('Answer.')
            for evt in fx.output_audio_deltas(fx.tone(300)):
                h.service._on_audio_delta(evt)
            silent=[]
            h.service.on_silent_response=lambda: silent.append(True)
            with patch('app.live_service.SILENT_RESPONSE_S', .01):
                await h.service._silent_response_watch()
            self.assertEqual(silent, [])
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])

    async def test_interrupt_discards_held_audio_and_text(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session()
            h.socket.push({'type':'session.output_transcript.delta','delta':'Cancelled.'})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(300)))
            await asyncio.sleep(.05)
            await h.service.cancel_active_response(force=True)
            h.socket.push_all(fx.output_audio_deltas(fx.silence(1000)))
            await asyncio.sleep(.05)
            self.assertEqual(h.sink.of(TTSAudioRawFrame),[])
            self.assertEqual(h.sink.of(TTSTextFrame),[])


class TestJitterAtDeviceBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_final_reaches_device_before_quiet_boundary(self):
        from tests.test_live_device_audio import DevicePath
        async with DevicePath(
            lead_ms=600, admit_complete_replies=True, stream_replies=True
        ) as path:
            path.service._user_transcript_seen_for_response = True
            path.service._requires_backend_for_reply = True
            path.service._backend_answer_ready = True
            path.model.push({
                'type': 'session.output_transcript.delta',
                'delta': 'Ready.',
            })
            spoken = fx.tone(900, amplitude=900)
            for event in fx.output_audio_deltas(spoken):
                path.model.push(event)
            for _ in range(100):
                if path.device.pcm:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(path.device.pcm)
            # The reply boundary has not arrived yet; streaming, rather than
            # whole-reply admission, is what made these bytes audible.
            self.assertTrue(path.service.reply_admission.streaming_segment)

    async def test_provider_stall_does_not_become_device_playout_gap(self):
        import time
        from tests.test_live_device_audio import DevicePath
        async with DevicePath(admit_complete_replies=True) as path:
            writes=[]
            original=path.device.send_bytes
            async def timed(data):
                writes.append(time.monotonic())
                await original(data)
            path.device.send_bytes=timed
            path.model.push({'type':'session.input_transcript.delta','delta':'Status?', 'start_ms':0,'end_ms':200})
            path.model.push({'type':'session.output_transcript.delta','delta':'Ready.'})
            pcm=fx.tone(400)
            for evt in fx.output_audio_deltas(pcm): path.model.push(evt)
            await asyncio.sleep(2.8)
            self.assertEqual(path.device.pcm,b'')
            for evt in fx.output_audio_deltas(pcm+fx.silence(1000)): path.model.push(evt)
            await asyncio.sleep(2.7)
            self.assertEqual(path.device.pcm,pcm+pcm+fx.silence(800))
            self.assertGreater(len(writes),10)
            self.assertLess(max(b-a for a,b in zip(writes,writes[1:])),.45)
