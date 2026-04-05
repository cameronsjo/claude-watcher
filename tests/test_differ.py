"""Tests for differ module."""

from pathlib import Path

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
