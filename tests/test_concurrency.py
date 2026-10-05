"""Tests for the bounded_gather concurrency helper."""

import asyncio

import pytest

from claude_watcher.concurrency import bounded_gather


@pytest.mark.asyncio
async def test_bounded_gather_caps_concurrency() -> None:
    """No more than `limit` coroutines run at once; peak never exceeds the bound."""
    in_flight = 0
    peak = 0

    async def work(value: int) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield repeatedly so the scheduler can interleave waiting coroutines.
        for _ in range(5):
            await asyncio.sleep(0)
        in_flight -= 1
        return value

    results = await bounded_gather(2, *(work(i) for i in range(10)))

    assert results == list(range(10))
    assert peak <= 2


@pytest.mark.asyncio
async def test_bounded_gather_returns_exceptions_by_default() -> None:
    """With return_exceptions=True (default), a failing coroutine becomes a result."""

    async def ok() -> str:
        return "ok"

    async def boom() -> str:
        raise ValueError("kaboom")

    results = await bounded_gather(2, ok(), boom(), ok())

    assert results[0] == "ok"
    assert isinstance(results[1], ValueError)
    assert results[2] == "ok"


@pytest.mark.asyncio
async def test_bounded_gather_preserves_order() -> None:
    """Results come back in submission order regardless of completion order."""

    async def work(value: int, delay_steps: int) -> int:
        for _ in range(delay_steps):
            await asyncio.sleep(0)
        return value

    # Earlier items finish later — order must still follow submission.
    results = await bounded_gather(4, work(0, 5), work(1, 3), work(2, 1), work(3, 0))
    assert results == [0, 1, 2, 3]
