"""Tests for drift_checker module."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.drift_checker import _load_mappings, check_drift

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **kwargs) -> Settings:
    """Create a Settings instance pointing at tmp_path for snapshots."""
    mappings_file = kwargs.pop("drift_mappings_file", tmp_path / "drift-mappings.yaml")
    return Settings(
        snapshots_dir=tmp_path,
        drift_mappings_file=mappings_file,
        anthropic_api_key="sk-ant-test",
        drift_check_enabled=True,
        _env_file=None,  # type: ignore[call-arg]
        **kwargs,
    )


def _write_mappings(path: Path, content: str) -> None:
    path.write_text(content)


def _write_snapshot(tmp_path: Path, filename: str, content: str) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Short-circuit: no intersection -> None, zero network/Anthropic calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_intersection_returns_none_and_no_calls(tmp_path: Path) -> None:
    """When no changed pages match the mapping, check_drift returns None immediately."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )
    settings = _settings(tmp_path, drift_mappings_file=mappings_path)

    diff = DiffResult(
        modified_pages=["docs__en__sub-agents.md"],  # NOT in mappings
        new_pages=[],
    )

    # Patch AsyncAnthropic so we can assert it's never instantiated/called
    with patch("claude_watcher.drift_checker.anthropic.AsyncAnthropic") as mock_cls:
        with patch("claude_watcher.drift_checker.httpx.AsyncClient") as mock_http_cls:
            result = await check_drift(diff, settings)

    assert result is None
    mock_cls.assert_not_called()
    mock_http_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Empty / missing mapping file -> None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_mapping_file_returns_none(tmp_path: Path) -> None:
    """An empty mapping file produces no drift check."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    mappings_path.write_text("")

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)

    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    with patch("claude_watcher.drift_checker.anthropic.AsyncAnthropic") as mock_cls:
        result = await check_drift(diff, settings)

    assert result is None
    mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# _load_mappings: drop malformed entries (non-list / non-string URLs)
# ---------------------------------------------------------------------------


def test_load_mappings_drops_non_string_urls(tmp_path: Path) -> None:
    """Entries with non-string URLs are dropped, not crashed on at fetch time."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    mappings_path.write_text(
        "good.md:\n"
        "  - https://example.com/a.md\n"
        "mixed.md:\n"
        "  - 123\n"
        "  - https://example.com/b.md\n"
        "not_a_list.md: https://example.com/c.md\n"
    )

    result = _load_mappings(mappings_path)

    # Only the all-strings entry survives; mixed-type list and scalar are dropped.
    assert result == {"good.md": ["https://example.com/a.md"]}


# ---------------------------------------------------------------------------
# drift_check_active gate: disabled when toggle is off
# ---------------------------------------------------------------------------


def test_drift_check_active_false_when_disabled(tmp_path: Path) -> None:
    """drift_check_active is False when drift_check_enabled=False."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(mappings_path, "docs__en__hooks.md:\n  - https://example.com/\n")

    settings = Settings(
        drift_check_enabled=False,
        anthropic_api_key="sk-ant-test",
        drift_mappings_file=mappings_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


def test_drift_check_active_false_with_no_api_key(tmp_path: Path) -> None:
    """drift_check_active is False when anthropic_api_key is missing."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(mappings_path, "docs__en__hooks.md:\n  - https://example.com/\n")

    settings = Settings(
        drift_check_enabled=True,
        anthropic_api_key="",
        drift_mappings_file=mappings_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


def test_drift_check_active_false_when_no_mapping_file(tmp_path: Path) -> None:
    """drift_check_active is False when the mapping file doesn't exist."""
    settings = Settings(
        drift_check_enabled=True,
        anthropic_api_key="sk-ant-test",
        drift_mappings_file=tmp_path / "nonexistent.yaml",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


def test_drift_check_active_false_by_default() -> None:
    """With no config at all, drift_check_active is False (safe default)."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


# ---------------------------------------------------------------------------
# Happy path: drift found -> returns digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_found_returns_digest(tmp_path: Path, httpx_mock) -> None:
    """When matched pages have drift, check_drift returns a synthesized digest."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )

    # Write a snapshot of the upstream page so the checker can read it
    _write_snapshot(tmp_path, "docs__en__hooks.md", "# Hooks\nNew hook behavior.")

    httpx_mock.add_response(
        url="https://raw.example.com/hooks-skill.md",
        text="# Writing Hooks\nOld hook behavior.",
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    # Build a mock Anthropic client that returns drift findings in map step
    # and a digest in reduce step
    map_message = MagicMock()
    map_message.content = [MagicMock(text="- Hook event names changed from X to Y")]

    reduce_message = MagicMock()
    reduce_message.content = [
        MagicMock(text="WRONG: Hook event names changed from X to Y")
    ]
    reduce_message.usage = MagicMock(input_tokens=100, output_tokens=50)

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=[map_message, reduce_message]
    )

    with patch(
        "claude_watcher.drift_checker.anthropic.AsyncAnthropic",
        return_value=mock_anthropic,
    ):
        result = await check_drift(diff, settings)

    assert result is not None
    assert "WRONG" in result
    assert mock_anthropic.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# All pairs return NO DRIFT -> returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_drift_returns_none(tmp_path: Path, httpx_mock) -> None:
    """When all map results are NO DRIFT, check_drift returns None."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__skills.md:\n  - https://raw.example.com/skills-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__skills.md", "# Skills\nUnchanged content.")

    httpx_mock.add_response(
        url="https://raw.example.com/skills-skill.md",
        text="# Writing Skills\nAccurate content.",
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__skills.md"])

    map_message = MagicMock()
    map_message.content = [MagicMock(text="NO DRIFT")]

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(return_value=map_message)

    with patch(
        "claude_watcher.drift_checker.anthropic.AsyncAnthropic",
        return_value=mock_anthropic,
    ):
        result = await check_drift(diff, settings)

    assert result is None
    # Only the map call should have been made (reduce is skipped when no drift)
    assert mock_anthropic.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# API error -> graceful fallback (None, no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_error_during_map_returns_none(tmp_path: Path, httpx_mock) -> None:
    """An Anthropic API error during map step returns None without crashing."""
    import anthropic as anthropic_lib

    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__hooks.md", "# Hooks\nContent.")

    httpx_mock.add_response(
        url="https://raw.example.com/hooks-skill.md",
        text="# Skill\nContent.",
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=anthropic_lib.APIStatusError(
            "rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
    )

    with patch(
        "claude_watcher.drift_checker.anthropic.AsyncAnthropic",
        return_value=mock_anthropic,
    ):
        result = await check_drift(diff, settings)

    assert result is None


@pytest.mark.asyncio
async def test_api_error_during_reduce_returns_none(tmp_path: Path, httpx_mock) -> None:
    """An Anthropic API error during reduce step returns None without crashing."""
    import anthropic as anthropic_lib

    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__hooks.md", "# Hooks\nContent.")

    httpx_mock.add_response(
        url="https://raw.example.com/hooks-skill.md",
        text="# Skill\nContent.",
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    map_message = MagicMock()
    map_message.content = [MagicMock(text="- Some drift item found")]

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=[
            map_message,
            anthropic_lib.APIStatusError(
                "server error",
                response=MagicMock(status_code=500, headers={}),
                body=None,
            ),
        ]
    )

    with patch(
        "claude_watcher.drift_checker.anthropic.AsyncAnthropic",
        return_value=mock_anthropic,
    ):
        result = await check_drift(diff, settings)

    assert result is None


# ---------------------------------------------------------------------------
# New pages (not just modified) are also checked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_pages_also_trigger_drift_check(tmp_path: Path, httpx_mock) -> None:
    """Newly added upstream pages (not just modified) trigger drift checks."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__agent-teams.md:\n  - https://raw.example.com/agent-teams-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__agent-teams.md", "# Agent Teams\nContent.")

    httpx_mock.add_response(
        url="https://raw.example.com/agent-teams-skill.md",
        text="# Using Agent Teams\nContent.",
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    # Simulate a NEW page (not modified)
    diff = DiffResult(new_pages=["docs__en__agent-teams.md"], modified_pages=[])

    map_message = MagicMock()
    map_message.content = [MagicMock(text="- Agent team launch API changed")]

    reduce_message = MagicMock()
    reduce_message.content = [MagicMock(text="OUTDATED: Agent team launch API changed")]
    reduce_message.usage = MagicMock(input_tokens=80, output_tokens=30)

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=[map_message, reduce_message]
    )

    with patch(
        "claude_watcher.drift_checker.anthropic.AsyncAnthropic",
        return_value=mock_anthropic,
    ):
        result = await check_drift(diff, settings)

    assert result is not None
    assert "OUTDATED" in result
