"""Provider-neutral tool guards shared by every voice runtime.

Both the Realtime service and the GPT-Live service register the same function
tools (Home Assistant via MCP, web search, timers, memory, enrollment,
confirmations, OpenClaw). The safety and telemetry wrapped around every tool
call must be identical regardless of which model requested it:

* speaker gate (male_only_tools) — enforced below the model;
* consequential-action gate (app/action_gate.py) — hold until confirmed;
* turn liveness ticks for the phase emitter's thinking watchdog;
* per-turn timeline tool records;
* the one-shot slow-tool acknowledgement.

This mixin was extracted from SafeRealtimeLLMService (main.py) so both runtimes
share one implementation. It also fails closed when guard evaluation raises or
a handler returns without submitting a result, preventing silent or falsely
successful tool calls. It expects the host class to be a pipecat ``LLMService`` and to carry these
attributes (set by Application.create_openai_service): ``speaker_probe``,
``male_only_tools``, ``action_gate``, ``spoken_prompts``, ``turn_liveness``,
``turn_timeline``, ``device_id``.
"""
import asyncio
import dataclasses
import logging

from app.action_gate import confirmation_result, replace_arguments

logger = logging.getLogger(__name__)


class GuardedToolsMixin:
    """register_function() with the speaker/action gates and liveness ticks."""

    def register_function(self, function_name, handler, start_callback=None, *,
                          cancel_on_interruption: bool = True):  # type: ignore[override]
        """Force cancel_on_interruption=False for every tool registration.

        pipecat cancels in-flight function-call tasks on EVERY user-speech
        interruption — and semantic_vad fires one per utterance fragment, so
        merely continuing your own sentence kills the tool call your previous
        fragment started. By then the HTTP request to Home Assistant has
        usually already been SENT: the action executes, but its result never
        reaches the model, which then tells the user it failed (observed
        live: the lights turned ON while the assistant claimed they
        wouldn't). Our tools are all short-lived (HA service calls, one web
        search), so letting them finish and report the truth always beats
        killing them halfway. This single override covers every registration
        path (MCP tools via pipecat's MCPClient, web_search, disconnect).

        The handler is also wrapped to tick its connection's liveness around its run, so
        the PhaseEmitter's thinking-watchdog knows a tool is in flight and a
        slow tool (web search: 10-20 s of pipeline silence) is never mistaken
        for a dead turn. All our handlers use the single-param
        FunctionCallParams signature, so the wrapper does too (pipecat
        inspects the signature to pick the calling convention).
        """
        async def liveness_tracked(params):
            # Speaker gate (fork): tools listed in male_only_tools only execute
            # when the last voice-type verdict is "male". Enforced HERE — below
            # the model — so prompt tricks can't bypass it. Fails closed on
            # uncertain/stale/absent verdicts. This is convenience gating on a
            # voice-type heuristic, not biometric auth.
            if self.male_only_tools and function_name in self.male_only_tools:
                speaker = self.speaker_probe.gate_speaker() if self.speaker_probe else "unknown"
                if speaker != "male":
                    owner = (self.speaker_probe.male_name if self.speaker_probe else "") or "the owner"
                    logger.info(f"⛔ speaker gate blocked '{function_name}' (speaker={speaker})")
                    await params.result_callback({
                        "error": (
                            f"Not available: this capability is reserved for {owner}, "
                            f"and the current speaker's voice was not recognized as {owner}. "
                            f"Relay this politely."
                        )
                    })
                    return
            # Consequential-action gate (app/action_gate.py): unlock/open/disarm
            # calls are held until the user answers a confirmation question.
            # Enforced here, below the model, like the speaker gate.
            gate = getattr(self, "action_gate", None)
            try:
                if gate is not None:
                    reconciled = await gate.reconcile_arguments(function_name, params.arguments)
                    if reconciled != dict(params.arguments or {}):
                        params = replace_arguments(params, function_name, reconciled)
                if gate is not None and gate.enabled and function_name != "confirm_action":
                    decision = await gate.check(function_name, params.arguments)
                    if decision.requires_confirmation:
                        # Live can receive a delegated call before the transcript
                        # for the utterance which caused it. Give runtimes a way
                        # to fence that still-unobserved utterance off so its late
                        # transcript cannot satisfy its own confirmation prompt.
                        request_context = getattr(self, "gate_request_context", self.gate_context)
                        device_id, user_seq, wake_seq = request_context()
                        pending = gate.request(
                            device_id, function_name, dict(params.arguments or {}), handler,
                            decision.summary, user_seq, wake_seq,
                            require_prompt_boundary=self.confirmation_requires_spoken_prompt_boundary(),
                        )
                        await params.result_callback(confirmation_result(pending, gate.window_s))
                        return
            except Exception:
                logger.exception("tool guard failed before dispatch: %s", function_name)
                await params.result_callback({
                    "error": (
                        f"{function_name} could not be safely checked before execution; "
                        "it was not run. Do not retry automatically."
                    )
                })
                return
            timeline = getattr(self, "turn_timeline", None)
            record = timeline.tool_started(function_name) if timeline is not None else None
            self.turn_liveness.tool_started()
            ack = self._schedule_slow_tool_ack(function_name)
            ok = False
            result_reported = False
            original_result_callback = params.result_callback

            async def tracked_result_callback(result, *, properties=None):
                nonlocal result_reported
                result_reported = True
                if properties is None:
                    return await original_result_callback(result)
                return await original_result_callback(result, properties=properties)

            params = dataclasses.replace(params, result_callback=tracked_result_callback)
            try:
                result = await handler(params)
                if not result_reported:
                    await params.result_callback({
                        "error": (
                            f"{function_name} returned without reporting whether it completed; "
                            "it may or may not have completed. Do not retry automatically."
                        )
                    })
                ok = result_reported
                return result
            except Exception:
                logger.exception("tool handler failed before completing: %s", function_name)
                if not result_reported:
                    await params.result_callback({
                        "error": (
                            f"{function_name} failed before reporting whether it completed; "
                            "it may or may not have completed. Do not retry automatically."
                        )
                    })
                return None
            finally:
                # Never cut an acknowledgement off mid-word; only a pending
                # (not yet started) one is cancelled when the tool finishes.
                if ack is not None and not ack.ack_state["playing"]:
                    ack.cancel()
                if timeline is not None:
                    timeline.tool_finished(record, ok)
                self.turn_liveness.tool_finished()

        super().register_function(
            function_name, liveness_tracked, start_callback, cancel_on_interruption=False
        )

    def gate_context(self):
        """(device_id, user_turn_seq, wake_seq) for confirmation bookkeeping."""
        timeline = getattr(self, "turn_timeline", None)
        if timeline is None:
            return getattr(self, "device_id", ""), 0, 0
        return timeline.device_id, timeline.user_turn_seq, timeline.wake_seq

    def gate_request_context(self):
        """Confirmation context at hold time; runtimes may add a safety fence."""
        return self.gate_context()

    def confirmation_requires_spoken_prompt_boundary(self) -> bool:
        return False

    def _schedule_slow_tool_ack(self, function_name: str):
        """Play one short acknowledgement if a slow tool is still running.

        Only tools known to be slow (or whose measured median exceeds the
        threshold) qualify, only after ack_delay_s, at most once per turn, and
        never while the assistant is already speaking.
        """
        prompts = getattr(self, "spoken_prompts", None)
        timeline = getattr(self, "turn_timeline", None)
        if prompts is None or not prompts.enabled or timeline is None:
            return None
        p50 = timeline.stats.p50(f"tool.{function_name}")
        if not prompts.is_slow(function_name, None if p50 is None else p50 / 1000.0):
            return None
        turn = timeline.current
        state = {"playing": False}

        async def _ack():
            await asyncio.sleep(prompts.ack_delay_s)
            if turn is None or turn.done or turn.meta.get("ack_played"):
                return
            if "first_audio_sent" in turn.stamps and "bot_stopped" not in turn.stamps:
                return  # the assistant is speaking right now
            turn.meta["ack_played"] = True
            state["playing"] = True
            await prompts.say("ack", timeline.device_id)

        task = asyncio.create_task(_ack())
        task.ack_state = state
        return task
