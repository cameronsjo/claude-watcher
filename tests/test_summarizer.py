"""Tests for summarizer module."""

import pytest

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.summarizer import _fallback_summary, summarize_diff


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
