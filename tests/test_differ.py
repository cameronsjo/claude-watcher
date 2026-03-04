"""Tests for differ module."""

from pathlib import Path

from claude_watcher.differ import commit_snapshot, compute_diff


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
