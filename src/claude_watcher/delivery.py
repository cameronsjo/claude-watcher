"""Discord webhook and email delivery for digests."""

import asyncio
import html
import re
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
import httpx
import structlog

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult

logger = structlog.get_logger()

# Discord embed color codes
COLOR_BREAKING = 0xED4245  # Red — breaking changes or security
COLOR_FEATURES = 0x5865F2  # Blurple — new features
COLOR_DOCS = 0x57F287  # Green — documentation updates

# Discord embeds have a 4096 char description limit
DISCORD_MAX_DESCRIPTION = 4000

# Discord webhooks rate-limit at roughly 5 requests per 2 seconds.
DISCORD_POST_INTERVAL_S = 0.5

# Split points, both zero-width lookarounds so the pieces concatenate back to
# the original digest byte-for-byte.
_HEADING_BOUNDARY = re.compile(r"(?=^#{2,3} )", re.MULTILINE)
_PARAGRAPH_BOUNDARY = re.compile(r"(?<=\n\n)(?=\S)")

_FENCE = "```"
_FENCE_CLOSE = "\n```"
_FENCE_OPEN = "```\n"


def _pick_color(summary: str) -> int:
    """Choose embed color based on digest content severity."""
    lower = summary.lower()
    if "breaking" in lower or "security" in lower:
        return COLOR_BREAKING
    if "new feature" in lower or "new page" in lower:
        return COLOR_FEATURES
    return COLOR_DOCS


def _today_label() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _footer_text(diff: DiffResult) -> str:
    """Compact page-change counts, or '' when the diff carries none.

    The drift path delivers with an empty DiffResult, so this is routinely
    empty and the footer is then omitted entirely.
    """
    parts: list[str] = []
    if diff.new_pages:
        parts.append(f"+{len(diff.new_pages)} new")
    if diff.modified_pages:
        parts.append(f"~{len(diff.modified_pages)} modified")
    if diff.removed_pages:
        parts.append(f"-{len(diff.removed_pages)} removed")
    return " · ".join(parts)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Cut one oversized section at character boundaries, repairing fences.

    A cut landing inside a code fence would render the rest of the part as
    prose and the next part as code. Close the fence at the cut and reopen it
    at the top of the next piece.
    """
    pieces: list[str] = []
    remaining = text
    carry = ""
    while remaining:
        budget = max(1, max_chars - len(carry) - len(_FENCE_CLOSE))
        piece = carry + remaining[:budget]
        remaining = remaining[budget:]
        carry = ""
        if piece.count(_FENCE) % 2 == 1:
            piece += _FENCE_CLOSE
            carry = _FENCE_OPEN
        pieces.append(piece)
    return pieces


def _split_digest(summary: str, max_chars: int = DISCORD_MAX_DESCRIPTION) -> list[str]:
    """Split a digest into ordered parts, each under `max_chars`.

    Prefers heading boundaries, falls back to paragraph breaks, and hard-splits
    only a single section that is oversized on its own. Nothing is dropped —
    absent a hard split the parts concatenate back to the input exactly.
    """
    if not summary.strip():
        return []
    if len(summary) <= max_chars:
        return [summary]

    sections = [s for s in _HEADING_BOUNDARY.split(summary) if s]
    if len(sections) == 1:
        sections = [s for s in _PARAGRAPH_BOUNDARY.split(summary) if s]

    chunks: list[str] = []
    current = ""
    for section in sections:
        if len(section) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(section, max_chars))
            continue
        if len(current) + len(section) > max_chars:
            if current:
                chunks.append(current)
            current = section
        else:
            current += section
    if current:
        chunks.append(current)
    return chunks


def _build_embeds(summary: str, diff: DiffResult) -> list[dict]:
    """Build the ordered Discord embeds for a digest — one per part."""
    chunks = _split_digest(summary)
    if not chunks:
        return []

    base_title = f"Claude Code Digest — {_today_label()}"
    # Color is a property of the whole digest, not of the part it landed in.
    color = _pick_color(summary)
    footer_text = _footer_text(diff)
    total = len(chunks)

    embeds: list[dict] = []
    for n, chunk in enumerate(chunks, start=1):
        embed: dict = {
            "title": base_title if total == 1 else f"{base_title} ({n}/{total})",
            "description": chunk,
            "color": color,
        }
        if n == total and footer_text:
            embed["footer"] = {"text": footer_text}
        embeds.append(embed)
    return embeds


async def deliver_discord(
    summary: str,
    diff: DiffResult,
    settings: Settings,
) -> bool:
    """Send digest to Discord via webhook. Returns True only if ALL parts posted."""
    if not settings.discord_enabled:
        logger.info("Discord delivery skipped, no webhook configured.")
        return True

    embeds = _build_embeds(summary, diff)
    if not embeds:
        # Zero parts would make "every part posted" vacuously true: nothing is
        # POSTed, delivery reports success, main.py commits the snapshot, and
        # the day's real changes are consumed for a digest nobody received.
        logger.error("Discord delivery failed: the digest is empty.")
        return False

    total = len(embeds)
    async with httpx.AsyncClient() as client:
        # Sequential by construction — out-of-order parts are worse than the
        # truncation this replaced, so never gather these.
        for n, embed in enumerate(embeds, start=1):
            try:
                response = await client.post(
                    settings.discord_webhook_url, json={"embeds": [embed]}
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # NOT `str(exc)`: httpx renders the full request URL in its
                # message, and the webhook URL IS the credential — anyone
                # holding it can post to the channel as this bot.
                logger.error(
                    "Discord delivery failed part-way; digest is incomplete.",
                    part=n,
                    of=total,
                    error_type=type(exc).__name__,
                    status_code=getattr(
                        getattr(exc, "response", None), "status_code", None
                    ),
                )
                return False
            if n < total:
                await asyncio.sleep(DISCORD_POST_INTERVAL_S)

    logger.info("Delivered digest to Discord.", parts=total)
    return True


async def deliver_email(
    summary: str,
    diff: DiffResult,
    settings: Settings,
) -> bool:
    """Send digest via email. Returns True on success."""
    if not settings.email_enabled:
        logger.info("Email delivery skipped, no SMTP configured.")
        return True

    subject = f"Claude Code Digest — {_today_label()}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(settings.email_to)

    # Plain text version
    msg.attach(MIMEText(summary, "plain"))

    # HTML version with diff in pre block. Both interpolations are escaped:
    # `summary` is model output and `raw_diff` is upstream page content, so a
    # doc page carrying `</pre><a href=...>` would otherwise land as live
    # markup in the recipient's mail client.
    # Named `html_body`, not `html` — a local named `html` would shadow the
    # module and make `html.escape` inside this very f-string an
    # UnboundLocalError.
    safe_summary = html.escape(summary)
    safe_diff = html.escape(diff.raw_diff[:50_000])
    html_body = f"""\
<html>
<body>
<h2>{subject}</h2>
<div style="white-space: pre-wrap; font-family: sans-serif;">{safe_summary}</div>
<hr>
<h3>Raw Diff</h3>
<pre style="background: #f4f4f4; padding: 12px; overflow-x: auto;
font-size: 12px;">{safe_diff}</pre>
</body>
</html>"""
    msg.attach(MIMEText(html_body, "html"))

    try:
        await aiosmtplib.send(
            msg,
            recipients=settings.email_to,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            start_tls=True,
        )
        logger.info("Delivered digest via email.", recipients=len(settings.email_to))
        return True
    except aiosmtplib.SMTPException as exc:
        logger.error("Email delivery failed.", error=str(exc))
        return False


async def deliver(summary: str, diff: DiffResult, settings: Settings) -> bool:
    """Deliver digest to all configured channels. True only if ALL succeeded.

    The caller commits the snapshot on True, consuming the diff — so "either
    channel worked" is the wrong bar. A partial Discord post that reported
    success would lose the missing parts permanently. An unconfigured channel
    returns True, so this still means "everything configured got everything".
    """
    discord_ok = await deliver_discord(summary, diff, settings)
    email_ok = await deliver_email(summary, diff, settings)

    if not (discord_ok and email_ok):
        logger.error(
            "Delivery incomplete; snapshot must not be committed.",
            discord_ok=discord_ok,
            email_ok=email_ok,
        )
        return False

    return True
