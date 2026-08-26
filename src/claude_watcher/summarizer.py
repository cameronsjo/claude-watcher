"""LLM digest generation from documentation diffs."""

import re

import structlog

from claude_watcher import llm
from claude_watcher.concurrency import bounded_gather
from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.llm import LLMError

_TRUNCATION_MARKER = "\n[... diff truncated ...]"

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
Flag anything a plugin developer or security engineer must act on with ⚠️.
Skip categories with no relevant changes.
Keep the total response under {max_chars} characters."""

# Changelog synthesis prompt — release-note style, not categorized
_CHANGELOG_SYNTHESIS_PROMPT = """\
You are summarizing Claude Code changelog entries.
Write a concise release summary: one TL;DR sentence, then bullet points for
each notable change. Group related items. Reference exact version numbers,
flags, and setting names. Skip minor wording fixes.
Keep the total response under {max_chars} characters."""

# The character target handed to the model, derived from the token budget so
# the two cannot drift. The old prompt asked for 3500 characters against a
# 1024-token budget — the instruction and the cap disagreed, and the cap won,
# mid-sentence. Asking for LESS than the budget can emit is the safe direction:
# the model finishes before it is cut off.
#
# Measured against the local preset rather than assumed. Digest-shaped output
# (prose bullets referencing settings and flags) ran 3.81 and 3.98 chars/token,
# but an identifier-dense sample — repeated `--flag-N` / `WATCHER_KEY_N` tokens
# with little prose between them — ran 2.22. Subword splitting on
# snake_case/camelCase and multi-byte glyphs is what pulls it down, and a digest
# is exactly the content that is dense in both. So the constant is floored at
# the WORST observation, not the typical one: at 4 it would over-ask on the very
# input this bug appears on, which is the original defect at a higher threshold.
_CHARS_PER_TOKEN = 2


def _char_target(max_tokens: int) -> int:
    """Characters the model can actually produce within `max_tokens`.

    Clamped at 1 token so a misconfigured budget cannot render the instruction
    "Keep the total response under 0 characters".
    """
    return max(1, max_tokens) * _CHARS_PER_TOKEN


# --- Triviality filter -----------------------------------------------------
# Inline markdown link: `[text](target)`, with an optional `"title"`. Anchored
# on the opening bracket, and the target may not contain whitespace — an
# earlier `\]\(.*?\)` matched a bare `](` with no `[` before it, so arbitrary
# prose written inside the parens was erased along with the target and the
# whole line classified as trivial.
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
# Reference-style link definitions: `[label]: https://...`
_REF_LINK_DEF = re.compile(r"^(\[[^\]]+\]:)\s*(\S+)")
# The scheme+authority of a URL — everything before the first `/`, `?`, or `#`.
_URL_HOST = re.compile(r"([a-zA-Z][\w+.-]*://[^/?#]*)")
# Anchor fragments, but only where they hang off a URL — stripping every `#foo`
# would also eat prose, and prose is exactly what this filter must not ignore.
_URL_ANCHOR = re.compile(r"(https?://\S*?)#[\w./-]+")
_WHITESPACE = re.compile(r"\s+")


def _link_signature(target: str) -> str:
    """The part of a link target whose change is worth a model call.

    A docs site reshuffling its own paths is noise. A link that starts
    pointing at a DIFFERENT HOST — an install script, a download, a security
    page — is exactly the change this watcher's audience must not have
    filtered out. So compare hosts and ignore path, query, and fragment; a
    relative link has no host and compares equal to another relative link.
    """
    match = _URL_HOST.match(target)
    return match.group(1).lower() if match else ""


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


def _normalize_content_line(body: str) -> str:
    """Reduce a diff content line to the prose it carries, or '' if none."""
    stripped = body.strip()
    definition = _REF_LINK_DEF.match(stripped)
    if definition:
        # A link definition carries no prose — keep its label and the target's
        # host, so a path fix reads as no change but a host swap does not.
        label, target = definition.groups()
        return f"{label} {_link_signature(target)}"
    body = _LINK.sub(lambda m: f"[{m.group(1)}]({_link_signature(m.group(2))})", body)
    body = _URL_ANCHOR.sub(r"\1", body)
    return _WHITESPACE.sub(" ", body).strip()


def _is_trivial_hunk(chunk: str) -> bool:
    """True when a file's diff provably changed no prose.

    Whitespace churn, same-host link repointing, and moved anchors cost a model call
    today for a change nobody wants a summary of. Everything else — including
    anything this cannot classify — goes to the model: a missed call is cheap,
    a missed change is not.
    """
    added: list[str] = []
    removed: list[str] = []
    in_hunk = False

    for line in chunk.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        # Everything before the first `@@` is the header block — `diff --git`,
        # `index`, `--- a/f`, `+++ b/f`. Skipping it wholesale is load-bearing:
        # a `+`/`-` scan that saw `+++ b/f` and `--- a/f` would find a mismatch
        # in every hunk, and the filter would fire on nothing, forever.
        if not in_hunk:
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            target = added
        elif line.startswith("-"):
            target = removed
        else:
            continue
        normalized = _normalize_content_line(line[1:])
        if normalized:
            target.append(normalized)

    if not in_hunk:
        # No hunk at all — a rename, mode change, or binary diff. Unclassified,
        # so it goes to the model.
        return False

    # Ordered comparison, not set: a pure reordering is a real change, and a
    # set would also collapse duplicate add/remove pairs.
    return added == removed


def _trivial_summary(filename: str) -> str:
    """One-liner standing in for a file whose diff changed no prose."""
    return f"`{filename}` — formatting/link changes only"


def _stub_summary(filename: str) -> str:
    """Placeholder used when a single file can't be summarized."""
    return f"(summary unavailable — {filename} changed)"


async def _summarize_file(
    filename: str,
    chunk: str,
    model: str,
    max_chars: int,
    settings: Settings,
) -> tuple[str, str]:
    """Summarize a single file diff. Returns (filename, summary).

    Truncates the per-file input to `max_chars` so one oversized doc can't
    overflow the model context window, and isolates per-file API failures: a
    failed call returns a stub instead of aborting the whole digest.
    """
    user_content = f"FILE: {filename}\n\n```diff\n{chunk}\n```"
    if len(user_content) > max_chars:
        # Truncate the diff so the whole message (wrapper + marker) fits the
        # budget — guarantees len(user_content) <= max_chars.
        overhead = len(user_content) - len(chunk)
        budget = max(0, max_chars - overhead - len(_TRUNCATION_MARKER))
        chunk = chunk[:budget] + _TRUNCATION_MARKER
        user_content = f"FILE: {filename}\n\n```diff\n{chunk}\n```"

    try:
        text, _input_tokens, _output_tokens = await llm.complete(
            _FILE_SUMMARY_PROMPT,
            user_content,
            settings.llm_map_max_tokens,
            model=model,
            settings=settings,
        )
    except LLMError as exc:
        logger.warning(
            "Per-file summarization failed, using stub.",
            file=filename,
            error=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
        return filename, _stub_summary(filename)
    return filename, text


async def summarize_diff(diff: DiffResult, settings: Settings) -> str:
    """Generate a categorized summary of documentation changes.

    Uses per-file summarization (fan-out) followed by synthesis (reduce),
    so no diff content is ever truncated regardless of total size.
    Changelog files and doc files are synthesized separately.
    """
    if not settings.summarizer_enabled:
        logger.warning("Summarizer disabled — WATCHER_LLM_API_KEY not set.")
        return _fallback_summary(diff, reason="no API key configured")

    per_file = _split_by_file(diff.raw_diff)

    # Per-run cap — keep the burst bounded. Excess files are surfaced as a
    # deferred list rather than silently dropped.
    items = list(per_file.items())
    deferred: list[str] = []
    if settings.summarizer_max_files > 0 and len(items) > settings.summarizer_max_files:
        deferred = [fname for fname, _ in items[settings.summarizer_max_files :]]
        items = items[: settings.summarizer_max_files]
        logger.warning(
            "Per-run file cap hit; deferring excess files.",
            cap=settings.summarizer_max_files,
            deferred=len(deferred),
        )

    # Prose-free hunks get a mechanical one-liner and no model call at all.
    trivial: dict[str, str] = {}
    substantive: list[tuple[str, str]] = []
    for fname, chunk in items:
        if _is_trivial_hunk(chunk):
            trivial[fname] = _trivial_summary(fname)
        else:
            substantive.append((fname, chunk))

    is_changelog = _CHANGELOG_PATTERN.search

    logger.info(
        "Starting per-file summarization.",
        changelog_files=sum(1 for f, _ in items if is_changelog(f)),
        doc_files=sum(1 for f, _ in items if not is_changelog(f)),
        skipped_trivial=len(trivial),
        summarizing=len(substantive),
        deferred=len(deferred),
    )

    # Fan out — summarize files with bounded concurrency so a large diff drains
    # as a throttled queue instead of bursting past the endpoint's capacity.
    tasks = [
        _summarize_file(
            fname,
            chunk,
            settings.llm_map_model,
            settings.summarizer_max_input_chars,
            settings,
        )
        for fname, chunk in substantive
    ]
    results = await bounded_gather(settings.summarizer_max_concurrency, *tasks)

    # Each result is a (filename, summary) tuple; a stray exception (anything
    # not already caught inside _summarize_file) degrades to a stub.
    summarized: dict[str, str] = {}
    for (fname, _chunk), result in zip(substantive, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Per-file summarization raised unexpectedly, using stub.",
                file=fname,
                error=str(result),
            )
            summarized[fname] = _stub_summary(fname)
        else:
            summarized_name, summary = result
            summarized[summarized_name] = summary

    # Re-inject the skipped files and restore the original diff order.
    summarized.update(trivial)
    file_summaries = {fname: summarized[fname] for fname, _ in items}

    changelog_summaries = {f: s for f, s in file_summaries.items() if is_changelog(f)}
    doc_summaries = {f: s for f, s in file_summaries.items() if not is_changelog(f)}

    # Reduce — synthesize each group. The two groups degrade INDEPENDENTLY: one
    # shared try/except meant a failed doc synthesis also discarded a changelog
    # digest that had already been produced and paid for.
    synthesis_parts: list[str] = []
    attempted = 0
    succeeded = 0

    async def _reduce(
        label: str, prompt: str, block: str, max_tokens: int
    ) -> str | None:
        nonlocal attempted, succeeded
        attempted += 1
        # Format here, not at the call sites: `_reduce` is the one place that
        # holds both the prompt and its budget, so the instruction cannot drift
        # from the cap. The old prompt asked for 3500 characters against a
        # 1024-token budget — they disagreed, and the cap won, mid-sentence.
        try:
            text, input_tokens, output_tokens = await llm.complete(
                prompt.format(max_chars=_char_target(max_tokens)),
                block,
                max_tokens,
                model=settings.llm_reduce_model,
                settings=settings,
            )
        except LLMError as exc:
            logger.error(
                "Synthesis failed for one group; the others still ship.",
                group=label,
                error=str(exc),
                status_code=getattr(exc, "status_code", None),
            )
            return None
        succeeded += 1
        logger.info(
            f"Synthesized {label} summary.",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return text

    if doc_summaries:
        page_meta: list[str] = []
        if diff.new_pages:
            new_list = "\n".join(f"  - {p}" for p in diff.new_pages)
            page_meta.append(f"NEW PAGES:\n{new_list}")
        if diff.removed_pages:
            removed_list = "\n".join(f"  - {p}" for p in diff.removed_pages)
            page_meta.append(f"REMOVED PAGES:\n{removed_list}")

        doc_message = llm.fit_sections(
            [f"### {fname}\n{summary}" for fname, summary in doc_summaries.items()],
            settings.summarizer_max_reduce_chars,
            prefix="\n\n".join(page_meta),
        )
        text = await _reduce(
            "doc", _SYNTHESIS_PROMPT, doc_message, settings.llm_reduce_max_tokens
        )
        if text:
            synthesis_parts.append(text)

    if changelog_summaries:
        changelog_block = llm.fit_sections(
            [
                f"### {fname}\n{summary}"
                for fname, summary in changelog_summaries.items()
            ],
            settings.summarizer_max_reduce_chars,
        )
        text = await _reduce(
            "changelog",
            _CHANGELOG_SYNTHESIS_PROMPT,
            changelog_block,
            settings.llm_changelog_max_tokens,
        )
        if text:
            synthesis_parts.append("**Changelog**\n" + text)

    # Everything we tried failed — there is no digest to send, so degrade to the
    # page list rather than delivering an empty message. A PARTIAL failure keeps
    # what worked; only a total one falls back.
    if attempted and not succeeded:
        return _fallback_summary(diff, reason="API error")

    if deferred:
        deferred_list = "\n".join(f"- `{f}`" for f in deferred)
        synthesis_parts.append(
            f"**Deferred (rate-limit cap): {len(deferred)} file(s)**\n{deferred_list}"
        )

    # Drop empties: a section that came back blank would otherwise render as a
    # horizontal rule with nothing above it. `llm.complete` now raises instead
    # of returning "", so this is the belt to that suspenders.
    return "\n\n---\n\n".join(p for p in synthesis_parts if p.strip())


def _fallback_summary(
    diff: DiffResult,
    reason: str = "summarizer unavailable",
) -> str:
    """Plain-text summary when the LLM gateway is not available.

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
