"""Tests for summarizer module."""

import asyncio
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.summarizer import _fallback_summary, summarize_diff

# ---------------------------------------------------------------------------
# Helpers for the fan-out hardening tests (issue #4)
# ---------------------------------------------------------------------------


def _raw_diff(*filenames: str, body: str = "+added line") -> str:
    """Build a unified diff with one section per filename."""
    return "".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n{body}\n" for f in filenames
    )


def _reduce_message(text: str = "DIGEST") -> MagicMock:
    """A synthesis (reduce) response with the usage fields the code logs."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    return msg


def _map_message(text: str = "- a change") -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def _is_map(model: str) -> bool:
    """Map (per-file) calls go to Haiku; reduce (synthesis) to Sonnet."""
    return "haiku" in model


def test_fallback_summary() -> None:
    """Fallback summary includes all change categories with counts."""
    diff = DiffResult(
        new_pages=["new-page.md"],
        removed_pages=["old-page.md"],
        modified_pages=["changed-page.md"],
        raw_diff="diff content",
    )
    summary = _fallback_summary(diff)

    assert "new-page.md" in summary
    assert "old-page.md" in summary
    assert "changed-page.md" in summary
    assert "3 page(s) changed" in summary
    assert "summarizer unavailable" in summary
    assert "**New Pages**" in summary
    assert "**Removed Pages**" in summary
    assert "**Modified Pages**" in summary


def test_fallback_summary_custom_reason() -> None:
    """Fallback summary displays the provided reason."""
    diff = DiffResult(
        modified_pages=["page.md"],
        raw_diff="diff",
    )
    summary = _fallback_summary(diff, reason="API error")

    assert "API error" in summary
    assert "1 page(s) changed" in summary


@pytest.mark.asyncio
async def test_summarize_diff_without_api_key() -> None:
    """Without API key, falls back to plain-text summary."""
    settings = Settings(
        anthropic_api_key="",
        _env_file=None,  # type: ignore[call-arg]
    )
    diff = DiffResult(
        new_pages=["page.md"],
        modified_pages=[],
        removed_pages=[],
        raw_diff="some diff",
    )
    summary = await summarize_diff(diff, settings)
    assert "page.md" in summary


# ---------------------------------------------------------------------------
# Per-file isolation: one bad file becomes a stub; the digest is still produced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_file_does_not_abort_digest() -> None:
    """A 429 on a single file yields a stub, not a whole-digest plain fallback."""
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        _env_file=None,  # type: ignore[call-arg]
    )
    diff = DiffResult(
        modified_pages=["good.md", "bad.md"],
        raw_diff=_raw_diff("good.md", "bad.md"),
    )

    reduce_inputs: list[str] = []

    async def create(**kwargs):
        content = kwargs["messages"][0]["content"]
        if _is_map(kwargs["model"]):
            if "bad.md" in content:
                raise anthropic.APIStatusError(
                    "rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                )
            return _map_message()
        reduce_inputs.append(content)
        return _reduce_message("SYNTHESIZED DIGEST")

    client = MagicMock()
    client.messages.create = create

    with patch(
        "claude_watcher.summarizer.anthropic.AsyncAnthropic", return_value=client
    ):
        result = await summarize_diff(diff, settings)

    # Synthesis ran and produced the digest — not the plain-text fallback.
    assert "SYNTHESIZED DIGEST" in result
    assert "page(s) changed" not in result
    # The failed file was synthesized as a stub, not dropped.
    assert reduce_inputs
    assert "(summary unavailable — bad.md changed)" in reduce_inputs[0]


# ---------------------------------------------------------------------------
# Concurrency cap: peak in-flight map calls never exceed the configured bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_respects_concurrency_cap() -> None:
    """No more than summarizer_max_concurrency map calls run at once."""
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        summarizer_max_concurrency=2,
        _env_file=None,  # type: ignore[call-arg]
    )
    files = [f"doc{i}.md" for i in range(6)]
    diff = DiffResult(modified_pages=files, raw_diff=_raw_diff(*files))

    state = {"in_flight": 0, "peak": 0}

    async def create(**kwargs):
        if _is_map(kwargs["model"]):
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            for _ in range(3):
                await asyncio.sleep(0)
            state["in_flight"] -= 1
            return _map_message()
        return _reduce_message()

    client = MagicMock()
    client.messages.create = create

    with patch(
        "claude_watcher.summarizer.anthropic.AsyncAnthropic", return_value=client
    ):
        result = await summarize_diff(diff, settings)

    assert state["peak"] <= 2
    assert "DIGEST" in result


# ---------------------------------------------------------------------------
# Truncation: oversized single-file input is clamped to the budget + marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_file_is_truncated() -> None:
    """A single oversized file is truncated to summarizer_max_input_chars."""
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        summarizer_max_input_chars=500,
        _env_file=None,  # type: ignore[call-arg]
    )
    diff = DiffResult(
        modified_pages=["big.md"],
        raw_diff=_raw_diff("big.md", body="+" + "x" * 5000),
    )

    captured: dict[str, str] = {}

    async def create(**kwargs):
        content = kwargs["messages"][0]["content"]
        if _is_map(kwargs["model"]):
            captured["content"] = content
            return _map_message()
        return _reduce_message()

    client = MagicMock()
    client.messages.create = create

    with patch(
        "claude_watcher.summarizer.anthropic.AsyncAnthropic", return_value=client
    ):
        await summarize_diff(diff, settings)

    assert len(captured["content"]) <= 500
    assert "[... diff truncated ...]" in captured["content"]


# ---------------------------------------------------------------------------
# max_retries wiring: the SDK client is built with the configured retry budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_built_with_max_retries() -> None:
    """AsyncAnthropic is constructed with max_retries from settings."""
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        summarizer_max_retries=5,
        _env_file=None,  # type: ignore[call-arg]
    )
    diff = DiffResult(modified_pages=["doc.md"], raw_diff=_raw_diff("doc.md"))

    async def create(**kwargs):
        if _is_map(kwargs["model"]):
            return _map_message()
        return _reduce_message()

    client = MagicMock()
    client.messages.create = create

    with patch(
        "claude_watcher.summarizer.anthropic.AsyncAnthropic", return_value=client
    ) as mock_cls:
        await summarize_diff(diff, settings)

    mock_cls.assert_called_once_with(api_key="sk-ant-test", max_retries=5)


# ---------------------------------------------------------------------------
# Per-run cap: only the first N files fire; the rest surface as deferred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_run_cap_defers_excess_files() -> None:
    """With summarizer_max_files=N, exactly N map calls fire; the rest are deferred."""
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        summarizer_max_files=2,
        _env_file=None,  # type: ignore[call-arg]
    )
    files = ["a.md", "b.md", "c.md"]
    diff = DiffResult(modified_pages=files, raw_diff=_raw_diff(*files))

    state = {"map_calls": 0}

    async def create(**kwargs):
        if _is_map(kwargs["model"]):
            state["map_calls"] += 1
            return _map_message()
        return _reduce_message()

    client = MagicMock()
    client.messages.create = create

    with patch(
        "claude_watcher.summarizer.anthropic.AsyncAnthropic", return_value=client
    ):
        result = await summarize_diff(diff, settings)

    assert state["map_calls"] == 2
    assert "Deferred" in result
    assert "c.md" in result
