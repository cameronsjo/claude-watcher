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


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-10-24T01:00:00", True),  # Fri 19:00 CST, Sat in UTC
        ("2026-10-26T01:00:00", False),  # Sun 19:00 CST, Mon in UTC
        ("2026-10-26T16:00:00", True),  # Mon 10:00 CST
        ("2026-10-26T15:59:00", False),  # Mon 09:59 CST
        ("2026-10-27T03:00:00", False),  # Mon 21:00 CST
    ],
)
def test_is_peak_hours_reads_the_cst_weekday(utc: str, expected: bool) -> None:
    from datetime import UTC, datetime

    now = datetime.fromisoformat(utc).replace(tzinfo=UTC)
    assert main_module._is_peak_hours(now) is expected


@pytest.mark.asyncio
async def test_pipeline_runs_do_not_overlap(monkeypatch, tmp_path: Path) -> None:
    """A changelog run waits for an in-flight docs run instead of interleaving."""
    import asyncio

    settings = Settings(
        snapshots_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    events: list[str] = []

    async def fake_fetch(_client, _settings):
        events.append("fetch")
        await asyncio.sleep(0.05)
        events.append("fetched")
        return FetchResult()

    monkeypatch.setattr(main_module, "fetch_all_docs", fake_fetch)
    monkeypatch.setattr(main_module, "fetch_changelog", fake_fetch)

    await asyncio.gather(
        main_module._run_pipeline("full", settings),
        main_module._run_pipeline("changelog", settings),
    )

    assert events == ["fetch", "fetched", "fetch", "fetched"]
