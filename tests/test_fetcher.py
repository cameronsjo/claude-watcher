"""Tests for fetcher module."""

from pathlib import Path

import pytest

from claude_watcher.config import Settings
from claude_watcher.fetcher import _url_to_filename, fetch_all_docs, fetch_changelog


def test_url_to_filename_simple() -> None:
    result = _url_to_filename("https://code.claude.com/docs/en/hooks")
    assert result == "docs__en__hooks.md"


def test_url_to_filename_with_md_extension() -> None:
    result = _url_to_filename("https://example.com/page.md")
    assert result == "page.md"


def test_url_to_filename_trailing_slash() -> None:
    result = _url_to_filename("https://example.com/docs/page/")
    assert result == "docs__page.md"


@pytest.mark.asyncio
async def test_fetch_changelog(httpx_mock, tmp_path: Path) -> None:
    """Fetching CHANGELOG.md writes to snapshots dir."""
    settings = Settings(
        snapshots_dir=tmp_path,
        changelog_url="https://raw.test.com/CHANGELOG.md",
        _env_file=None,  # type: ignore[call-arg]
    )

    httpx_mock.add_response(
        url="https://raw.test.com/CHANGELOG.md",
        text="# Changelog\n## v1.0.0\n- Fixed stuff",
    )

    import httpx

    async with httpx.AsyncClient() as client:
        result = await fetch_changelog(client, settings)

    assert "CHANGELOG.md" in result.fetched_pages
    assert (tmp_path / "CHANGELOG.md").exists()
    assert "Fixed stuff" in (tmp_path / "CHANGELOG.md").read_text()


@pytest.mark.asyncio
async def test_fetch_all_docs(httpx_mock, tmp_path: Path) -> None:
    """Fetching all docs discovers pages from llms.txt and fetches them."""
    settings = Settings(
        snapshots_dir=tmp_path,
        docs_base_url="https://docs.test.com",
        changelog_url="https://raw.test.com/CHANGELOG.md",
        _env_file=None,  # type: ignore[call-arg]
    )

    httpx_mock.add_response(
        url="https://docs.test.com/llms.txt",
        text="https://docs.test.com/en/hooks\nhttps://docs.test.com/en/plugins\n",
    )
    httpx_mock.add_response(
        url="https://docs.test.com/en/hooks",
        text="# Hooks\nHook content here.",
    )
    httpx_mock.add_response(
        url="https://docs.test.com/en/plugins",
        text="# Plugins\nPlugin content here.",
    )
    httpx_mock.add_response(
        url="https://raw.test.com/CHANGELOG.md",
        text="# Changelog",
    )

    import httpx

    async with httpx.AsyncClient() as client:
        result = await fetch_all_docs(client, settings)

    assert len(result.fetched_pages) == 3
    assert len(result.failed_pages) == 0
