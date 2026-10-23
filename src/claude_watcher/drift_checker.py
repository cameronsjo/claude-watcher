"""Ecosystem drift detection: compare upstream docs against local ecosystem files."""

import asyncio
import enum
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
import yaml

from claude_watcher import llm
from claude_watcher.concurrency import bounded_gather
from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.llm import LLMError

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
Begin your reply with exactly CLEAN on its own line if no drift exists. Otherwise begin
with DRIFT: followed by a concise bullet list."""

# REDUCE prompt: synthesize map results into a prioritized digest
_REDUCE_PROMPT = """\
You are synthesizing drift findings for a Claude Code plugin ecosystem maintainer.
You will receive per-pair drift reports (upstream doc + ecosystem file).
Produce a single prioritized digest. Label each item:
  WRONG — ecosystem file states something that is now incorrect
  OUTDATED — ecosystem file is missing new upstream content that matters
If no pair reports real drift, return exactly: CLEAN
Use Discord markdown. Keep the total response under 2500 characters."""

# Accepted as clean for backward compatibility with the old sentinel prompt.
_LEGACY_NO_DRIFT = "NO DRIFT"
# Shown in the digest for a pair whose verdict could not be parsed.
_UNPARSEABLE_MARK = "(verdict unparseable)"
# How much of an unparseable reply reaches the log.
_LOG_REPLY_CHARS = 200

# The token is only ever attached to these hosts. A mapping file is user-edited;
# a typo'd or hostile entry must not be able to collect the credential.
_GITHUB_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})
# The contents API returns a JSON envelope by default; this asks for the file.
_GITHUB_RAW_ACCEPT = "application/vnd.github.raw+json"
_GITHUB_API_VERSION = "2022-11-28"


class VerdictKind(enum.Enum):
    """The three outcomes of parsing a map reply."""

    CLEAN = "clean"
    DRIFT = "drift"
    UNPARSEABLE = "unparseable"


@dataclass(frozen=True)
class Verdict:
    """A parsed map reply. `body` holds the findings for DRIFT, the raw reply
    for UNPARSEABLE, and is empty for CLEAN."""

    kind: VerdictKind
    body: str = ""


# Markdown a model may wrap the leading token in: bold, italics, inline code,
# a heading, a quote.
_WRAPPER_CHARS = "*_`#> \t"
_OPENING_FENCE = re.compile(r"^```[^\n]*\n?")
_DRIFT_TOKEN = re.compile(r"^DRIFT[*_`]*\s*:[*_`]*\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_verdict(reply: str) -> Verdict:
    """Parse a map reply that should begin with `CLEAN` or `DRIFT:`.

    Tolerates surrounding whitespace, case, a code fence around the reply, and
    markdown wrappers on the token (`**CLEAN**`, `**DRIFT:**`). A leading
    `NO DRIFT` is still clean. Anything else is UNPARSEABLE, which callers
    treat as drift: a wordy "clean" answer must surface, not pass silently.
    """
    text = reply.strip()
    if text.startswith("```"):
        text = _OPENING_FENCE.sub("", text, count=1).rstrip()
        text = text.removesuffix("```").strip()
    if not text:
        return Verdict(VerdictKind.UNPARSEABLE, reply)

    lead = text.lstrip(_WRAPPER_CHARS)
    drift = _DRIFT_TOKEN.match(lead)
    if drift:
        return Verdict(VerdictKind.DRIFT, drift.group(1).strip() or text)

    first_line = lead.partition("\n")[0]
    if first_line.strip().strip("*_`.!:").strip().upper() == "CLEAN":
        return Verdict(VerdictKind.CLEAN)
    if lead.upper().startswith(_LEGACY_NO_DRIFT):
        return Verdict(VerdictKind.CLEAN)
    return Verdict(VerdictKind.UNPARSEABLE, reply)


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


def _request_headers(url: str, settings: Settings) -> dict[str, str]:
    """Headers for fetching one mapped URL: raw media type and, for GitHub, auth."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return {}
    host = parsed.host
    headers: dict[str, str] = {}
    if host == "api.github.com":
        headers["Accept"] = _GITHUB_RAW_ACCEPT
        headers["X-GitHub-Api-Version"] = _GITHUB_API_VERSION
    token = settings.github_token.get_secret_value()
    # Origin, not just host: an `http://` entry would send the token in
    # cleartext, and a non-default port is not GitHub's service. httpx
    # normalizes an explicit :443 on https to `port is None`.
    if (
        token
        and parsed.scheme == "https"
        and host in _GITHUB_HOSTS
        and parsed.port is None
    ):
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_ecosystem_file(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> str | None:
    """Fetch a single raw ecosystem file. Returns content or None on error."""
    try:
        response = await client.get(
            url, headers=_request_headers(url, settings), timeout=20.0
        )
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Failed to fetch ecosystem file.",
            url=url,
            status_code=exc.response.status_code,
        )
        return None
    except Exception as exc:
        # Never log str(exc): this request may carry the bearer token, and some
        # errors quote the offending header — h11's "Illegal header value
        # b'Bearer <token>'" does. The type name and the URL are enough.
        logger.warning(
            "Failed to fetch ecosystem file.",
            url=url,
            error_type=type(exc).__name__,
        )
        return None


async def _probe_url(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> int | str:
    """HEAD one mapped URL. Returns the status code, or the error type name."""
    try:
        response = await client.head(
            url, headers=_request_headers(url, settings), timeout=10.0
        )
    except Exception as exc:
        # The type name only — never the message, which can quote the token.
        return type(exc).__name__
    return response.status_code


async def check_mapping_reachability(settings: Settings) -> list[str]:
    """HEAD every mapped ecosystem URL once and log the ones that fail.

    Run at startup so a mapping that rots (a moved file, a repo gone private,
    an expired token) is loud on the day it breaks instead of silently
    producing "no fetchable pairs" forever. Returns the unreachable URLs.
    Never raises on a per-URL failure of any kind: each probe catches its own
    error, so one bad request cannot stop the scheduler from starting.
    """
    mappings = _load_mappings(settings.drift_mappings_file)
    urls = sorted({url for targets in mappings.values() for url in targets})
    if not urls:
        return []

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_probe_url(client, url, settings) for url in urls)
        )

    authenticated = bool(settings.github_token.get_secret_value())
    unreachable: list[str] = []
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, int) and 200 <= result < 300:
            continue
        unreachable.append(url)
        logger.warning(
            "Drift check: mapped ecosystem file is unreachable.",
            url=url,
            status_code=result if isinstance(result, int) else None,
            error_type=result if isinstance(result, str) else None,
            # A private repo answers 404, not 401/403, without a token.
            hint=None
            if authenticated
            else "WATCHER_GITHUB_TOKEN is unset; private repos read as 404",
        )
    logger.info(
        "Drift check: mapping reachability probed.",
        total=len(urls),
        unreachable=len(unreachable),
        authenticated=authenticated,
    )
    return unreachable


async def _check_pair(
    upstream_page: str,
    upstream_content: str,
    ecosystem_url: str,
    ecosystem_content: str,
    map_model: str,
    settings: Settings,
) -> tuple[str, str, Verdict]:
    """MAP step: check a single (upstream-page, ecosystem-file) pair for drift.

    Returns (upstream_page, ecosystem_url, verdict).
    """
    user_message = (
        f"UPSTREAM PAGE: {upstream_page}\n\n"
        f"```\n{upstream_content[:8000]}\n```\n\n"
        f"ECOSYSTEM FILE: {ecosystem_url}\n\n"
        f"```\n{ecosystem_content[:8000]}\n```"
    )
    text, _input_tokens, _output_tokens = await llm.complete(
        _MAP_PROMPT,
        user_message,
        settings.llm_map_max_tokens,
        model=map_model,
        settings=settings,
    )
    verdict = parse_verdict(text)
    if verdict.kind is VerdictKind.UNPARSEABLE:
        logger.warning(
            "Drift check: map verdict unparseable, treating as drift.",
            upstream_page=upstream_page,
            ecosystem_url=ecosystem_url,
            reply_head=text.strip()[:_LOG_REPLY_CHARS],
        )
    logger.debug(
        "Drift pair checked.",
        upstream_page=upstream_page,
        ecosystem_url=ecosystem_url,
        verdict=verdict.kind.value,
    )
    return upstream_page, ecosystem_url, verdict


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
            _fetch_ecosystem_file(http_client, url, settings)
            for _, url in ecosystem_urls
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

    map_model = settings.llm_map_model
    reduce_model = settings.drift_review_model or settings.llm_reduce_model

    # MAP step — fan out per pair with bounded concurrency. A failed pair is
    # skipped rather than aborting the whole check (return_exceptions=True).
    # _check_pair has no try/except of its own and relies on that.
    map_tasks = [
        _check_pair(page, upstream_content, url, ecosystem_content, map_model, settings)
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

    # Drop clean pairs before reduce. An unparseable verdict counts as drift
    # and carries a visible mark, so a reply that dodged the format surfaces.
    drift_findings: list[tuple[str, str, str]] = []
    unparseable_pairs: list[str] = []
    for page, url, verdict in map_results:
        if verdict.kind is VerdictKind.CLEAN:
            continue
        body = verdict.body
        if verdict.kind is VerdictKind.UNPARSEABLE:
            unparseable_pairs.append(f"{page} vs {url}")
            body = f"{_UNPARSEABLE_MARK}\n{body.strip()}"
        drift_findings.append((page, url, body))

    if not drift_findings:
        logger.info("Drift check: no drift found across all pairs.")
        return None

    # REDUCE step — synthesize into a single digest, under the same assembled
    # -input budget the summarizer's reduce steps use.
    reduce_input = llm.fit_sections(
        [f"### {page} vs {url}\n{findings}" for page, url, findings in drift_findings],
        settings.summarizer_max_reduce_chars,
    )

    try:
        text, input_tokens, output_tokens = await llm.complete(
            _REDUCE_PROMPT,
            reduce_input,
            settings.llm_reduce_max_tokens,
            model=reduce_model,
            settings=settings,
        )
        digest = text.strip()
        logger.info(
            "Drift check synthesis complete.",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except LLMError as exc:
        logger.error(
            "Drift check API error during reduce step, skipping.",
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return None

    if parse_verdict(digest).kind is VerdictKind.CLEAN:
        digest = ""
    # Name unparseable pairs outside the model's prose, so the reduce step
    # cannot drop or reword the mark.
    if unparseable_pairs:
        footer = "\n".join(
            [f"**{_UNPARSEABLE_MARK}** review these pairs by hand:"]
            + [f"- {pair}" for pair in unparseable_pairs]
        )
        digest = f"{digest}\n\n{footer}" if digest else footer

    return digest or None
