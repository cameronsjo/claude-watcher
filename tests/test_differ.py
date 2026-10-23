"""Tests for differ module."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_watcher import differ
from claude_watcher.differ import (
    DiffResult,
    _build_commit_message,
    commit_snapshot,
    compute_diff,
)


def test_no_changes_returns_none(tmp_path: Path) -> None:
    """When no files exist in snapshots, diff returns None."""
    result = compute_diff(tmp_path)
    assert result is None


def test_new_file_detected(tmp_path: Path) -> None:
    """A new file in snapshots is detected as a new page."""
    # First commit to establish baseline
    compute_diff(tmp_path)

    # Add a new file
    (tmp_path / "test-page.md").write_text("# Test Page\nContent here.")
    result = compute_diff(tmp_path)

    assert result is not None
    assert result.has_changes
    assert "test-page.md" in result.new_pages


def test_modified_file_detected(tmp_path: Path) -> None:
    """A modified file is detected as modified."""
    # Create file and commit
    (tmp_path / "page.md").write_text("original content")
    compute_diff(tmp_path)
    commit_snapshot(tmp_path, "test")

    # Modify file
    (tmp_path / "page.md").write_text("modified content")
    result = compute_diff(tmp_path)

    assert result is not None
    assert result.has_changes
    assert "page.md" in result.modified_pages


def test_removed_file_detected(tmp_path: Path) -> None:
    """A removed file is detected as removed."""
    # Create file and commit
    (tmp_path / "page.md").write_text("content")
    compute_diff(tmp_path)
    commit_snapshot(tmp_path, "test")

    # Remove file
    (tmp_path / "page.md").unlink()
    result = compute_diff(tmp_path)

    assert result is not None
    assert result.has_changes
    assert "page.md" in result.removed_pages


def test_commit_snapshot(tmp_path: Path) -> None:
    """Committing a snapshot makes subsequent diff return None."""
    (tmp_path / "page.md").write_text("content")
    compute_diff(tmp_path)
    commit_snapshot(tmp_path, "test")

    # No new changes
    result = compute_diff(tmp_path)
    assert result is None


def test_build_commit_message_with_diff_and_summary() -> None:
    """Commit message includes counts, TL;DR, and file lists."""
    diff = DiffResult(
        new_pages=["new-page.md"],
        modified_pages=["auth.md", "hooks.md"],
        removed_pages=[],
        raw_diff="",
    )
    summary = "**TL;DR**: New Bedrock setup wizard and shell execution controls"
    msg = _build_commit_message("full", diff, summary)

    # Subject has counts only
    subject = msg.splitlines()[0]
    assert subject == "docs(full): 1 new, 2 modified"

    # Body has TL;DR
    assert "Bedrock" in msg


def test_build_commit_message_no_summary() -> None:
    """Without a summary, commit message is just the subject."""
    diff = DiffResult(
        modified_pages=["config.md"],
        raw_diff="",
    )
    msg = _build_commit_message("changelog", diff)
    assert msg == "docs(changelog): 1 modified"


def test_build_commit_message_no_diff() -> None:
    """Gracefully handles missing diff."""
    msg = _build_commit_message("full", None)
    assert msg == "docs(full): update"


def test_run_git_disables_detached_auto_maintenance(monkeypatch) -> None:
    """Every git call runs auto-maintenance in the foreground (issue #6)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(differ.subprocess, "run", fake_run)
    differ._run_git(["status"], cwd=Path("."))

    cmd = calls[0]
    assert cmd[0] == "git"
    assert cmd[-1] == "status"
    assert "gc.autoDetach=false" in cmd
    assert "maintenance.autoDetach=false" in cmd


# Runs in its own interpreter: it makes itself a child subreaper, so any git
# process that daemonizes is reparented to IT (standing in for the container's
# PID 1) instead of to the real init, where the leak would be invisible.
_ORPHAN_PROBE = r"""
import ctypes, hashlib, os, pathlib, sys, time

from claude_watcher import differ

PR_SET_CHILD_SUBREAPER = 36
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    print("SKIP"); sys.exit(0)

repo = pathlib.Path(sys.argv[1])
differ._ensure_git_repo(repo)
# `git gc --auto` samples objects/17/: with gc.auto=1 two loose objects there
# trip it. Write blobs whose ids start with 17 so the trigger is certain.
differ._run_git(["config", "gc.auto", "1"], cwd=repo)
found = i = 0
while found < 3:
    body = f"page {i}\n".encode()
    i += 1
    if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest().startswith("17"):
        (repo / f"p{found}.md").write_bytes(body)
        found += 1
differ._run_git(["add", "-A"], cwd=repo)
differ.commit_snapshot(repo, "probe")

me = os.getpid()
orphans = set()
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat = (proc / "stat").read_text()
        except OSError:
            continue
        if int(stat.rsplit(")", 1)[1].split()[1]) == me:
            orphans.add(proc.name)
    time.sleep(0.05)
for pid in orphans:
    try:
        os.waitpid(int(pid), 0)
    except ChildProcessError:
        pass
print(len(orphans))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs prctl")
def test_commit_leaves_no_orphaned_git_process(tmp_path: Path) -> None:
    """A commit that trips auto-gc must not leave a detached git behind.

    Before the fix, git daemonized its auto-gc; the daemon was reparented to
    PID 1 and, with the app as PID 1, became a zombie nobody reaped.
    """
    src = Path(differ.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(src)}
    result = subprocess.run(
        [sys.executable, "-c", _ORPHAN_PROBE, str(tmp_path / "repo")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    last = result.stdout.strip().splitlines()[-1]
    if last == "SKIP":
        pytest.skip("cannot become a child subreaper here")
    assert last == "0", result.stdout + result.stderr
