"""Discord webhook and email delivery for digests."""

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


async def deliver_discord(
    summary: str,
    diff: DiffResult,
    settings: Settings,
) -> bool:
    """Send digest to Discord via webhook. Returns True on success."""
    if not settings.discord_enabled:
        logger.info("Discord delivery skipped, no webhook configured.")
        return True

    title = f"Claude Code Digest — {_today_label()}"
    description = summary
    if len(description) > DISCORD_MAX_DESCRIPTION:
        description = description[:DISCORD_MAX_DESCRIPTION] + "\n\n[...truncated]"

    payload: dict = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": _pick_color(summary),
            }
        ],
    }

    # Attach raw diff as file if it's too large for embed
    files = None
    if len(diff.raw_diff) > 2000:
        files = {"file": ("diff.patch", diff.raw_diff.encode(), "text/plain")}

    async with httpx.AsyncClient() as client:
        try:
            if files:
                # Multipart upload with file attachment
                response = await client.post(
                    settings.discord_webhook_url,
                    data={"payload_json": httpx.QueryParams(payload).multi_items()},
                    files=files,
                )
            else:
                response = await client.post(settings.discord_webhook_url, json=payload)
            response.raise_for_status()
            logger.info("Delivered digest to Discord.")
            return True
        except httpx.HTTPError as exc:
            logger.error("Discord delivery failed.", error=str(exc))
            return False


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
    msg["To"] = settings.email_to

    # Plain text version
    msg.attach(MIMEText(summary, "plain"))

    # HTML version with diff in pre block
    html = f"""\
<html>
<body>
<h2>{subject}</h2>
<div style="white-space: pre-wrap; font-family: sans-serif;">{summary}</div>
<hr>
<h3>Raw Diff</h3>
<pre style="background: #f4f4f4; padding: 12px; overflow-x: auto;
font-size: 12px;">{diff.raw_diff[:50_000]}</pre>
</body>
</html>"""
    msg.attach(MIMEText(html, "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            start_tls=True,
        )
        logger.info("Delivered digest via email.", to=settings.email_to)
        return True
    except aiosmtplib.SMTPException as exc:
        logger.error("Email delivery failed.", error=str(exc))
        return False


async def deliver(summary: str, diff: DiffResult, settings: Settings) -> bool:
    """Deliver digest to all configured channels. Returns True if any succeeded."""
    discord_ok = await deliver_discord(summary, diff, settings)
    email_ok = await deliver_email(summary, diff, settings)

    if not discord_ok and not email_ok:
        logger.error("All delivery channels failed.")
        return False

    return True
