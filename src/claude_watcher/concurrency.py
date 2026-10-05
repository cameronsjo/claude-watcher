"""Shared concurrency utilities for throttled fan-out work."""

import asyncio
from collections.abc import Awaitable
from typing import Any


async def bounded_gather(
    limit: int,
    *coros: Awaitable[Any],
    return_exceptions: bool = True,
) -> list[Any]:
    """Gather coroutines with at most `limit` running concurrently.

    A coroutine does not start executing until it is awaited, so wrapping
    pre-built coroutines in a semaphore-guarded runner bounds concurrency at the
    `await` boundary. Results preserve submission order.

    With ``return_exceptions=True`` (the default) a failing coroutine yields its
    exception in the result list rather than aborting the whole batch — callers
    decide per-item how to degrade.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro: Awaitable[Any]) -> Any:
        async with sem:
            return await coro

    return await asyncio.gather(
        *(_run(c) for c in coros),
        return_exceptions=return_exceptions,
    )
