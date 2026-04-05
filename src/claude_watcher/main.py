"""Entry point — scheduler setup and check cycle orchestration."""

import argparse
import asyncio
import logging
from datetime import UTC, datetime

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from claude_watcher.config import Settings
from claude_watcher.delivery import deliver
from claude_watcher.differ import DiffResult, commit_snapshot, compute_diff
from claude_watcher.fetcher import fetch_all_docs, fetch_changelog
from claude_watcher.summarizer import summarize_diff

logger = structlog.get_logger()


def _configure_logging(settings: Settings) -> None:
    """Configure structlog for JSON output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if settings.log_level == "DEBUG"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
    )


def _is_peak_hours() -> bool:
    """Check if current time is peak hours (Mon-Fri 10AM-9PM CST / 16:00-03:00 UTC)."""
    now = datetime.now(tz=UTC)
    # CST = UTC-6, so 10AM CST = 16:00 UTC, 9PM CST = 03:00 UTC (next day)
    weekday = now.weekday()  # 0=Monday, 6=Sunday
    hour = now.hour

    is_weekday = weekday < 5
    # Peak is 16:00-23:59 UTC or 00:00-02:59 UTC (wraps midnight)
    is_peak_time = hour >= 16 or hour < 3

    return is_weekday and is_peak_time


async def _run_pipeline(scope: str, settings: Settings) -> None:
    """Run the fetch → diff → summarize → deliver → commit pipeline."""
    log = logger.bind(scope=scope)
    log.info("Starting check cycle.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch
        if scope == "changelog":
            fetch_result = await fetch_changelog(client, settings)
        else:
            fetch_result = await fetch_all_docs(client, settings)

    if not fetch_result.fetched_pages:
        log.warning("No pages fetched, skipping cycle.")
        return

    # Diff
    diff = compute_diff(settings.snapshots_dir)
    if diff is None:
        log.info("No changes detected.")
        return

    # Summarize
    summary = await summarize_diff(diff, settings)

    # Deliver
    delivered = await deliver(summary, diff, settings)

    # Commit only after successful delivery
    if delivered:
        commit_snapshot(
            settings.snapshots_dir,
            scope,
            settings.git_remote_url,
            diff=diff,
            summary=summary,
        )
    else:
        log.error(
            "Delivery failed, snapshot NOT committed. Changes preserved for next run."
        )


async def check_changelog(settings: Settings) -> None:
    """Scheduled job: check CHANGELOG.md only."""
    await _run_pipeline("changelog", settings)


async def check_docs(settings: Settings) -> None:
    """Scheduled job: check all documentation pages."""
    await _run_pipeline("full", settings)


async def run_scheduler(settings: Settings) -> None:
    """Start the APScheduler with two jobs."""
    scheduler = AsyncIOScheduler()

    # CHANGELOG check — adaptive interval based on peak/off-peak
    def changelog_interval() -> int:
        """Return check interval in hours based on time of day."""
        if _is_peak_hours():
            return settings.changelog_peak_interval_hours
        return settings.changelog_offpeak_interval_hours

    # Start with current interval, reschedule dynamically
    initial_hours = changelog_interval()
    scheduler.add_job(
        check_changelog,
        IntervalTrigger(hours=initial_hours),
        args=[settings],
        id="changelog_check",
        name="CHANGELOG.md check",
    )

    # Full docs check — once daily at configured hour UTC
    scheduler.add_job(
        check_docs,
        CronTrigger(hour=settings.docs_check_hour_utc, minute=0),
        args=[settings],
        id="docs_check",
        name="Full docs check",
    )

    scheduler.start()
    logger.info(
        "Scheduler started.",
        changelog_interval_hours=initial_hours,
        docs_check_hour_utc=settings.docs_check_hour_utc,
    )

    # Keep running
    try:
        while True:
            # Reschedule changelog check if peak/off-peak changed
            current_hours = changelog_interval()
            job = scheduler.get_job("changelog_check")
            if job and job.trigger.interval.total_seconds() != current_hours * 3600:
                scheduler.reschedule_job(
                    "changelog_check",
                    trigger=IntervalTrigger(hours=current_hours),
                )
                logger.info(
                    "Rescheduled changelog check.",
                    new_interval_hours=current_hours,
                    peak=_is_peak_hours(),
                )
            await asyncio.sleep(300)  # Re-evaluate every 5 minutes
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


async def _test_summarizer(settings: Settings) -> None:
    """Run the summarizer with a synthetic diff to verify API connectivity."""
    synthetic_diff = DiffResult(
        new_pages=["docs__new-hooks-api.md"],
        removed_pages=["docs__deprecated-config.md"],
        modified_pages=["CHANGELOG.md", "docs__cli-reference.md"],
        raw_diff=(
            "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
            "--- a/CHANGELOG.md\n"
            "+++ b/CHANGELOG.md\n"
            "@@ -1,3 +1,8 @@\n"
            "+## 1.2.0 (2026-03-13)\n"
            "+- Added new hooks API for PreToolUse events\n"
            "+- Removed deprecated config.legacy_mode setting\n"
            "+- Fixed MCP server connection timeout handling\n"
        ),
    )
    logger.info("Running summarizer test with synthetic diff...")
    summary = await summarize_diff(synthetic_diff, settings)
    print("\n--- Summarizer Output ---")
    print(summary)
    print("--- End ---")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Claude Code documentation watcher",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single full check cycle and exit",
    )
    parser.add_argument(
        "--test-summary",
        action="store_true",
        help="Test the summarizer with a synthetic diff and exit",
    )
    args = parser.parse_args()

    settings = Settings()
    _configure_logging(settings)

    if args.test_summary:
        asyncio.run(_test_summarizer(settings))
    elif args.once:
        asyncio.run(_run_pipeline("full", settings))
    else:
        asyncio.run(run_scheduler(settings))


if __name__ == "__main__":
    main()
