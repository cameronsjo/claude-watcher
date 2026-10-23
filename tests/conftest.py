"""Shared test fixtures."""

import asyncio

import pytest

from claude_watcher import llm
from claude_watcher import main as main_module


@pytest.fixture(autouse=True)
def _clear_llm_client_cache():
    """Drop cached LLM clients between tests.

    `llm.complete` caches one client per endpoint config so a 130-call run does
    not build 130 connection pools. That cache would otherwise make the
    constructor-kwarg assertions order-dependent.
    """
    llm.reset_client_cache()
    yield
    llm.reset_client_cache()


@pytest.fixture(autouse=True)
def _fresh_pipeline_lock(monkeypatch):
    """Give each test its own pipeline lock.

    An asyncio.Lock binds to the first event loop that waits on it, and
    pytest-asyncio runs each test on a new loop, so a shared lock would fail
    the second test that contends on it.
    """
    monkeypatch.setattr(main_module, "_pipeline_lock", asyncio.Lock())
