"""Git diff operations for detecting documentation changes."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()


@dataclass
class DiffResult:
    """Structured result from a git diff operation."""

    new_pages: list[str] = field(default_factory=list)
    removed_pages: list[str] = field(default_factory=list)
    modified_pages: list[str] = field(default_factory=list)
    raw_diff: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.new_pages or self.removed_pages or self.modified_pages)


def _run_git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given directory."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _ensure_git_repo(snapshots_dir: Path) -> None:
    """Initialize git repo in snapshots dir if it doesn't exist.

    Handles fresh volumes, bind mounts, and first-run scenarios.
    """
    git_dir = snapshots_dir / ".git"
    if not git_dir.exists():
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        _run_git(["init", "-b", "main"], cwd=snapshots_dir)
        # Set local identity so commits work without global git config
        _run_git(["config", "user.name", "claude-watcher"], cwd=snapshots_dir)
        _run_git(
            ["config", "user.email", "claude-watcher@localhost"],
            cwd=snapshots_dir,
        )
        _run_git(
            ["commit", "--allow-empty", "-m", "init"],
            cwd=snapshots_dir,
        )
        logger.info("Initialized git repo in snapshots directory.")


def compute_diff(snapshots_dir: Path) -> DiffResult | None:
    """Compute diff between current snapshots and last committed state.

    Returns None if no changes detected.
    """
    _ensure_git_repo(snapshots_dir)

    # Stage everything so we can diff against HEAD
    _run_git(["add", "-A"], cwd=snapshots_dir)

    # Check for any staged changes
    stat_result = _run_git(
        ["diff", "--cached", "--stat"], cwd=snapshots_dir, check=False
    )
    if not stat_result.stdout.strip():
        logger.info("No changes detected in snapshots.")
        # Unstage
        _run_git(["reset", "HEAD"], cwd=snapshots_dir, check=False)
        return None

    # Get full diff
    diff_result = _run_git(["diff", "--cached"], cwd=snapshots_dir, check=False)

    # Get list of changed files with status
    name_status = _run_git(
        ["diff", "--cached", "--name-status"], cwd=snapshots_dir, check=False
    )

    result = DiffResult(raw_diff=diff_result.stdout)

    for line in name_status.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, filename = parts
        if status == "A":
            result.new_pages.append(filename)
        elif status == "D":
            result.removed_pages.append(filename)
        elif status.startswith("M") or status.startswith("R"):
            result.modified_pages.append(filename)

    # Unstage — commit happens after successful delivery
    _run_git(["reset", "HEAD"], cwd=snapshots_dir, check=False)

    logger.info(
        "Diff computed.",
        new=len(result.new_pages),
        removed=len(result.removed_pages),
        modified=len(result.modified_pages),
    )
    return result


def commit_snapshot(snapshots_dir: Path, scope: str) -> None:
    """Commit current snapshot state after successful delivery."""
    _ensure_git_repo(snapshots_dir)
    _run_git(["add", "-A"], cwd=snapshots_dir)

    # Only commit if there are staged changes
    stat = _run_git(["diff", "--cached", "--stat"], cwd=snapshots_dir, check=False)
    if not stat.stdout.strip():
        return

    _run_git(
        ["commit", "-m", f"chore: update {scope} snapshot"],
        cwd=snapshots_dir,
    )
    logger.info("Committed snapshot.", scope=scope)
