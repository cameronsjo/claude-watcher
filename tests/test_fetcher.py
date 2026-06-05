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
        api_docs_base_url="",  # single-source: API docs disabled here
        changelog_url="https://raw.test.com/CHANGELOG.md",
        _env_file=None,  # type: ignore[call-arg]
    )

    httpx_mock.add_response(
        url="https://docs.test.com/llms.txt",
        text=(
            "# Test Docs\n\n## Docs\n\n"
            "- [Hooks](https://docs.test.com/en/hooks): Hook reference.\n"
            "- [Plugins](https://docs.test.com/en/plugins): Plugin guide.\n"
        ),
    )
    httpx_mock.add_response(
        url="https://docs.test.com/en/hooks",
        text="# Hooks\nHook content here.",
    )
    httpx_mock.add_response(
        url="https://docs.test.com/en/plugins",
        text="# Plugins\nPlugin content here.",
    )
    import httpx

    async with httpx.AsyncClient() as client:
        result = await fetch_all_docs(client, settings)

    assert len(result.fetched_pages) == 2
    assert len(result.failed_pages) == 0


@pytest.mark.asyncio
async def test_fetch_all_docs_multi_source(httpx_mock, tmp_path: Path) -> None:
    """Both sources are fetched; API pages land namespaced under api-docs/.

    The two sources use a page path that maps to the *same* flat filename
    (``docs__shared.md``) to prove the subdirectory prevents a collision.
    """
    from claude_watcher.fetcher import API_DOCS_SUBDIR

    settings = Settings(
        snapshots_dir=tmp_path,
        docs_base_url="https://code.test.com",
        api_docs_base_url="https://platform.test.com",
        changelog_url="https://raw.test.com/CHANGELOG.md",
        _env_file=None,  # type: ignore[call-arg]
    )

    # Claude Code docs index + page → flat snapshots/docs__shared.md
    httpx_mock.add_response(
        url="https://code.test.com/llms.txt",
        text="- [Shared](https://code.test.com/docs/shared): Claude Code page.\n",
    )
    httpx_mock.add_response(
        url="https://code.test.com/docs/shared",
        text="# Claude Code shared",
    )
    # API docs index + page → snapshots/api-docs/docs__shared.md
    httpx_mock.add_response(
        url="https://platform.test.com/llms.txt",
        text="- [Shared](https://platform.test.com/docs/shared): API page.\n",
    )
    httpx_mock.add_response(
        url="https://platform.test.com/docs/shared",
        text="# Anthropic API shared",
    )
    import httpx

    async with httpx.AsyncClient() as client:
        result = await fetch_all_docs(client, settings)

    assert len(result.fetched_pages) == 2
    assert len(result.failed_pages) == 0

    # Both base URLs were hit (llms.txt of each)
    requested = {str(r.url) for r in httpx_mock.get_requests()}
    assert "https://code.test.com/llms.txt" in requested
    assert "https://platform.test.com/llms.txt" in requested

    # Namespaced layout: same filename, different directories, no collision
    flat = tmp_path / "docs__shared.md"
    namespaced = tmp_path / API_DOCS_SUBDIR / "docs__shared.md"
    assert flat.exists()
    assert namespaced.exists()
    assert flat.read_text() == "# Claude Code shared"
    assert namespaced.read_text() == "# Anthropic API shared"

    # Report names keep the sources distinct
    assert "docs__shared.md" in result.fetched_pages
    assert f"{API_DOCS_SUBDIR}/docs__shared.md" in result.fetched_pages
