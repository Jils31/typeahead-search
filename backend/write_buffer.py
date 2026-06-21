"""Write-back batch buffer: aggregates search counts in memory and flushes to
Postgres on size (BATCH_SIZE_N) or interval (FLUSH_INTERVAL_T). A crash loses at
most one un-flushed window — acceptable for approximate counts (DESIGN.md §8)."""
import asyncio
from typing import Awaitable, Callable, Dict

from . import config, metrics

FlushHandler = Callable[[Dict[str, int]], Awaitable[None]]


class WriteBuffer:
    def __init__(self, flush_handler: FlushHandler):
        self._buf: Dict[str, int] = {}
        self._handler = flush_handler
        self._size_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    def add(self, query: str) -> None:
        query = query.lower().strip()
        if not query:
            return
        metrics.searches_received += 1
        self._buf[query] = self._buf.get(query, 0) + 1
        if len(self._buf) >= config.BATCH_SIZE_N:
            self._size_event.set()  # wake the loop for a size-triggered flush

    async def _flush_once(self) -> int:
        if not self._buf:
            return 0
        window = self._buf            # swap out the current window...
        self._buf = {}                # ...and start a fresh one (no await between)
        self._size_event.clear()
        await self._handler(window)
        return len(window)

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._size_event.wait(), timeout=config.FLUSH_INTERVAL_T)
            except asyncio.TimeoutError:
                pass  # time-based flush
            await self._flush_once()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        self._size_event.set()
        if self._task:
            await self._task
        await self._flush_once()  # final drain so nothing is left buffered

    def pending(self) -> int:
        return len(self._buf)
