"""Shared test fixtures."""

import pytest

from claude_watcher import llm


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
