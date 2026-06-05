"""Async page fetching from llms.txt and raw GitHub."""

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog

from claude_watcher.config import Settings

logger = structlog.get_logger()

MAX_CONCURRENT_REQUESTS = 10

# Anthropic API docs are namespaced into their own snapshot subdirectory so they
# stay distinct from the flat Claude Code docs (and never collide on filename).
API_DOCS_SUBDIR = "api-docs"


@dataclass
class FetchResult:
    """Result of a fetch cycle."""

    fetched_pages: list[str] = field(default_factory=list)
    new_pages: list[str] = field(default_factory=list)
    failed_pages: list[str] = field(default_factory=list)


async def fetch_page_list(client: httpx.AsyncClient, base_url: str) -> list[str]:
    """Fetch list of documentation page URLs from a source's llms.txt index."""
    llms_url = f"{base_url}/llms.txt"
    response = await client.get(llms_url)
    response.raise_for_status()

    # llms.txt uses markdown link format: - [title](url): description
    # Extract URLs from markdown links, falling back to bare URLs
    link_pattern = re.compile(r"\(https?://[^)]+\)")
    urls: list[str] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = link_pattern.search(line)
        if match:
            urls.append(match.group(0)[1:-1])  # Strip parens
        elif line.startswith("http"):
            urls.append(line)

    logger.info("Fetched page list from llms.txt.", page_count=len(urls))
    return urls


def _url_to_filename(url: str) -> str:
    """Convert a documentation URL to a local filename.

    Strips the base URL prefix and replaces slashes with double underscores
    to create a flat file structure in snapshots/.
    """
    # Remove protocol and domain
    path = url.split("//", 1)[-1]
    # Remove domain
    path = path.split("/", 1)[-1] if "/" in path else path
    # Replace slashes with double underscores, strip leading/trailing
    path = path.strip("/").replace("/", "__")
    if not path.endswith(".md"):
        path += ".md"
    return path


async def _fetch_single_page(
    client: httpx.AsyncClient,
    url: str,
    target_dir: Path,
    semaphore: asyncio.Semaphore,
    report_prefix: str = "",
) -> tuple[str, bool, bool]:
    """Fetch a single page and write it under target_dir.

    The file is written to ``target_dir / _url_to_filename(url)``; the returned
    name is prefixed with ``report_prefix`` (e.g. ``api-docs/``) so callers can
    tell sources apart in logs and results. Returns (report_name, is_new, success).
    """
    filename = _url_to_filename(url)
    filepath = target_dir / filename
    report_name = f"{report_prefix}{filename}"

    is_new = not filepath.exists()

    async with semaphore:
        try:
            response = await client.get(url)
            response.raise_for_status()
            filepath.write_text(response.text, encoding="utf-8")
            logger.debug("Fetched page.", url=url, filename=report_name, is_new=is_new)
            return report_name, is_new, True
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch page.", url=url, error=str(exc))
            return report_name, is_new, False


async def _fetch_source(
    client: httpx.AsyncClient,
    base_url: str,
    target_dir: Path,
    semaphore: asyncio.Semaphore,
    report_prefix: str = "",
) -> list[tuple[str, bool, bool]]:
    """Fetch every page of one documentation source into target_dir.

    Discovers the page list from ``{base_url}/llms.txt`` then fetches all pages
    concurrently (bounded by the shared semaphore).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    urls = await fetch_page_list(client, base_url)
    tasks = [
        _fetch_single_page(client, url, target_dir, semaphore, report_prefix)
        for url in urls
    ]
    return list(await asyncio.gather(*tasks))


async def fetch_changelog(client: httpx.AsyncClient, settings: Settings) -> FetchResult:
    """Fetch only CHANGELOG.md from raw GitHub."""
    snapshots_dir = settings.snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    filepath = snapshots_dir / "CHANGELOG.md"
    is_new = not filepath.exists()

    try:
        response = await client.get(settings.changelog_url)
        response.raise_for_status()
        filepath.write_text(response.text, encoding="utf-8")
        logger.info("Fetched CHANGELOG.md.", is_new=is_new)
        return FetchResult(
            fetched_pages=["CHANGELOG.md"],
            new_pages=["CHANGELOG.md"] if is_new else [],
        )
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch CHANGELOG.md.", error=str(exc))
        return FetchResult(failed_pages=["CHANGELOG.md"])


async def fetch_all_docs(client: httpx.AsyncClient, settings: Settings) -> FetchResult:
    """Fetch all documentation pages from every enabled source.

    Claude Code docs write flat into ``snapshots/``; the Anthropic API docs (when
    enabled) write into ``snapshots/api-docs/`` so the two sets stay distinct.
    CHANGELOG.md is handled by its own job (check_changelog) — not duplicated here.
    """
    snapshots_dir = settings.snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    # Claude Code docs → flat in snapshots/ (unchanged layout)
    all_results = await _fetch_source(
        client, settings.docs_base_url, snapshots_dir, semaphore
    )

    # Anthropic API docs → snapshots/api-docs/ (namespaced)
    if settings.api_docs_enabled:
        api_dir = snapshots_dir / API_DOCS_SUBDIR
        api_results = await _fetch_source(
            client,
            settings.api_docs_base_url,
            api_dir,
            semaphore,
            report_prefix=f"{API_DOCS_SUBDIR}/",
        )
        all_results.extend(api_results)

    result = FetchResult()
    for filename, is_new, success in all_results:
        if success:
            result.fetched_pages.append(filename)
            if is_new:
                result.new_pages.append(filename)
        else:
            result.failed_pages.append(filename)

    logger.info(
        "Fetch cycle complete.",
        fetched=len(result.fetched_pages),
        new=len(result.new_pages),
        failed=len(result.failed_pages),
    )
    return result
