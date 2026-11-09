"""Fire-and-forget tasks that cannot be garbage-collected mid-flight.

asyncio keeps only a weak reference to a task; one nobody holds can vanish
before it finishes. ``spawn`` keeps a reference until the task is done and
logs a failure instead of leaving it unretrieved.
"""
import asyncio
import logging
from typing import Coroutine, Optional, Set

logger = logging.getLogger(__name__)

_TASKS: Set[asyncio.Task] = set()


def _done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.debug(f"background task {task.get_name()} failed: {error!r}")


def spawn(coro: Coroutine, name: Optional[str] = None,
          into: Optional[Set[asyncio.Task]] = None) -> asyncio.Task:
    """Run ``coro`` in the background, held in ``into`` (default: a module
    set) until it finishes."""
    tasks = _TASKS if into is None else into
    task = asyncio.get_running_loop().create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_done)
    return task
