"""Claude API digest generation from documentation diffs."""

import anthropic
import structlog

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult

logger = structlog.get_logger()

SYSTEM_PROMPT = """\
You are a technical digest writer for Claude Code documentation changes.
Categorize changes into:
- Security & Permissions
- New Features
- Breaking Changes
- Plugin/Hook/Skill Developer Impact
- Power User Changes
- Documentation Updates
- New Pages / Removed Pages

For each category with changes, write 2-3 sentences summarizing what changed \
and why it matters.
Highlight anything a plugin developer or security engineer should act on.
Skip categories with no relevant changes.
Be concise and specific — reference exact setting names, hook types, or API changes."""

# Truncate diffs to avoid blowing context window budget
MAX_DIFF_CHARS = 80_000


async def summarize_diff(diff: DiffResult, settings: Settings) -> str:
    """Generate a categorized summary of documentation changes using Claude."""
    if not settings.summarizer_enabled:
        logger.info("Summarizer disabled, returning raw diff metadata.")
        return _fallback_summary(diff)

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
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    summary = response.content[0].text
    logger.info("Generated digest summary.", input_tokens=response.usage.input_tokens)
    return summary


def _fallback_summary(diff: DiffResult) -> str:
    """Plain-text summary when Claude API is not configured."""
    lines: list[str] = ["Claude Code Documentation Changes\n"]
    if diff.new_pages:
        lines.append("New Pages:")
        lines.extend(f"  - {p}" for p in diff.new_pages)
    if diff.removed_pages:
        lines.append("Removed Pages:")
        lines.extend(f"  - {p}" for p in diff.removed_pages)
    if diff.modified_pages:
        lines.append("Modified Pages:")
        lines.extend(f"  - {p}" for p in diff.modified_pages)
    return "\n".join(lines)
