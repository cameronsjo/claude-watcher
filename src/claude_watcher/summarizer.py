"""Claude API digest generation from documentation diffs."""

import asyncio
import re

import anthropic
import structlog

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult

logger = structlog.get_logger()

_CHANGELOG_PATTERN = re.compile(r"(?i)changelog")
_FILE_BOUNDARY = re.compile(r"(?=^diff --git )", re.MULTILINE)

# Per-file summarization prompt — focused, no synthesis
_FILE_SUMMARY_PROMPT = """\
You are summarizing a single documentation file diff for Claude Code.
Write 2-5 bullet points covering what changed and why it matters.
Reference exact names: settings, hook types, flags, env vars, commands.
Be specific and concise. Skip boilerplate and unchanged context."""

# Final synthesis prompt — combines per-file summaries into a digest
_SYNTHESIS_PROMPT = """\
You are a technical digest writer for Claude Code documentation changes.
Your output will be displayed in a Discord embed, so use Discord markdown.
You will receive per-file summaries. Synthesize them into a single digest.

Format your response EXACTLY like this:

1. Start with a 1-2 sentence TL;DR of the most important change(s).
2. Then list changes grouped by category using **bold headers**. Only include \
categories that have relevant changes:
   - **Breaking Changes**
   - **Security & Permissions**
   - **New Features**
   - **Plugin/Hook/Skill Developer Impact**
   - **Power User Changes**
   - **Documentation Updates**

Under each category, use bullet points (`-`) with concise descriptions. \
Reference exact setting names, hook types, API changes, or config keys.
Highlight anything a plugin developer or security engineer should act on with \u26a0\ufe0f.
Skip categories with no relevant changes.
Keep the total response under 3500 characters."""

# Changelog synthesis prompt — release-note style, not categorized
_CHANGELOG_SYNTHESIS_PROMPT = """\
You are summarizing Claude Code changelog entries.
Write a concise release summary: one TL;DR sentence, then bullet points for
each notable change. Group related items. Reference exact version numbers,
flags, and setting names. Skip minor wording fixes."""


def _split_by_file(raw_diff: str) -> dict[str, str]:
    """Split a unified diff into per-file chunks keyed by filename."""
    chunks = _FILE_BOUNDARY.split(raw_diff)
    files: dict[str, str] = {}
    for chunk in chunks:
        if not chunk.strip():
            continue
        # Extract filename from "diff --git a/foo b/foo"
        match = re.match(r"diff --git a/(\S+)", chunk)
        if match:
            files[match.group(1)] = chunk
    return files


async def _summarize_file(
    filename: str, chunk: str, client: anthropic.AsyncAnthropic, model: str
) -> tuple[str, str]:
    """Summarize a single file diff. Returns (filename, summary)."""
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        system=_FILE_SUMMARY_PROMPT,
        messages=[
            {"role": "user", "content": f"FILE: {filename}\n\n```diff\n{chunk}\n```"}
        ],
    )
    return filename, response.content[0].text


async def summarize_diff(diff: DiffResult, settings: Settings) -> str:
    """Generate a categorized summary of documentation changes using Claude.

    Uses per-file summarization (fan-out) followed by synthesis (reduce),
    so no diff content is ever truncated regardless of total size.
    Changelog files and doc files are synthesized separately.
    """
    if not settings.summarizer_enabled:
        logger.warning("Summarizer disabled — WATCHER_ANTHROPIC_API_KEY not set.")
        return _fallback_summary(diff, reason="no API key configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    # Fan-out uses Haiku — fast and cheap for focused per-file work
    map_model = "claude-haiku-4-5-20251001"
    # Synthesis uses Sonnet for higher-quality cross-file reasoning
    reduce_model = "claude-sonnet-4-20250514"

    per_file = _split_by_file(diff.raw_diff)

    is_changelog = _CHANGELOG_PATTERN.search
    changelog_files = {f: c for f, c in per_file.items() if is_changelog(f)}
    doc_files = {f: c for f, c in per_file.items() if not is_changelog(f)}

    logger.info(
        "Starting per-file summarization.",
        changelog_files=len(changelog_files),
        doc_files=len(doc_files),
    )

    # Fan out — summarize all files in parallel
    try:
        tasks = [
            _summarize_file(fname, chunk, client, map_model)
            for fname, chunk in per_file.items()
        ]
        results = await asyncio.gather(*tasks)
    except anthropic.APIError as exc:
        logger.error(
            "Summarizer API call failed during fan-out, using fallback.",
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return _fallback_summary(diff, reason="API error")

    file_summaries: dict[str, str] = dict(results)

    changelog_summaries = {f: s for f, s in file_summaries.items() if is_changelog(f)}
    doc_summaries = {f: s for f, s in file_summaries.items() if not is_changelog(f)}

    # Reduce — synthesize each group
    synthesis_parts: list[str] = []

    try:
        if doc_summaries:
            doc_block = "\n\n".join(
                f"### {fname}\n{summary}" for fname, summary in doc_summaries.items()
            )
            page_meta: list[str] = []
            if diff.new_pages:
                new_list = "\n".join(f"  - {p}" for p in diff.new_pages)
                page_meta.append(f"NEW PAGES:\n{new_list}")
            if diff.removed_pages:
                removed_list = "\n".join(f"  - {p}" for p in diff.removed_pages)
                page_meta.append(f"REMOVED PAGES:\n{removed_list}")

            doc_message = "\n\n".join([*page_meta, doc_block])
            doc_response = await client.messages.create(
                model=reduce_model,
                max_tokens=1024,
                system=_SYNTHESIS_PROMPT,
                messages=[{"role": "user", "content": doc_message}],
            )
            synthesis_parts.append(doc_response.content[0].text)
            logger.info(
                "Synthesized doc summary.",
                input_tokens=doc_response.usage.input_tokens,
                output_tokens=doc_response.usage.output_tokens,
            )

        if changelog_summaries:
            changelog_block = "\n\n".join(
                f"### {fname}\n{summary}"
                for fname, summary in changelog_summaries.items()
            )
            cl_response = await client.messages.create(
                model=reduce_model,
                max_tokens=512,
                system=_CHANGELOG_SYNTHESIS_PROMPT,
                messages=[{"role": "user", "content": changelog_block}],
            )
            synthesis_parts.append("**Changelog**\n" + cl_response.content[0].text)
            logger.info(
                "Synthesized changelog summary.",
                input_tokens=cl_response.usage.input_tokens,
                output_tokens=cl_response.usage.output_tokens,
            )
    except anthropic.APIError as exc:
        logger.error(
            "Summarizer API call failed during synthesis, using fallback.",
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return _fallback_summary(diff, reason="API error")

    return "\n\n---\n\n".join(synthesis_parts)


def _fallback_summary(
    diff: DiffResult,
    reason: str = "summarizer unavailable",
) -> str:
    """Plain-text summary when Claude API is not available.

    Produces a Discord-markdown-friendly summary with change counts
    and categorized file lists. The reason parameter surfaces WHY
    the fallback was used (no key, API error, etc.).
    """
    total = len(diff.new_pages) + len(diff.removed_pages) + len(diff.modified_pages)
    header = f"**{total} page(s) changed** ({reason})\n"
    lines: list[str] = [header]

    if diff.new_pages:
        lines.append(f"**New Pages** ({len(diff.new_pages)})")
        lines.extend(f"- `{p}`" for p in diff.new_pages)
        lines.append("")
    if diff.removed_pages:
        lines.append(f"**Removed Pages** ({len(diff.removed_pages)})")
        lines.extend(f"- `{p}`" for p in diff.removed_pages)
        lines.append("")
    if diff.modified_pages:
        lines.append(f"**Modified Pages** ({len(diff.modified_pages)})")
        lines.extend(f"- `{p}`" for p in diff.modified_pages)
        lines.append("")

    return "\n".join(lines)
