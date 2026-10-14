"""Offline regressions for independent review R1-R6; no providers/devices."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.live_audio import OutputChunk
from app.live_reply import LiveReplyAdmission
from app.live_cost import LiveSpendLedger
from app.live_session_gate import LiveSessionGate
from app.live_evidence import TurnEvidence
from pipecat.frames.frames import (
    InputAudioRawFrame, LLMFullResponseEndFrame, OutputTransportMessageFrame,
    TTSAudioRawFrame, TTSTextFrame,
)
from tests.live_harness import LiveHarness
from tests.test_live_device_audio import DevicePath
from tests.test_live_session_gate import Clock, FakeLiveService
from tests import live_fixtures as fx
from tests.test_live_gate_wiring import GateWiringCase, FakeLiveService as PipelineFakeLive


def segment(admission, text, end_ms):
    admission.transcript(text, end_ms=end_ms)
    admission.feed(OutputChunk(pcm=fx.tone(100), audible=True), False, end_ms=end_ms)
    return admission.feed(OutputChunk(reason='idle run exceeded max_silence_ms'), False, end_ms=end_ms)


def metrics(batch, seq=0):
    return {'output_batch_id':batch,'seq':seq,'audible_to_drained_ms':100,'outcome':'replied'}


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def _assert_second_session_uses_its_own_audio_clock(self, h, reopen):
        s=h.service
        for e in fx.output_audio_deltas(fx.silence(10000)): s._on_audio_delta(e)
        self.assertEqual(s._session_output_audio_bytes,480000)

        await reopen(h)
        await h.start_session('second')
        self.assertEqual(s._session_output_audio_bytes,0)
        self.assertEqual(s.audio.metrics.bytes_in,480000)

        s._user_transcript_seen_for_response=True
        h.socket.push({'type':'session.output_transcript.delta',
                       'delta':'Safe answer.','start_ms':1100,'end_ms':1400})
        h.socket.push_all(fx.output_audio_deltas(fx.tone(100)+fx.silence(900)))
        await asyncio.sleep(.03)
        self.assertEqual(s.reply_admission.audio_end_ms,1000)
        self.assertEqual(h.sink.of(LLMFullResponseEndFrame),[])
        self.assertIsNotNone(s.reply_admission.alignment_deadline)

        h.socket.push_all(fx.output_audio_deltas(fx.silence(400)))
        await h.sink.wait_for(LLMFullResponseEndFrame)
        self.assertFalse(s.reply_admission.terminal)
        self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)],['Safe answer.'])

    async def test_untimed_audio_clock_resets_after_on_demand_close_and_open(self):
        async with LiveHarness(self,on_demand=True,admit_complete_replies=True) as h:
            await h.open_session(); await h.start_session('first')
            async def reopen(path):
                socket=path.socket
                closing=asyncio.create_task(path.service.close_session('test reopen'))
                await socket.wait_for_sent('session.close')
                socket.push({'type':'session.closed','reason':'close_requested',
                             'usage':{'seconds':10}})
                await closing
                await path.open_session()
            await self._assert_second_session_uses_its_own_audio_clock(h,reopen)

    async def test_untimed_audio_clock_resets_after_conversation_reset(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session('first')
            async def reopen(path):
                await path.service.reset_conversation()
            await self._assert_second_session_uses_its_own_audio_clock(h,reopen)

    async def test_old_drain_cannot_arm_blocked_later_question(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; s.turn_timeline.begin('wake_review')
            s._user_transcript_seen_for_response=True; g=s.reply_admission
            first=segment(g,'First.',100); second=segment(g,'Unlock?',200)
            armed=[]; finished=[]
            s._arm_confirmation_prompts=lambda:armed.append(True)
            s.on_reply_finished=lambda:finished.append(True)
            await s._deliver_reply(first); old=next(iter(s._drain_batches))
            entered=asyncio.Event(); release=asyncio.Event(); original=s.push_frame
            async def block(frame,*args,**kwargs):
                if isinstance(frame,TTSAudioRawFrame):
                    entered.set(); await release.wait()
                await original(frame,*args,**kwargs)
            s.push_frame=block
            delivery=asyncio.create_task(s._deliver_reply(second))
            await asyncio.wait_for(entered.wait(),2)
            current=next(k for k in s._drain_batches if k!=old)
            s.note_output_drained()
            self.assertFalse(s.note_device_drain('wake_review',metrics(current)))
            s.note_output_drained(old)
            self.assertTrue(s.note_device_drain('wake_review',metrics(old)))
            self.assertEqual(g.enqueued,set()); self.assertEqual(g.pending,{2})
            self.assertEqual((armed,finished),([],[]))
            self.assertFalse(s.note_device_drain('wake_review',metrics(old)))
            release.set(); await delivery
            s.note_output_drained(current)
            self.assertTrue(s.note_device_drain('wake_review',metrics(current)))
            self.assertEqual((armed,finished),([True],[True]))

    async def test_revoked_batch_cannot_complete_replacement(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; s.turn_timeline.begin('wake_review')
            s._user_transcript_seen_for_response=True; g=s.reply_admission
            await s._deliver_reply(segment(g,'Old?',100)); old=next(iter(s._drain_batches))
            await s._invalidate_queued_reply()
            await s._deliver_reply(segment(g,'New?',200)); new=next(iter(s._drain_batches))
            s.note_output_drained(old)
            self.assertFalse(s.note_device_drain('wake_review',metrics(old)))
            self.assertFalse(s.note_device_drain('wake_review',metrics(new)))
            s.note_output_drained(new)
            self.assertTrue(s.note_device_drain('wake_review',metrics(new)))

    async def test_real_transport_marker_follows_partial_chunk_pcm(self):
        async with DevicePath(lead_ms=0,admit_complete_replies=True) as p:
            s=p.service; s._user_transcript_seen_for_response=True
            s.turn_timeline.begin('wake_review')
            observed=[]
            def drained(batch):
                observed.append((batch,p.device.pcm)); s.note_output_drained(batch)
            p.serializer.on_output_batch_end=drained
            reply=segment(s.reply_admission,'Question?',100)
            await s._deliver_reply(reply)
            async def wait_marker():
                while not p.device.text: await asyncio.sleep(.005)
            await asyncio.wait_for(wait_marker(),2)
            marker=json.loads(p.device.text[-1]); batch=marker['output_batch_id']
            self.assertEqual(observed,[(batch,fx.tone(100))])
            self.assertEqual(marker['pcm_bytes'],len(fx.tone(100)))
            self.assertTrue(s.note_device_drain('wake_review',metrics(batch)))

    async def test_real_service_uncertain_close_reserves_full_and_refuses_reopen(self):
        async with LiveHarness(self,on_demand=True,admit_complete_replies=True) as h:
            s=h.service; ledger=LiveSpendLedger(path=None); gate=LiveSessionGate(s,ledger=ledger)
            await gate.open('wake'); await h.start_session(); reserved=gate._reserved_seconds
            async def fail(): raise OSError('synthetic close failure')
            h.socket.close=fail
            with patch('app.live_service.SESSION_CLOSE_TIMEOUT_S',.001): await gate.close('uncertain')
            self.assertGreaterEqual(gate.last_charge.seconds,reserved)
            self.assertIn('unconfirmed',ledger.persistence_error)
            self.assertFalse(await gate.open('late wake'))
            self.assertIsNone(s._websocket); self.assertIsNone(s._receive_task)
            await gate.shutdown()

    async def test_rescue_owns_returned_tool_result_continuation_and_admission(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            executed=[]
            async def local_tool(params):
                executed.append(params.tool_call_id)
                await params.result_callback({'success':True})
            s.register_function('todo__AddItem',local_tool)
            await s._on_transcript_delta('user',{'delta':'add milk to the shopping list','start_ms':0,'end_ms':300})
            await s._end_turn('user')
            with patch('app.live_service.SILENT_RESPONSE_S',.001): await s._silent_response_watch()
            await s._on_response_event(fx.response_created(None))
            # Registered local fake runs through real dispatch/result wiring.
            # No provider, HA, or household action is performed.
            await s._on_response_event(fx.function_call_done('rescue_call','todo__AddItem','{}',None))
            await s._on_response_event(fx.response_completed(None))
            await h.socket.wait_for_sent('response.create',count=2)
            self.assertEqual(s.calls.observed,1)
            self.assertEqual(executed,['rescue_call'])
            self.assertIn('rescue_call',s._evidence.tool_results)
            self.assertEqual(len(h.socket.of('response.create')),2)
            self.assertFalse(s._backend_answer_ready)
            created=fx.response_created(None); created['event']['response']['id']='continuation'
            await s._on_response_event(created)
            completed=fx.response_completed(None); completed['event']['response']['id']='continuation'
            await s._on_response_event(completed)
            self.assertTrue(s._backend_answer_ready)
            reply=segment(s.reply_admission,'Added.',100)
            await s._deliver_reply(reply)
            self.assertIn(reply.segment,s.reply_admission.enqueued)
            await s._on_response_event(fx.response_created(None))
            await s._on_response_event(fx.function_call_done('rescue_call','todo__AddItem','{}',None))
            self.assertEqual(s.calls.observed,1)

    async def test_two_alignment_waits_expire_without_more_provider_events(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; s._user_transcript_seen_for_response=True
            g=s.reply_admission; g.alignment_s=.08; failed=[]
            s.on_silent_response=lambda:failed.append(True)
            for e in fx.output_audio_deltas(fx.tone(100)+fx.silence(1000)): s._on_audio_delta(e)
            await asyncio.sleep(.02)
            await s._on_transcript_delta('assistant',{'delta':'First sentence.'})
            self.assertIsNone(g.alignment_deadline)
            await asyncio.sleep(.02)
            await s._on_transcript_delta('assistant',{'delta':'Question?','end_ms':10000})
            for e in fx.output_audio_deltas(fx.tone(100)+fx.silence(1000)): s._on_audio_delta(e)
            await asyncio.sleep(.12)
            self.assertTrue(g.terminal); self.assertIn('alignment timeout',g.failure_reason)
            self.assertEqual(failed,[True]); self.assertIsNone(s._alignment_task)

    async def test_untimed_audio_uses_continuous_pcm_timeline_for_timestamped_text(self):
        """Reproduce the first Workshop gate's real endpoint contract.

        GPT-Live output audio has no timestamps, output transcript deltas do,
        and the transcript can arrive after the acoustic idle boundary. The
        continuous PCM stream must cover the transcript's end before admission.
        """
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            s._user_transcript_seen_for_response=True
            h.socket.push_all(fx.output_audio_deltas(fx.tone(100)+fx.silence(1000)))
            await asyncio.sleep(.02)
            self.assertIsNotNone(s.reply_admission.alignment_deadline)
            h.socket.push({'type':'session.output_transcript.delta',
                           'delta':'Twelve thirty-four.','start_ms':1100,'end_ms':1400})
            h.socket.push_all(fx.output_audio_deltas(fx.silence(200)))
            await asyncio.sleep(.02)
            self.assertEqual(h.sink.of(TTSAudioRawFrame),[])
            self.assertIsNotNone(s.reply_admission.alignment_deadline)
            h.socket.push_all(fx.output_audio_deltas(fx.silence(100)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertFalse(s.reply_admission.terminal)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)],
                             ['Twelve thirty-four.'])

    async def test_followup_quiet_frames_and_residual_model_cannot_extend_deadline(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; clock=Clock(); fake=FakeLiveService()
            gate=LiveSessionGate(fake,ledger=LiveSpendLedger(path=None),monotonic=clock)
            await gate.open('wake'); gate.note_reply_finished(); deadline=gate._idle_deadline
            fake.response_active=True; s.on_input_audio=gate.note_user_audio
            s.on_user_utterance=gate.note_user_utterance
            for i in range(80):
                clock.advance(.1)
                await s._send_user_audio(InputAudioRawFrame(audio=fx.silence(100,peak=2),sample_rate=24000,num_channels=1))
                gate.note_backend_activity(); gate.note_audible_output()
                self.assertEqual(gate._idle_deadline,deadline)
            clock.advance(.001)
            self.assertEqual(gate._expired(gate.generation),'follow-up quiet')
            await s._on_transcript_delta('user',{'delta':'what time is it','start_ms':8000,'end_ms':8300})
            self.assertEqual(gate._idle_reason,'idle'); self.assertTrue(gate._turn_in_flight)
            await gate.shutdown()


class OwnershipAndPackaging(unittest.TestCase):
    def test_late_retired_client_create_never_belongs_to_new_utterance(self):
        e=TurnEvidence(); self.assertTrue(e.request_client_response('old'))
        e.next_turn(); self.assertFalse(e.request_client_response('new'))
        self.assertFalse(e.accept(fx.response_created(None)))
        self.assertTrue(e.request_client_response('new'))
        self.assertFalse(e.accept(fx.response_created(None)))
        fresh=fx.response_created(None); fresh['event']['response']['id']='fresh'
        self.assertTrue(e.accept(fresh))
        stale_call=fx.function_call_done('late_old','todo__AddItem','{}',None)
        stale_call['event']['response_id']=fx.RESPONSE_ID
        self.assertFalse(e.accept(stale_call))

    def test_full_suite_runtime_assets_exist(self):
        root=Path(__file__).resolve().parents[2]
        self.assertTrue((root/'examples/openclaw-bridge/bridge.mjs').is_file())
        for asset in ('icon.png','logo.png'):
            self.assertTrue((root/'openai_realtime_voice_agent'/asset).is_file())


class ProductionDrainWiring(GateWiringCase):
    async def test_batch_marker_and_firmware_metrics_reach_same_service(self):
        output=[]; device=[]
        with patch.object(PipelineFakeLive,'note_output_drained',lambda _,batch=None:output.append(batch),create=True), \
             patch.object(PipelineFakeLive,'note_device_drain',lambda _,turn,metrics:device.append((turn,metrics)),create=True):
            c=self.build()
            marker={'type':'output_batch_end','output_batch_id':'exact','pcm_bytes':4800,'sample_rate':24000}
            encoded=await c.serializer.serialize(OutputTransportMessageFrame(message=marker))
            self.assertEqual(json.loads(encoded),marker)
            await self.send(c,{'type':'turn_metrics','turn':'wake_review',**metrics('exact')})
            self.assertEqual(output,['exact'])
            self.assertEqual(len(device),1)
            self.assertEqual(device[0][0],'wake_review')
            self.assertEqual(device[0][1]['output_batch_id'],'exact')


# ---------------------------------------------------------------------------
# R9: transcript/audio alignment on the measured Workshop timeline.
#
# Real-provider measurements behind every number (see
# measured against the live provider): GPT-Live timestamps
# output transcripts on its input-sample clock and delivers untimed output PCM
# at 0.95-0.97x real time from session.started. The device audio buffered
# during the wake handshake and flushed at session start puts every transcript
# that much ahead of its PCM: 1.95 s burst -> 2,000-2,300 ms lead, admitted with
# 600 ms owed at the boundary; 3.94 s burst -> 4,200-4,300 ms lead, 2,600 ms
# owed at the boundary, and the frozen R8 admission failed closed after a
# successful llm__GetDateTime with no byte reaching the device, exactly as the
# Workshop gate did. A fixed wall-clock budget cannot cover a shortfall that is
# simply the endpoint's own delivery lag.
# ---------------------------------------------------------------------------
PACE_S = 0.104  # wall seconds per 100 ms delta: 0.952-0.968x delivery measured


class R9AlignmentTimeline(unittest.TestCase):
    def _admission(self, alignment_s=2.):
        clock = Clock()
        return LiveReplyAdmission(alignment_s=alignment_s, clock=clock), clock

    def _speech(self, admission, clock, from_ms, chunks):
        end = from_ms
        for _ in range(chunks):
            end += 100
            self.assertIsNone(admission.feed(OutputChunk(pcm=fx.tone(100), audible=True), False, end_ms=end))
            clock.t += PACE_S
        return end

    def _pause(self, admission, clock, from_ms, chunks=8):
        """The first 800 ms of a pause is forwarded with PCM, like the assembler does."""
        end = from_ms
        for _ in range(chunks):
            end += 100
            self.assertIsNone(admission.feed(OutputChunk(pcm=fx.silence(100), audible=False), False, end_ms=end))
            clock.t += PACE_S
        return end

    def _idle(self, admission, clock, end):
        reply = admission.feed(OutputChunk(reason='idle run exceeded max_silence_ms'), False, end_ms=end)
        clock.t += PACE_S
        return reply

    def test_slow_handshake_replay_waits_for_owed_pcm_instead_of_failing(self):
        """probe-r8-device-slow-handshake.json, reduced to the admission contract."""
        g, clock = self._admission()
        for text, end in ((' 3', 7400), (':25', 7600), (' p.m', 8000), ('.', 8200)):
            g.transcript(text, end_ms=end)
        end = self._speech(g, clock, 2600, 20)      # audible 2600 -> 4600
        end = self._pause(g, clock, end)            # forwarded pause -> 5400
        started = clock.t
        self.assertIsNone(self._idle(g, clock, end + 100))   # boundary at 5500
        self.assertIsNotNone(g.alignment_deadline)
        self.assertEqual(g.owed_ms(), 2700.)
        delivered = end + 100
        reply = None
        while reply is None and delivered < 8200:
            delivered += 100
            reply = self._idle(g, clock, delivered)
            self.assertFalse(g.terminal, g.failure_reason)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, ' 3:25 p.m.')
        self.assertEqual(len(reply.chunks), 28)
        self.assertEqual(g.audio_end_ms, 8200.)
        self.assertIsNone(g.alignment_deadline)
        self.assertFalse(reply.prompt_complete)
        # The wait was longer than the fixed budget that discarded the reply on
        # the Workshop gate: that budget never described the endpoint's pace.
        self.assertGreater(clock.t - started, g.alignment_s)

    def test_fixed_budget_would_have_failed_this_timeline(self):
        """Pin the defect shape: no progress-based extension means a timeout."""
        g, clock = self._admission()
        g.transcript(' 3:25 p.m.', end_ms=8200)
        end = self._pause(g, clock, self._speech(g, clock, 2600, 20))
        self.assertIsNone(self._idle(g, clock, end + 100))
        clock.t += g.alignment_s          # a stalled stream: nothing arrives
        g.check_alignment()
        self.assertTrue(g.terminal)
        self.assertIn('alignment timeout', g.failure_reason)
        self.assertIn('2700 ms still owed', g.failure_reason)

    def test_pause_then_long_tail_is_not_failed_mid_sentence(self):
        """Text leads PCM across a natural pause, then speech outlasts the budget."""
        g, clock = self._admission()
        g.transcript('Twelve thirty-four, and the sky over the workshop is clear.', end_ms=10000)
        end = self._pause(g, clock, self._speech(g, clock, 0, 10))   # 1000 speech, 800 pause
        self.assertIsNone(self._idle(g, clock, end + 100))            # boundary, 8100 owed
        self.assertIsNotNone(g.alignment_deadline)
        end = self._speech(g, clock, end + 100, 30)                   # 3.1 s more speech
        self.assertFalse(g.terminal, g.failure_reason)
        end = self._pause(g, clock, end)
        reply = None
        while reply is None and end < 10000:
            end += 100
            reply = self._idle(g, clock, end)
            self.assertFalse(g.terminal, g.failure_reason)
        self.assertIsNotNone(reply)
        self.assertEqual(len(reply.chunks), 10 + 8 + 30 + 8)
        self.assertEqual(reply.text, 'Twelve thirty-four, and the sky over the workshop is clear.')
        self.assertEqual(g.segments, 1)

    def test_stalled_output_still_fails_closed(self):
        g, clock = self._admission()
        g.transcript('Safe answer.', end_ms=3000)
        end = self._pause(g, clock, self._speech(g, clock, 0, 10))
        end += 100
        self.assertIsNone(self._idle(g, clock, end))         # boundary at 1900
        for _ in range(5):                                   # progress keeps it alive
            end += 100
            self.assertIsNone(self._idle(g, clock, end))
        self.assertFalse(g.terminal)
        deadline = g.alignment_deadline
        clock.t = deadline - 0.001
        g.check_alignment()
        self.assertFalse(g.terminal)
        clock.t = deadline
        g.check_alignment()
        self.assertTrue(g.terminal)
        self.assertIn('alignment timeout', g.failure_reason)
        self.assertIn('600 ms still owed', g.failure_reason)

    def test_shortfall_no_session_can_explain_fails_closed_at_once(self):
        g, clock = self._admission()
        g.transcript('Broken timeline.', end_ms=15000)
        end = self._pause(g, clock, self._speech(g, clock, 0, 10))
        self.assertIsNone(self._idle(g, clock, end + 100))
        self.assertTrue(g.terminal)
        self.assertIn('alignment diverged', g.failure_reason)
        self.assertIn('13100 ms owed', g.failure_reason)

    def test_waiting_for_a_missing_transcript_is_not_extended_by_silence(self):
        """More PCM brings no text: the late-transcript wait keeps its fixed bound."""
        g, clock = self._admission()
        end = self._pause(g, clock, self._speech(g, clock, 0, 10))
        self.assertIsNone(self._idle(g, clock, end + 100))
        self.assertIsNotNone(g.alignment_deadline)
        deadline = g.alignment_deadline
        for _ in range(25):
            end += 100
            self.assertIsNone(self._idle(g, clock, end))
            if g.terminal:
                break
        self.assertTrue(g.terminal)
        # Failed at the first delta on or after the original deadline; the
        # helper advances the clock once more after that delta.
        self.assertLessEqual(clock.t - deadline, 2 * PACE_S)
        self.assertIn('0 ms still owed', g.failure_reason)


class R9AlignmentService(unittest.IsolatedAsyncioTestCase):
    async def test_prestart_burst_lead_is_awaited_through_the_real_service(self):
        """The production path: device audio buffered through the handshake,
        flushed at session.started, then a transcript ahead of its PCM."""
        from pipecat.processors.frame_processor import FrameDirection
        async with LiveHarness(self, on_demand=True, admit_complete_replies=True) as h:
            s = h.service
            for _ in range(20):      # 2.0 s of device audio before the session exists
                await h.source.push_frame(InputAudioRawFrame(audio=fx.silence(100), sample_rate=24000, num_channels=1), FrameDirection.DOWNSTREAM)
            await h.open_session(); await h.start_session('burst')
            await h.socket.wait_for_sent('session.input_audio.append', 20)
            self.assertEqual(s._input_audio_ms_sent, 2000.)
            s._user_transcript_seen_for_response = True
            g = s.reply_admission; g.alignment_s = .15
            h.socket.push({'type': 'session.output_transcript.delta', 'delta': 'Twenty-one minutes past three.', 'start_ms': 1500, 'end_ms': 1900})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(400) + fx.silence(900)))
            await asyncio.sleep(.03)
            self.assertIsNotNone(g.alignment_deadline)
            self.assertEqual(g.owed_ms(), 600.)
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
            # 600 ms owed, delivered at 100 ms per 50 ms of wall: 0.3 s in all,
            # twice the stall budget. Progress keeps the wait alive.
            for _ in range(6):
                await asyncio.sleep(.05)
                h.socket.push_all(fx.output_audio_deltas(fx.silence(100)))
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertFalse(g.terminal, g.failure_reason)
            self.assertEqual([f.text for f in h.sink.of(TTSTextFrame)], ['Twenty-one minutes past three.'])
            self.assertEqual(g.audio_end_ms, 1900.)
            async def watcher_gone():
                while s._alignment_task is not None:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(watcher_gone(), 1)

    async def test_real_service_stall_after_burst_still_closes_the_turn(self):
        async with LiveHarness(self, admit_complete_replies=True) as h:
            await h.start_session(); s = h.service
            s._user_transcript_seen_for_response = True
            g = s.reply_admission; g.alignment_s = .1
            failed = []
            s.on_silent_response = lambda: failed.append(True)
            h.socket.push({'type': 'session.output_transcript.delta', 'delta': 'Lost reply.', 'start_ms': 1500, 'end_ms': 4000})
            h.socket.push_all(fx.output_audio_deltas(fx.tone(400) + fx.silence(900)))
            await asyncio.sleep(.03)
            self.assertIsNotNone(g.alignment_deadline)
            await asyncio.sleep(.25)
            self.assertTrue(g.terminal)
            self.assertIn('alignment timeout', g.failure_reason)
            self.assertEqual(failed, [True])
            self.assertEqual(h.sink.of(TTSAudioRawFrame), [])
