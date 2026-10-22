"""In-process pub/sub that feeds the SSE stream, plus structured logging."""

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger("gauntlet")


def log_event(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self._subs[run_id].discard(q)
        if not self._subs[run_id]:
            del self._subs[run_id]

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled client misses events; it can refetch GET /runs/{id}
