"""How a tool gets registered, identically on every engine.

This used to live inside `SafeRealtimeLLMService`, so it protected the OpenAI
path only. The house then moved to Gemini and the protection stayed behind:
measured live 2026-09-09 22:23, `play_media` was cancelled 81 ms after it
started while the assistant said "jajemän, fixar det" and nothing played.
Same for `GetLiveContext` and `vaderprognos`, all evening.

Keeping it in one mixin is the point. A rule that has to be remembered twice
is a rule that protects one engine.
"""
import asyncio
import json
import logging
import os
import time

from app.early_ack import paa as early_ack_paa

logger = logging.getLogger(__name__)


def _result_max_chars() -> int:
    try:
        return max(0, int(os.environ.get("TOOL_RESULT_MAX_CHARS", "6000")))
    except ValueError:
        return 6000


def cap_tool_result(result, max_chars: int):
    """Shorten a tool result that would flood the conversation.

    Every later turn re-bills everything in the conversation. One unfiltered
    GetLiveContext is the whole house, 16 kB / ~6,300 tokens: measured live
    2026-10-02, a turn went 8,767 -> 15,026 input tokens and the next
    sentence hit the 40k TPM limit. 6,000 chars still fits a whole-domain
    query (all lights: 5.1 kB). The note tells the model how to get the rest.
    """
    if not max_chars or result is None:
        return result
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if len(text) <= max_chars:
        return result
    cut = text[:max_chars]
    cut = cut[: max(cut.rfind("\n"), cut.rfind("\\n"), max_chars // 2)]
    return (cut + f"\n[TRUNCATED: {len(text) - len(cut)} of {len(text)} characters cut. "
            "Ask again with a narrower filter (name, area or domain).]")


def _early_ack_ms() -> int:
    if not early_ack_paa():
        return 0
    try:
        return max(0, int(os.environ.get("EARLY_ACK_MS", "700")))
    except ValueError:
        return 700


def _silence_ack_ms() -> int:
    if not early_ack_paa():
        return 0
    try:
        return max(0, int(os.environ.get("EARLY_ACK_SILENCE_MS", "1500")))
    except ValueError:
        return 1500


def _followup_ack_ms() -> int:
    if not early_ack_paa():
        return 0
    try:
        return max(0, int(os.environ.get("EARLY_ACK_FOLLOWUP_MS", "3000")))
    except ValueError:
        return 3000


def ack_delay_ms(wake_ms: int, liveness) -> int:
    """How long to wait before "jag kollar" in this turn; 0 = never.

    The owner, 2026-10-02 21:16: the ack is good on the first question after
    the wake word, not on follow-ups in the same conversation, unless the wait
    is really long. A follow-up turn (no wake since the last idle) waits
    EARLY_ACK_FOLLOWUP_MS (3000, 0 = never) for both triggers.
    """
    if wake_ms <= 0 or liveness is None or liveness.from_wake():
        return wake_ms
    return _followup_ack_ms()


# Loop guard (0.25.1), every engine: Grok called GetLiveContext up to 48
# times in one turn with made-up arguments when the answer was not there
# (probe 2026-10-02). Past the limit HA is not called; the model is told to
# answer with what it has. 0.25.6: IDENTICAL calls are counted (same tool,
# same arguments) -- "tänd kontoret, köket, hallen och sovrummet" is four
# HassTurnOn and all four must run.
MAX_SAME_TOOL_PER_TURN = 3
MAX_TOOLS_PER_TURN = 12


def _norm(value):
    if isinstance(value, str):
        return " ".join(value.lower().split())
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _call_key(function_name, arguments) -> str:
    """The tool and its arguments, normalized (key order, case, spaces)."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    return function_name + " " + json.dumps(_norm(arguments or {}), sort_keys=True,
                                            ensure_ascii=False, default=str)


def loop_stop(liveness, function_name, arguments=None):
    """Count this run; return the stop text if it is over the turn's limit."""
    if liveness is None:
        return None
    counts = liveness.tool_counts
    key = _call_key(function_name, arguments)
    same, total = counts.get(key, 0), sum(counts.values())
    if same >= MAX_SAME_TOOL_PER_TURN or total >= MAX_TOOLS_PER_TURN:
        logger.info(f"⏱ tool-loop stopp {function_name} {same + 1}")
        if same >= MAX_SAME_TOOL_PER_TURN:
            what = f"{function_name} med samma argument {same} gånger"
        else:
            what = f"verktyg {total} gånger"
        return (f"Stopp: du har redan anropat {what} i den här turen. "
                "Svara nu med det du vet, eller säg ärligt att du inte hittar det.")
    counts[key] = same + 1
    return None


def _is_error(result) -> bool:
    return isinstance(result, dict) and "error" in result


class ToolRegistrationMixin:
    """Registers every tool the same way, whichever engine is running.

    Mix in BEFORE the pipecat service class so this `register_function` wins:

        class Foo(ToolRegistrationMixin, SomeLLMService): ...
    """

    # main.py sets these on the service right after build_service(). The
    # defaults exist so a tool registered before that never raises.
    speaker_probe = None
    male_only_tools: set = set()
    turn_liveness = None
    # async (started, recent=None, tool=None) -> None: speaks a short phrase for `tool`
    # ("Jag söker på nätet.") on this connection's device. Set by main.py; None (tests, no device) means stay silent.
    early_ack = None
    _silence_ack_task = None

    def arm_silence_ack(self) -> None:
        """The model was just handed the user's turn; fill a long silence.

        Called where each engine is asked to answer: Gemini's activityEnd,
        OpenAI's speech_stopped (or bana 0's miss). Never on a bana 0 hit,
        which never asks the model. Live 2026-10-02 15:19:44 Gemini took
        3.7 s to its function call and 5.5 s to its first audio, and the
        tool ack never fired because the tool itself took 0.2 s. If the
        model has said nothing after EARLY_ACK_SILENCE_MS (1500, 0 = off),
        the same early ack plays: once per turn, never over the model's own
        audio or a new utterance, never in its history.
        """
        self.cancel_silence_ack()
        ms = ack_delay_ms(_silence_ack_ms(), self.turn_liveness)
        if self.early_ack is None or ms <= 0:
            return
        asked = time.monotonic()

        async def ack_if_silent():
            await asyncio.sleep(ms / 1000.0)
            liveness = self.turn_liveness
            if liveness is None or not liveness.claim_silence_ack(asked):
                return
            try:
                await self.early_ack(asked, 0.0)
            except Exception as e:
                logger.warning(f"⚠️ silence ack failed: {e!r}")

        self._silence_ack_task = asyncio.ensure_future(ack_if_silent())

    def cancel_silence_ack(self) -> None:
        if self._silence_ack_task is not None:
            self._silence_ack_task.cancel()
            self._silence_ack_task = None

    def register_function(self, function_name, handler, start_callback=None, *,
                          cancel_on_interruption: bool = True):  # type: ignore[override]
        """Force cancel_on_interruption=False for every tool registration.

        pipecat cancels in-flight function-call tasks on EVERY user-speech
        interruption. Both engines produce one per utterance, for different
        reasons: OpenAI's semantic_vad fires on each utterance fragment, so
        merely continuing your own sentence kills the tool call your previous
        fragment started; on Gemini the user transcript arrives AFTER the
        model has already called the tool, and the user aggregator turns that
        late transcript into an emulated "user started speaking" — an
        interruption caused by the very sentence that asked for the tool.

        By then the HTTP request to Home Assistant has usually already been
        SENT: the action executes, but its result never reaches the model,
        which then tells the user it failed (observed live: the lights turned
        ON while the assistant claimed they wouldn't). Or the request is cut
        off mid-flight and nothing happens at all, while the assistant still
        says it did. Our tools are all short-lived (HA service calls, one web
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
            # One line per call, every engine, every tool (MCP included):
            # `⏱ tool <name> <ms> ok|fel`, timed to the moment the result is
            # handed back -- that is what the person in the room waits for.
            started = time.monotonic()
            logged = False
            ack_speaking = False

            def log_timing(status: str) -> None:
                nonlocal logged
                if not logged:
                    logged = True
                    ms = round((time.monotonic() - started) * 1000)
                    logger.info(f"⏱ tool {function_name} {ms} {status}")

            async def ack_if_slow():
                # The owner, 2026-10-02: a smart agent says it has understood
                # but needs to check, instead of going quiet. Fast tools (HA:
                # 0.1-0.8 s) finish before this fires and stay silent.
                nonlocal ack_speaking
                await asyncio.sleep(ack_ms / 1000.0)
                liveness = self.turn_liveness
                if liveness is not None and not liveness.claim_ack(started):
                    return
                ack_speaking = True
                try:
                    await self.early_ack(started, tool=function_name)
                except Exception as e:
                    logger.warning(f"⚠️ early ack failed: {e!r}")

            ack_task = None
            ack_ms = ack_delay_ms(_early_ack_ms(), self.turn_liveness)
            if self.early_ack is not None and ack_ms > 0:
                ack_task = asyncio.ensure_future(ack_if_slow())

            def stop_waiting_ack() -> None:
                # Only a pending ack is dropped; one already speaking finishes
                # its sentence (the device queues the reply after it).
                if ack_task is not None and not ack_speaking:
                    ack_task.cancel()

            # Capped here, below every engine and every tool, MCP included.
            original_callback = params.result_callback
            max_chars = _result_max_chars()

            async def capped_callback(result, *args, **kwargs):
                stop_waiting_ack()
                log_timing("fel" if _is_error(result) else "ok")
                return await original_callback(cap_tool_result(result, max_chars), *args, **kwargs)

            params.result_callback = capped_callback
            if self.turn_liveness is not None:
                self.turn_liveness.tool_started()
            try:
                stop = loop_stop(self.turn_liveness, function_name, getattr(params, "arguments", None))
                if stop is not None:
                    await params.result_callback({"result": stop})
                    return
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
                return await handler(params)
            except BaseException:
                log_timing("fel")
                raise
            finally:
                stop_waiting_ack()
                log_timing("ok")
                if self.turn_liveness is not None:
                    self.turn_liveness.tool_finished()

        super().register_function(
            function_name, liveness_tracked, start_callback, cancel_on_interruption=False
        )
