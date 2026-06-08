"""Ecosystem drift detection: compare upstream docs against local ecosystem files."""

import asyncio
from pathlib import Path

import anthropic
import httpx
import structlog
import yaml

from claude_watcher.concurrency import bounded_gather
from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult

logger = structlog.get_logger()

# MAP prompt: per-pair analysis — does the ecosystem file conflict with upstream?
_MAP_PROMPT = """\
You are a drift detector for a Claude Code plugin ecosystem.
You will receive an upstream Claude Code documentation page and a local ecosystem file
(a skill, guide, or reference).
Identify specific items where the ecosystem file now CONTRADICTS or OMITS something the
upstream page states.
Focus on: API changes, renamed flags/settings/hook types, removed features, new required
fields, changed behavior.
Output a concise bullet list. If no drift exists, output exactly: NO DRIFT"""

# REDUCE prompt: synthesize map results into a prioritized digest
_REDUCE_PROMPT = """\
You are synthesizing drift findings for a Claude Code plugin ecosystem maintainer.
You will receive per-pair drift reports (upstream doc + ecosystem file).
Produce a single prioritized digest. Label each item:
  WRONG — ecosystem file states something that is now incorrect
  OUTDATED — ecosystem file is missing new upstream content that matters
Skip NO DRIFT pairs. If ALL pairs report NO DRIFT, return exactly: NO DRIFT
Use Discord markdown. Keep the total response under 2500 characters."""


def _load_mappings(mappings_file: Path) -> dict[str, list[str]]:
    """Load the YAML mapping of upstream page -> ecosystem file URLs."""
    try:
        with mappings_file.open() as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        logger.warning(
            "Drift check: cannot read mappings file.",
            path=str(mappings_file),
            error=str(exc),
        )
        return {}
    except yaml.YAMLError as exc:
        logger.warning(
            "Drift check: invalid YAML in mappings file.",
            path=str(mappings_file),
            error=str(exc),
        )
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only string-list values: non-string URLs would raise an unhandled
    # TypeError when fetched. The mapping file is user-maintained.
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, list) and all(isinstance(url, str) for url in v)
    }


async def _fetch_ecosystem_file(client: httpx.AsyncClient, url: str) -> str | None:
    """Fetch a single raw ecosystem file. Returns content or None on error."""
    try:
        response = await client.get(url, timeout=20.0)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch ecosystem file.", url=url, error=str(exc))
        return None


async def _check_pair(
    upstream_page: str,
    upstream_content: str,
    ecosystem_url: str,
    ecosystem_content: str,
    client: anthropic.AsyncAnthropic,
    map_model: str,
) -> tuple[str, str, str]:
    """MAP step: check a single (upstream-page, ecosystem-file) pair for drift.

    Returns (upstream_page, ecosystem_url, drift_findings).
    """
    user_message = (
        f"UPSTREAM PAGE: {upstream_page}\n\n"
        f"```\n{upstream_content[:8000]}\n```\n\n"
        f"ECOSYSTEM FILE: {ecosystem_url}\n\n"
        f"```\n{ecosystem_content[:8000]}\n```"
    )
    response = await client.messages.create(
        model=map_model,
        max_tokens=512,
        system=_MAP_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    findings = response.content[0].text.strip()
    logger.debug(
        "Drift pair checked.",
        upstream_page=upstream_page,
        ecosystem_url=ecosystem_url,
        has_drift=findings != "NO DRIFT",
    )
    return upstream_page, ecosystem_url, findings


async def check_drift(diff: DiffResult, settings: Settings) -> str | None:
    """Check ecosystem files for drift against changed upstream doc pages.

    Returns a digest string if drift is found, None if no drift or check skipped.
    Degrades gracefully on API or network errors — never crashes the pipeline.
    """
    mappings = _load_mappings(settings.drift_mappings_file)
    if not mappings:
        logger.info("Drift check: no mappings loaded, skipping.")
        return None

    # Intersect mapping keys with pages that actually changed
    changed_pages = set(diff.modified_pages) | set(diff.new_pages)
    matched_pages = [p for p in changed_pages if p in mappings]

    if not matched_pages:
        logger.info(
            "Drift check: no changed pages match mapping keys, skipping.",
            changed_count=len(changed_pages),
        )
        return None

    logger.info(
        "Drift check: matched pages will be checked.",
        matched=matched_pages,
    )

    # Fan out: fetch all ecosystem files in parallel
    ecosystem_urls: list[tuple[str, str]] = []
    for page in matched_pages:
        for url in mappings[page]:
            ecosystem_urls.append((page, url))

    async with httpx.AsyncClient() as http_client:
        fetch_tasks = [
            _fetch_ecosystem_file(http_client, url) for _, url in ecosystem_urls
        ]
        fetched_contents = await asyncio.gather(*fetch_tasks)

    # Build pairs where both upstream snapshot and ecosystem file are available
    # Upstream page content comes from the snapshots dir
    pairs: list[tuple[str, str, str, str]] = []
    for (page, url), content in zip(ecosystem_urls, fetched_contents, strict=True):
        if content is None:
            continue
        snapshot_path = settings.snapshots_dir / page
        if not snapshot_path.exists():
            logger.warning(
                "Drift check: upstream snapshot not found, skipping pair.",
                page=page,
            )
            continue
        upstream_content = await asyncio.to_thread(
            snapshot_path.read_text, encoding="utf-8"
        )
        pairs.append((page, upstream_content, url, content))

    if not pairs:
        logger.info("Drift check: no fetchable pairs remain after content loading.")
        return None

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.summarizer_max_retries,
    )
    map_model = "claude-haiku-4-5-20251001"
    reduce_model = settings.drift_review_model or "claude-sonnet-4-6"

    # MAP step — fan out per pair with bounded concurrency. A failed pair is
    # skipped rather than aborting the whole check (return_exceptions=True).
    map_tasks = [
        _check_pair(page, upstream_content, url, ecosystem_content, client, map_model)
        for page, upstream_content, url, ecosystem_content in pairs
    ]
    raw_results = await bounded_gather(settings.summarizer_max_concurrency, *map_tasks)
    map_results = []
    for result in raw_results:
        if isinstance(result, BaseException):
            logger.warning(
                "Drift pair check failed, skipping pair.",
                error=str(result),
                status_code=getattr(result, "status_code", None),
            )
            continue
        map_results.append(result)

    # Filter out NO DRIFT pairs before reduce
    drift_findings = [
        (page, url, findings)
        for page, url, findings in map_results
        if findings != "NO DRIFT"
    ]

    if not drift_findings:
        logger.info("Drift check: no drift found across all pairs.")
        return None

    # REDUCE step — synthesize into a single digest
    reduce_input = "\n\n".join(
        f"### {page} vs {url}\n{findings}" for page, url, findings in drift_findings
    )

    try:
        reduce_response = await client.messages.create(
            model=reduce_model,
            max_tokens=1024,
            system=_REDUCE_PROMPT,
            messages=[{"role": "user", "content": reduce_input}],
        )
        digest = reduce_response.content[0].text.strip()
        logger.info(
            "Drift check synthesis complete.",
            input_tokens=reduce_response.usage.input_tokens,
            output_tokens=reduce_response.usage.output_tokens,
        )
    except anthropic.APIError as exc:
        logger.error(
            "Drift check API error during reduce step, skipping.",
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return None

    if digest == "NO DRIFT":
        return None

    return digest
