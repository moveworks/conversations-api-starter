"""Server-sent event helpers shared by the turn runner and the server."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Iterator, TypeVar

T = TypeVar("T")


def sse_frame(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def to_async(sync_iter: Iterator[T]) -> AsyncIterator[T]:
    """Drain a blocking iterator on a worker thread without blocking the loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _producer() -> None:
        try:
            for item in sync_iter:
                asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(sentinel), loop).result()

    task = asyncio.create_task(asyncio.to_thread(_producer))
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item
    finally:
        await task
