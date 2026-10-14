"""Astra B1–B7 counterexamples, device-free and synthetic only."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from app.live_evidence import TurnEvidence
from app.live_cost import LiveSpendLedger
from app.live_session_gate import LiveSessionGate, LiveSessionPolicy
from app.live_reply import LiveReplyAdmission
from app.live_audio import LiveOutputAudio
from tests import live_fixtures as fx
from tests.live_harness import LiveHarness
from tests.test_live_session_gate import FakeLiveService, Clock
from pipecat.frames.frames import TTSAudioRawFrame


def response(kind, rid, did='d', output=None):
    return {'type':'response.event','delegation_id':did,
            'event':{'type':kind,'response':{'id':rid,'output':output or []}}}


class EvidenceTests(unittest.TestCase):
    def test_old_completion_and_no_tool_completion_never_unlock_new_turn(self):
        e=TurnEvidence(); e.delegate('old')
        e.accept(response('response.created','r','old'))
        e.accept(response('response.completed','r','old'))
        e.next_turn(); e.delegate('new')
        self.assertFalse(e.accept(response('response.completed','r','old')))
        e.accept(response('response.created','new','new'))
        e.accept(response('response.completed','new','new'))
        self.assertFalse(e.ready)

    def test_result_requires_completed_continuation_and_replay_has_no_effect(self):
        e=TurnEvidence(); e.delegate('d')
        e.accept(response('response.created','r'))
        e.calls['call']=e.generation
        e.accept(response('response.completed','r'))
        self.assertTrue(e.result('call')); e.continuation()
        self.assertFalse(e.ready)
        self.assertFalse(e.accept(response('response.completed','r')))
        e.accept(response('response.created','cont'))
        self.assertFalse(e.ready)
        e.accept(response('response.completed','cont'))
        self.assertTrue(e.ready)

    def test_structured_refusal_is_distinct_from_tool_success(self):
        e=TurnEvidence(); e.delegate('d')
        e.accept(response('response.created','r'))
        e.accept(response('response.completed','r',output=[{'content':[{'type':'refusal','refusal':'No'}]}]))
        self.assertTrue(e.ready); self.assertEqual(e.kind,'refusal')


class AdmissionTests(unittest.TestCase):
    def feed(self,g,a,pcm,busy=False):
        out=[]
        for event in fx.output_audio_deltas(pcm):
            r=g.feed(a.feed(event['delta']),busy)
            if r: out.append(r)
        return out

    def test_natural_pause_over_800ms_keeps_second_sentence(self):
        g,a=LiveReplyAdmission(),LiveOutputAudio()
        g.transcript('First sentence. Second sentence.')
        one=self.feed(g,a,fx.tone(500)+fx.silence(1000))
        two=self.feed(g,a,fx.tone(500)+fx.silence(1000))
        self.assertEqual(len(one+two),2)
        self.assertEqual(sum(len(c.pcm) for r in one+two for c in r.chunks),2*len(fx.tone(500)+fx.silence(800)))

    def test_oversized_response_cannot_recover_as_tail(self):
        g,a=LiveReplyAdmission(max_seconds=2),LiveOutputAudio()
        g.transcript('Too long')
        self.feed(g,a,fx.tone(2200))
        self.feed(g,a,fx.tone(400,amplitude=60))
        g.transcript('Tail only')
        self.assertEqual(self.feed(g,a,fx.tone(200)+fx.silence(1000)),[])
        self.assertTrue(g.terminal)

    def test_more_than_30_seconds_across_segments_is_not_truncated(self):
        g,a=LiveReplyAdmission(),LiveOutputAudio()
        replies=[]
        for i in range(4):
            g.transcript('Sentence.'); replies += self.feed(g,a,fx.tone(8000)+fx.silence(1000))
        self.assertEqual(len(replies),4)
        self.assertFalse(g.terminal)

    def test_late_transcript_before_alignment_deadline_recovers_exact_audio(self):
        clock=Clock(); g,a=LiveReplyAdmission(clock=clock),LiveOutputAudio()
        self.assertEqual(self.feed(g,a,fx.tone(200)+fx.silence(1000)),[])
        g.transcript('Late text')
        replies=self.feed(g,a,fx.silence(100))
        self.assertEqual(len(replies),1)
        self.assertEqual(b''.join(c.pcm for c in replies[0].chunks),fx.tone(200)+fx.silence(800))

    def test_alignment_timeout_is_terminal_and_observable(self):
        clock=Clock(); g,a=LiveReplyAdmission(clock=clock),LiveOutputAudio()
        self.feed(g,a,fx.tone(200)+fx.silence(1000)); clock.advance(3)
        g.check_alignment(); g.transcript('Late')
        self.assertTrue(g.terminal); self.assertIn('alignment',g.failure_reason)
        self.assertEqual(self.feed(g,a,fx.tone(100)+fx.silence(1000)),[])

    def test_text_before_any_recovery_pcm_is_retained(self):
        g,a=LiveReplyAdmission(),LiveOutputAudio()
        self.feed(g,a,fx.tone(300),True)
        g.transcript('Final answer')
        self.feed(g,a,fx.tone(400,amplitude=60))
        out=self.feed(g,a,fx.tone(300)+fx.silence(1000))
        self.assertEqual([r.text for r in out],['Final answer'])


class PersistenceTests(unittest.TestCase):
    def test_restart_charges_active_reservation_conservatively(self):
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'budget.json')
            ledger=LiveSpendLedger(path=path,max_session_minutes_per_day=1,max_usd_per_day=0)
            self.assertIsNotNone(ledger.reserve(60,lambda:17))
            restarted=LiveSpendLedger(path=path,max_session_minutes_per_day=1,max_usd_per_day=0)
            self.assertEqual(restarted.window_seconds,60)
            self.assertFalse(restarted.check())

    def test_concurrent_owners_aggregate_and_reserve_before_open(self):
        l=LiveSpendLedger(path=None,max_session_minutes_per_day=1,max_usd_per_day=0)
        self.assertIsNotNone(l.reserve(30,lambda:40))
        self.assertIsNotNone(l.reserve(30,lambda:40))
        self.assertEqual(l.active_seconds,80)
        self.assertFalse(l.check())
        self.assertIsNone(l.reserve(10,lambda:0))

    def test_missing_directory_and_failed_write_refuse_session(self):
        with tempfile.TemporaryDirectory() as d:
            l=LiveSpendLedger(path=os.path.join(d,'missing','ledger.json'))
            self.assertFalse(l.check()); self.assertIsNone(l.reserve(60,lambda:0))
            l=LiveSpendLedger(path=os.path.join(d,'ledger.json'))
            with patch('app.live_cost.os.replace',side_effect=PermissionError):
                self.assertIsNone(l.reserve(60,lambda:0))
            self.assertFalse(l.check())


class AsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_contiguous_input_retains_full_tool_requirement(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session()
            await h.service._on_transcript_delta('user',{'delta':'turn on the ','start_ms':0,'end_ms':500})
            await h.service._end_turn('user')
            await h.service._on_transcript_delta('user',{'delta':'kitchen lights','start_ms':500,'end_ms':900})
            await h.service._end_turn('user')
            self.assertEqual(h.service._last_user_text,'turn on the kitchen lights')
            self.assertTrue(h.service._requires_backend_for_reply)

    async def test_delegation_revokes_reply_while_output_task_is_blocked(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session()
            s=h.service; s._user_transcript_seen_for_response=True
            g=s.reply_admission; a=s.audio; g.transcript('Acknowledgement')
            reply=None
            for e in fx.output_audio_deltas(fx.tone(300)+fx.silence(1000)):
                reply=g.feed(a.feed(e['delta']),False) or reply
            entered,release=asyncio.Event(),asyncio.Event()
            async def blocked():
                entered.set(); await release.wait()
            s.on_assistant_response_started(blocked)
            task=asyncio.create_task(s._deliver_reply(reply)); await entered.wait()
            await s._on_delegation_created(fx.delegation_created())
            release.set(); await task
            self.assertFalse(g.can_deliver(reply))
            self.assertEqual(h.sink.of(TTSAudioRawFrame),[])

    async def test_delayed_close_includes_final_usage(self):
        clock=Clock(); service=FakeLiveService()
        async def close(reason):
            clock.advance(3); service.session_seconds=13
        service.close_session=close
        ledger=LiveSpendLedger(path=None)
        gate=LiveSessionGate(service,ledger=ledger,monotonic=clock)
        await gate.open('test'); clock.advance(10); await gate.close('test')
        self.assertEqual(gate.last_charge.seconds,13)

    async def test_stop_racing_recovery_close_never_reopens(self):
        service=FakeLiveService(); entered,release=asyncio.Event(),asyncio.Event()
        gate=LiveSessionGate(service,ledger=LiveSpendLedger(path=None))
        await gate.open('test')
        async def close(reason):
            entered.set(); await release.wait()
        service.close_session=close
        recovery=asyncio.create_task(gate.recover('drop')); await entered.wait()
        stop=asyncio.create_task(gate.close('device stop')); await asyncio.sleep(0)
        release.set(); self.assertFalse(await recovery); await stop
        self.assertEqual(service.opens,1); self.assertFalse(gate.is_open)

    async def test_drain_not_enqueue_arms_prompt_and_followup(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            done=[]; armed=[]
            s.on_reply_finished=lambda:done.append(True)
            s._arm_confirmation_prompts=lambda:armed.append(True)
            s._drain_pending={'generation':s.reply_admission.generation,'segment':1,'enqueued':True,'turn_id':'test','output_drained':False,'prompt_complete':True}
            s._drain_batches['batch']=s._drain_pending
            s.reply_admission.enqueued.add(1)
            self.assertFalse(s.note_device_drain('test',{'seq':0,'audible_to_drained_ms':30000}))
            s.note_output_drained('batch')
            self.assertFalse(s.note_device_drain('old',{'seq':0,'audible_to_drained_ms':30000}))
            self.assertTrue(s.note_device_drain('test',{'seq':0,'audible_to_drained_ms':30000,'output_batch_id':'batch'}))
            self.assertEqual((done,armed),([True],[True]))
            self.assertFalse(s.note_device_drain('test',{'seq':0,'audible_to_drained_ms':30000}))

from tests.test_live_gate_wiring import GateWiringCase

class RecoveryPathTests(GateWiringCase):
    async def test_actual_connection_recovery_is_invalidated_during_close(self):
        connection=self.build(); gate=connection.session_gate; service=connection.openai_service
        await gate.open('wake')
        entered,release=asyncio.Event(),asyncio.Event()
        original=service.close_session
        async def blocked(reason):
            entered.set(); await release.wait(); await original(reason)
        service.close_session=blocked
        task=asyncio.create_task(connection.recovery.force_reconnect('test',unstick=False))
        await asyncio.wait_for(entered.wait(),2)
        stop=asyncio.create_task(gate.close('device stop')); await asyncio.sleep(0)
        release.set(); await task; await stop
        self.assertEqual(service.opens,1); self.assertEqual(service.resets,0)
        self.assertFalse(gate.is_open)

    async def test_actual_connection_recovery_is_invalidated_during_open(self):
        connection=self.build(); gate=connection.session_gate; service=connection.openai_service
        await gate.open('wake')
        entered,release=asyncio.Event(),asyncio.Event()
        original=service.open_session
        async def blocked():
            entered.set(); await release.wait(); await original()
        service.open_session=blocked
        task=asyncio.create_task(connection.recovery.force_reconnect('test',unstick=False))
        await asyncio.wait_for(entered.wait(),2)
        stop=asyncio.create_task(gate.shutdown()); await asyncio.sleep(0)
        release.set(); await task; await stop
        self.assertEqual(service.resets,0); self.assertFalse(gate.is_open)
        self.assertIn('open invalidated',service.closes)
        self.assertFalse(await gate.open('late wake'))

class ServiceEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_replayed_completion_after_new_time_request_has_no_usage_or_ready_side_effect(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            await s._on_transcript_delta('user',{'delta':'What time is it?','start_ms':0,'end_ms':500})
            await s._on_delegation_created(fx.delegation_created())
            await s._on_response_event(fx.response_created())
            await s._on_response_event(fx.response_completed())
            self.assertFalse(s._backend_answer_ready,'no tool is not a clock lookup')
            usage=dict(s.backend_tokens)
            await s._end_turn('user')
            await s._on_transcript_delta('user',{'delta':'Turn on the light','start_ms':5000,'end_ms':6000})
            await s._on_response_event(fx.response_completed())
            self.assertFalse(s._backend_answer_ready)
            self.assertEqual(s.backend_tokens,usage)

    async def test_revocation_after_first_chunk_stops_remaining_audio(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; s._user_transcript_seen_for_response=True
            g=s.reply_admission; g.transcript('Initial claim.')
            reply=None
            for event in fx.output_audio_deltas(fx.tone(700)+fx.silence(1000)):
                reply=g.feed(s.audio.feed(event['delta']),False) or reply
            original=s.push_frame; heard=[]
            async def push(frame,*args,**kwargs):
                await original(frame,*args,**kwargs)
                if isinstance(frame,TTSAudioRawFrame):
                    heard.append(frame)
                    await s._on_delegation_created(fx.delegation_created())
            s.push_frame=push
            await s._deliver_reply(reply)
            self.assertEqual(len(heard),1)
            self.assertFalse(g.can_deliver(reply)); self.assertFalse(g.delivered)

    async def test_drain_timeout_never_arms_confirmation(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; armed=[]; closed=[]
            s._arm_confirmation_prompts=lambda:armed.append(True)
            s.on_silent_response=lambda:closed.append(True)
            pending={'generation':s.reply_admission.generation,'turn_id':'test','output_drained':True}
            s._drain_pending=pending
            await s._drain_watch(pending,0)
            self.assertEqual(armed,[]); self.assertEqual(closed,[True])
            self.assertFalse(s.note_device_drain('test',{'seq':0,'audible_to_drained_ms':30}))

    async def test_followup_uses_matching_sequence_and_full_window_after_long_reply(self):
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service; clock=Clock()
            service=FakeLiveService(); gate=LiveSessionGate(service,ledger=LiveSpendLedger(path=None),monotonic=clock)
            await gate.open('wake'); clock.advance(30)
            s.on_reply_finished=gate.note_reply_finished
            s._drain_pending={'generation':s.reply_admission.generation,'segment':1,'enqueued':True,'turn_id':'test.f2','output_drained':True}
            s._drain_batches['batch']=s._drain_pending
            s.reply_admission.enqueued.add(1)
            self.assertFalse(s.note_device_drain('test',{'seq':1,'audible_to_drained_ms':30000}))
            self.assertTrue(s.note_device_drain('test',{'seq':2,'audible_to_drained_ms':30000,'output_batch_id':'batch'}))
            clock.advance(7.99); self.assertIsNone(gate._expired(gate.generation))
            clock.advance(.01); self.assertEqual(gate._expired(gate.generation),'follow-up quiet')
            await gate.shutdown()

class AdditionalEvidenceTests(unittest.TestCase):
    def test_context_lookup_is_not_evidence_for_clock_or_action(self):
        for requirement in ['clock','action']:
            e=TurnEvidence(); e.requirement=requirement; e.delegate('d')
            e.accept(response('response.created','r'))
            e.calls['c']=e.generation; e.call_names['c']='GetLiveContext'
            e.accept(response('response.completed','r')); e.result('c'); e.continuation()
            e.accept(response('response.created','final')); e.accept(response('response.completed','final'))
            self.assertFalse(e.ready)

    def test_no_tool_clarification_is_distinct_from_claim(self):
        for text,allowed in [('Which light?',True),('It is twelve thirty-four.',False),('Done. Which light?',False)]:
            e=TurnEvidence();e.requirement='clock';e.delegate('d');e.accept(response('response.created','r'))
            e.accept(response('response.completed','r',output=[{'content':[{'type':'output_text','text':text}]}]))
            self.assertEqual(e.ready,allowed)
            if allowed:self.assertEqual(e.kind,'clarification')

    def test_question_cannot_be_complete_before_its_timestamped_audio(self):
        g,a=LiveReplyAdmission(),LiveOutputAudio()
        g.transcript('Do you want to unlock the test fixture?',end_ms=3000)
        out=[]
        for event in fx.output_audio_deltas(fx.tone(300)+fx.silence(1000)):
            reply=g.feed(a.feed(event['delta']),False,end_ms=1000)
            if reply:out.append(reply)
        self.assertEqual(out,[])
        for event in fx.output_audio_deltas(fx.tone(300)+fx.silence(1000)):
            reply=g.feed(a.feed(event['delta']),False,end_ms=3500)
            if reply:out.append(reply)
        self.assertEqual(len(out),1);self.assertTrue(out[0].prompt_complete)
        self.assertEqual(sum(len(c.pcm) for c in out[0].chunks),2*len(fx.tone(300)+fx.silence(800)))

    def test_actual_30_second_single_segment_overflow_is_terminal(self):
        g,a=LiveReplyAdmission(),LiveOutputAudio()
        g.transcript('A long continuous answer')
        for event in fx.output_audio_deltas(fx.tone(30100)):
            self.assertIsNone(g.feed(a.feed(event['delta']),False))
        self.assertTrue(g.terminal)
        g.transcript('A suffix')
        for event in fx.output_audio_deltas(fx.tone(400,amplitude=60)+fx.tone(200)+fx.silence(1000)):
            self.assertIsNone(g.feed(a.feed(event['delta']),False))

class PendingContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_audio_before_continuation_created_is_held_then_delivered(self):
        from pipecat.frames.frames import LLMFullResponseEndFrame
        from tests.test_live_reply import continuation_complete
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            async def tool(params):await params.result_callback({'time':'12:34'})
            s.register_function('llm__GetDateTime',tool)
            await s._on_transcript_delta('user',{'delta':'What time is it?','start_ms':0,'end_ms':500})
            h.socket.push_all(fx.function_call_sequence('pending_time','llm__GetDateTime','{}'))
            await h.socket.wait_for_sent('response.create')
            h.socket.push({'type':'session.output_transcript.delta','delta':'Twelve thirty-four.'})
            pcm=fx.tone(400)+fx.silence(1000)
            h.socket.push_all(fx.output_audio_deltas(pcm))
            await asyncio.sleep(.03)
            self.assertFalse(s._backend_answer_ready)
            self.assertEqual(h.sink.of(TTSAudioRawFrame),[])
            h.socket.push_all(continuation_complete())
            await h.sink.wait_for(LLMFullResponseEndFrame)
            self.assertEqual(b''.join(f.audio for f in h.sink.of(TTSAudioRawFrame)),fx.tone(400)+fx.silence(800))
            self.assertEqual(len(h.socket.of('response.create')),1)

class ReservationMarginTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_must_cover_handshake_close_and_supervisor_margin(self):
        service=FakeLiveService()
        ledger=LiveSpendLedger(path=None,max_session_minutes_per_day=20/60,max_usd_per_day=0)
        gate=LiveSessionGate(service,ledger=ledger)
        self.assertFalse(await gate.open('insufficient reserved handshake time'))
        self.assertEqual(service.opens,0)
        self.assertEqual(ledger.active_seconds,0)


class DeliveryStateTests(unittest.TestCase):
    def test_admission_and_enqueue_do_not_claim_device_delivery(self):
        from app.live_reply import Reply
        g=LiveReplyAdmission();g.pending.add(1)
        reply=Reply([], 'A reply', g.generation, 1)
        self.assertFalse(g.delivered)
        g.mark_enqueued(reply)
        self.assertFalse(g.delivered)
        self.assertEqual(g.enqueued,{1})
        g.mark_delivered(g.generation - 1)
        self.assertFalse(g.delivered)
        g.mark_delivered(g.generation,1)
        self.assertTrue(g.delivered)
        self.assertEqual(g.enqueued,set())


class ProvisionalTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_before_first_transcript_keeps_identity_and_no_repeat_fence(self):
        from tests.test_live_reply import continuation_complete
        async with LiveHarness(self,admit_complete_replies=True) as h:
            await h.start_session(); s=h.service
            async def tool(params):await params.result_callback({'time':'12:34'})
            s.register_function('llm__GetDateTime',tool)
            h.socket.push_all(fx.function_call_sequence('early_time','llm__GetDateTime','{}'))
            await h.socket.wait_for_sent('response.create')
            h.socket.push_all(continuation_complete())
            for _ in range(100):
                if s._backend_answer_ready:break
                await asyncio.sleep(.005)
            self.assertTrue(s._backend_answer_ready)
            generation=s._evidence.generation
            await s._on_transcript_delta('user',{'delta':'What time is it?','start_ms':0,'end_ms':500})
            self.assertEqual(s._evidence.generation,generation)
            self.assertTrue(s._backend_answer_ready)
            await s._end_turn('user')
            with patch('app.live_service.SILENT_RESPONSE_S',.001):
                await s._silent_response_watch()
            self.assertEqual(len(h.socket.of('response.create')),1)
