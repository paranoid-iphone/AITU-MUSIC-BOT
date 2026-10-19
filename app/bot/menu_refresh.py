import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class MenuRefresher:
    """Runs schedule-menu refreshes in the background, one at a time.

    A change that arrives while a pass is running is not lost and does not start a second pass: the dirty flag stays
    set and a single follow-up pass starts once the current one finishes.
    """

    def __init__(self, refresh: Callable[[], Awaitable[None]]) -> None:
        self._refresh = refresh
        self._dirty = False
        self._task: asyncio.Task | None = None

    def mark_dirty(self) -> None:
        self._dirty = True

    def poll(self) -> None:
        """Start a pass if something changed and none is running. Call it whenever the event queue is drained."""
        if self._dirty and (self._task is None or self._task.done()):
            self._dirty = False
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Schedule menu refresh failed")

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
