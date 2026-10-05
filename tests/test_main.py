"""Tests for the main orchestration entry point."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_watcher import main as main_module
from claude_watcher.config import Settings
from claude_watcher.fetcher import FetchResult


@pytest.mark.asyncio
async def test_seed_baseline_commits_without_digest(
    monkeypatch, tmp_path: Path
) -> None:
    """--seed fetches + commits but never summarizes or delivers."""
    settings = Settings(
        snapshots_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )

    fake_fetch = AsyncMock(
        return_value=FetchResult(
            fetched_pages=["docs__a.md", "api-docs/docs__en__b.md"],
            new_pages=["docs__a.md", "api-docs/docs__en__b.md"],
        )
    )
    fake_commit = MagicMock()
    fake_summarize = AsyncMock()
    fake_deliver = AsyncMock()

    monkeypatch.setattr(main_module, "fetch_all_docs", fake_fetch)
    monkeypatch.setattr(main_module, "commit_snapshot", fake_commit)
    monkeypatch.setattr(main_module, "summarize_diff", fake_summarize)
    monkeypatch.setattr(main_module, "deliver", fake_deliver)

    await main_module._seed_baseline(settings)

    fake_fetch.assert_awaited_once()
    fake_commit.assert_called_once()
    # Committed with the "seed" scope...
    assert "seed" in fake_commit.call_args.args
    # ...and crucially, no summary or delivery happened.
    fake_summarize.assert_not_called()
    fake_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_seed_baseline_no_pages_skips_commit(monkeypatch, tmp_path: Path) -> None:
    """When nothing is fetched, the baseline commit is skipped."""
    settings = Settings(
        snapshots_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )

    monkeypatch.setattr(
        main_module, "fetch_all_docs", AsyncMock(return_value=FetchResult())
    )
    fake_commit = MagicMock()
    monkeypatch.setattr(main_module, "commit_snapshot", fake_commit)

    await main_module._seed_baseline(settings)

    fake_commit.assert_not_called()
