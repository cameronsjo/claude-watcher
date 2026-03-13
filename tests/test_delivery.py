"""Tests for delivery module."""

from claude_watcher.delivery import _build_embed, _pick_color
from claude_watcher.differ import DiffResult


def test_pick_color_breaking() -> None:
    """Breaking/security content gets red."""
    assert _pick_color("breaking changes detected") == 0xED4245
    assert _pick_color("Security fix applied") == 0xED4245


def test_pick_color_features() -> None:
    """New features get blurple."""
    assert _pick_color("New feature: hooks API") == 0x5865F2
    assert _pick_color("New page added") == 0x5865F2


def test_pick_color_docs() -> None:
    """Generic docs get green."""
    assert _pick_color("Updated formatting guide") == 0x57F287


def test_build_embed_footer_counts() -> None:
    """Embed footer shows compact change counts."""
    diff = DiffResult(
        new_pages=["a.md", "b.md"],
        removed_pages=["c.md"],
        modified_pages=["d.md", "e.md", "f.md"],
        raw_diff="diff",
    )
    embed = _build_embed("Test summary", diff)

    assert embed["footer"]["text"] == "+2 new · ~3 modified · -1 removed"


def test_build_embed_footer_partial() -> None:
    """Footer omits categories with zero pages."""
    diff = DiffResult(
        modified_pages=["x.md"],
        raw_diff="diff",
    )
    embed = _build_embed("Test summary", diff)

    assert embed["footer"]["text"] == "~1 modified"
    assert "new" not in embed["footer"]["text"]
    assert "removed" not in embed["footer"]["text"]


def test_build_embed_no_footer_when_empty() -> None:
    """No footer when there are somehow no page changes."""
    diff = DiffResult(raw_diff="diff")
    embed = _build_embed("Test summary", diff)

    assert "footer" not in embed


def test_build_embed_truncates_long_description() -> None:
    """Long summaries are truncated to fit Discord limits."""
    long_summary = "x" * 5000
    diff = DiffResult(modified_pages=["a.md"], raw_diff="diff")
    embed = _build_embed(long_summary, diff)

    assert len(embed["description"]) <= 4020
    assert embed["description"].endswith("[...truncated]")
