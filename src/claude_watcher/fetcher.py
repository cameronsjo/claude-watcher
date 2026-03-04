"""Async page fetching from llms.txt and raw GitHub."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog

from claude_watcher.config import Settings

logger = structlog.get_logger()

MAX_CONCURRENT_REQUESTS = 10


@dataclass
class FetchResult:
    """Result of a fetch cycle."""

    fetched_pages: list[str] = field(default_factory=list)
    new_pages: list[str] = field(default_factory=list)
    failed_pages: list[str] = field(default_factory=list)


async def fetch_page_list(client: httpx.AsyncClient, settings: Settings) -> list[str]:
    """Fetch list of documentation page URLs from llms.txt."""
    llms_url = f"{settings.docs_base_url}/llms.txt"
    response = await client.get(llms_url)
    response.raise_for_status()

    urls: list[str] = []
    for line in response.text.splitlines():
        line = line.strip()
        if line and line.startswith("http"):
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
    snapshots_dir: Path,
    semaphore: asyncio.Semaphore,
) -> tuple[str, bool, bool]:
    """Fetch a single page and write to snapshots.

    Returns (filename, is_new, success).
    """
    filename = _url_to_filename(url)
    filepath = snapshots_dir / filename

    is_new = not filepath.exists()

    async with semaphore:
        try:
            response = await client.get(url)
            response.raise_for_status()
            filepath.write_text(response.text, encoding="utf-8")
            logger.debug("Fetched page.", url=url, filename=filename, is_new=is_new)
            return filename, is_new, True
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch page.", url=url, error=str(exc))
            return filename, is_new, False


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
    """Fetch all documentation pages from llms.txt plus CHANGELOG.md."""
    snapshots_dir = settings.snapshots_dir
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Fetch page list
    urls = await fetch_page_list(client, settings)

    # Add changelog URL
    urls.append(settings.changelog_url)

    # Fetch all pages concurrently with rate limiting
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    tasks = [_fetch_single_page(client, url, snapshots_dir, semaphore) for url in urls]
    results = await asyncio.gather(*tasks)

    result = FetchResult()
    for filename, is_new, success in results:
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
