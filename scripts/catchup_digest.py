"""One-shot catch-up digest runner.

Fetches the live docs into ``WATCHER_SNAPSHOTS_DIR``, diffs against the committed
baseline snapshot, and surfaces the result WITHOUT delivering or committing.
Used to catch up on what changed while the deployed watcher was dormant, against
a throwaway copy of the baseline so the canonical snapshot repo is never touched.

Honors all ``WATCHER_*`` settings. ``summarize_diff`` returns the
Claude-synthesized digest when ``WATCHER_ANTHROPIC_API_KEY`` is set, and falls
back to a plain file-level summary (no API call) when it is not. The full raw
diff is always written to ``--diff-out`` (default ``/tmp/cw-catchup-diff.txt``)
for inspection.

This deliberately omits ``deliver()`` and ``commit_snapshot()`` from the normal
pipeline: no Discord/email is sent and no commit or push is made.
"""

import argparse
import asyncio

import httpx

from claude_watcher.config import Settings
from claude_watcher.differ import compute_diff
from claude_watcher.fetcher import fetch_all_docs
from claude_watcher.summarizer import summarize_diff


async def _run(diff_out: str) -> int:
    settings = Settings()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        fetch_result = await fetch_all_docs(client, settings)

    if not fetch_result.fetched_pages:
        print("No pages fetched — check connectivity / source URLs.")
        return 1

    diff = compute_diff(settings.snapshots_dir)
    if diff is None:
        print("No changes detected against the baseline snapshot.")
        return 0

    # Always persist the full raw diff for inspection — kept out of stdout so a
    # large multi-week diff doesn't drown the summary.
    with open(diff_out, "w", encoding="utf-8") as fh:
        fh.write(diff.raw_diff)
    print(f"Full raw diff written to: {diff_out}\n")

    # summarize_diff handles both modes: synthesized digest when an API key is
    # configured, plain file-level fallback (no API call) when it is not.
    print(await summarize_diff(diff, settings))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot catch-up digest runner.")
    parser.add_argument(
        "--diff-out",
        default="/tmp/cw-catchup-diff.txt",
        help="Where to write the full raw diff (default: /tmp/cw-catchup-diff.txt)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.diff_out)))


if __name__ == "__main__":
    main()
