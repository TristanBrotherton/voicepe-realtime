"""Keep the current request's mic audio so a reconnect does not lose it.

When the OpenAI socket has to be replaced mid-request (a half-open socket
found at wake time, the 60-minute cap, a network drop) the old conversation's
input buffer is gone. This processor keeps a rolling window of the inbound
audio for the request in progress and, while a reconnect runs, holds new mic
frames back instead of letting pipecat drop them on a socket that is not
there. After the reconnect it either replays the whole unanswered request
into the fresh session (the user's question still gets answered) or simply
releases the held frames in order.

Only ``InputAudioRawFrame`` is buffered or held; every other frame passes
straight through so pipeline control flow is never delayed.
"""
from __future__ import annotations

from collections import deque
from typing import Deque

from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class InputReplayBuffer(FrameProcessor):
    def __init__(self, max_seconds: float = 15.0, sample_rate: int = 24000, **kwargs):
        super().__init__(**kwargs)
        self._max_bytes = int(max_seconds * sample_rate * 2)
        self._frames: Deque[InputAudioRawFrame] = deque()
        self._bytes = 0
        self._holding = False
        self._held: list = []
        self.replayed_frames = 0

    @property
    def holding(self) -> bool:
        return self._holding

    @property
    def buffered_ms(self) -> int:
        return int(self._bytes / 48)  # 24 kHz mono PCM16 = 48 bytes/ms

    def clear(self) -> None:
        """Drop the window: a new request began, or the current one was answered."""
        self._frames.clear()
        self._bytes = 0

    def begin_hold(self) -> None:
        """Stop forwarding mic audio until release() (a reconnect is running)."""
        self._holding = True

    async def release(self, replay: bool) -> int:
        """Resume forwarding; optionally replay the buffered request first.

        Returns the number of frames pushed.
        """
        if replay:
            queue = list(self._frames)  # includes everything held since the hold began
            self._held = []
        else:
            queue, self._held = self._held, []
        pushed = 0
        while queue:
            frame = queue.pop(0)
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
            pushed += 1
            if not queue and self._held:
                # Frames that arrived while we were pushing keep their order.
                queue, self._held = self._held, []
        self._holding = False
        if replay:
            self.replayed_frames += pushed
        return pushed

    def _record(self, frame: InputAudioRawFrame) -> None:
        self._frames.append(frame)
        self._bytes += len(frame.audio)
        while self._bytes > self._max_bytes and self._frames:
            self._bytes -= len(self._frames.popleft().audio)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self._record(frame)
            if self._holding:
                self._held.append(frame)
                return
        await self.push_frame(frame, direction)
