"""Provider-neutral session controls for the device handler.

``websocket_handler.build_pipeline`` reacts to device messages (stop, wake,
flush, connect, speaker verdicts) by poking the model session. Those pokes
used to be OpenAI *Realtime* client events written inline. With a second
runtime (GPT-Live, a different protocol) the handler must not know either
wire format, so it asks for a ``SessionControls`` view of the service:

* a service that implements the methods itself (the GPT-Live service) is
  used directly;
* anything else is wrapped in ``RealtimeSessionControls``, which contains the
  exact Realtime client events the handler sent before, unchanged.

This keeps the device transport, phases, timers and safety gates free of
``if live`` branches, and keeps the Realtime behaviour identical.
"""
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

REQUIRED = ("discard_pending_input", "cancel_active_response", "inject_context",
            "on_assistant_response_started", "response_active")


class RealtimeSessionControls:
    """OpenAI Realtime client events for the device handler, verbatim."""

    def __init__(self, service: Any):
        self._service = service

    @property
    def service(self) -> Any:
        return self._service

    @property
    def response_active(self) -> bool:
        return getattr(self._service, "_current_assistant_response", None) is not None

    async def discard_pending_input(self, reason: str = "") -> bool:
        """input_audio_buffer.clear — drop not-yet-committed user audio."""
        from pipecat.services.openai.realtime import events as openai_rt_events
        await self._service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
        return True

    async def cancel_active_response(self, reason: str = "", force: bool = False) -> bool:
        """response.cancel. With force=False only while a response is active.

        Returns True when a cancel was sent.
        """
        from pipecat.services.openai.realtime import events as openai_rt_events
        if not force and not self.response_active:
            return False
        await self._service.send_client_event(openai_rt_events.ResponseCancelEvent())
        return True

    async def inject_context(self, text: str) -> None:
        """A system conversation item (speaker verdicts)."""
        from pipecat.services.openai.realtime import events as openai_rt_events
        await self._service.send_client_event(
            openai_rt_events.ConversationItemCreateEvent(
                item=openai_rt_events.ConversationItem(
                    type="message",
                    role="system",
                    content=[openai_rt_events.ItemContent(type="input_text", text=text)],
                )
            )
        )

    def on_assistant_response_started(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run callback whenever a NEW assistant item starts.

        Pipecat fires on_conversation_item_created for every
        conversation.item.added; only ASSISTANT items count.
        """
        @self._service.event_handler("on_conversation_item_created")
        async def _on_item(service, item_id, item):
            if getattr(item, "role", None) != "assistant":
                return
            await callback()


def controls_for(service: Any):
    """The SessionControls view of a model service."""
    if all(hasattr(service, name) for name in REQUIRED):
        return service
    return RealtimeSessionControls(service)
