"""Claude API digest generation from documentation diffs."""

import anthropic
import structlog

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult

logger = structlog.get_logger()

SYSTEM_PROMPT = """\
You are a technical digest writer for Claude Code documentation changes.
Your output will be displayed in a Discord embed, so use Discord markdown.

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
Highlight anything a plugin developer or security engineer should act on with ⚠️.
Skip categories with no relevant changes.
Keep the total response under 3500 characters."""

# Truncate diffs to avoid blowing context window budget
MAX_DIFF_CHARS = 80_000


async def summarize_diff(diff: DiffResult, settings: Settings) -> str:
    """Generate a categorized summary of documentation changes using Claude."""
    if not settings.summarizer_enabled:
        logger.warning("Summarizer disabled — WATCHER_ANTHROPIC_API_KEY not set.")
        return _fallback_summary(diff, reason="no API key configured")

    # Build user message with diff context
    sections: list[str] = []

    if diff.new_pages:
        sections.append("NEW PAGES:\n" + "\n".join(f"  - {p}" for p in diff.new_pages))
    if diff.removed_pages:
        sections.append(
            "REMOVED PAGES:\n" + "\n".join(f"  - {p}" for p in diff.removed_pages)
        )
    if diff.modified_pages:
        sections.append(
            "MODIFIED PAGES:\n" + "\n".join(f"  - {p}" for p in diff.modified_pages)
        )

    raw_diff = diff.raw_diff
    if len(raw_diff) > MAX_DIFF_CHARS:
        raw_diff = raw_diff[:MAX_DIFF_CHARS] + "\n\n[...truncated...]"
        logger.info(
            "Diff truncated for summarization.",
            original_chars=len(diff.raw_diff),
            truncated_to=MAX_DIFF_CHARS,
        )

    sections.append(f"DIFF:\n```\n{raw_diff}\n```")

    user_message = "\n\n".join(sections)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as exc:
        logger.error(
            "Summarizer API call failed, using fallback.",
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return _fallback_summary(diff, reason="API error")

    summary = response.content[0].text
    logger.info(
        "Generated digest summary.",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return summary


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
