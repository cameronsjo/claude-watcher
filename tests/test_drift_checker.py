"""Tests for drift_checker module."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.drift_checker import _is_no_drift, _load_mappings, check_drift

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **kwargs) -> Settings:
    """Create a Settings instance pointing at tmp_path for snapshots."""
    mappings_file = kwargs.pop("drift_mappings_file", tmp_path / "drift-mappings.yaml")
    return Settings(
        snapshots_dir=tmp_path,
        drift_mappings_file=mappings_file,
        llm_api_key="gw-test-key",
        drift_check_enabled=True,
        _env_file=None,  # type: ignore[call-arg]
        **kwargs,
    )


def _write_mappings(path: Path, content: str) -> None:
    path.write_text(content)


def _write_snapshot(tmp_path: Path, filename: str, content: str) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")


def _response(text: str, *, prompt_tokens: int = 10, completion_tokens: int = 5):
    """An OpenAI-shaped chat completion."""
    msg = MagicMock()
    msg.choices = [MagicMock(message=MagicMock(content=text))]
    msg.usage = MagicMock(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return msg


def _rate_limited() -> openai.RateLimitError:
    response = MagicMock(status_code=429, headers={}, request=MagicMock())
    return openai.RateLimitError("rate limited", response=response, body=None)


def _server_error() -> openai.APIStatusError:
    response = MagicMock(status_code=500, headers={}, request=MagicMock())
    return openai.APIStatusError("server error", response=response, body=None)


# The client is built inside `claude_watcher.llm`, so that is the patch point
# for every test in this module.
_PATCH_TARGET = "claude_watcher.llm.openai.AsyncOpenAI"


def _client(create) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create = create
    return client


# ---------------------------------------------------------------------------
# NO DRIFT sentinel normalization
# ---------------------------------------------------------------------------


def test_no_drift_sentinel_is_normalized() -> None:
    """A model that answers 'No drift found.' must not produce a digest."""
    assert _is_no_drift("NO DRIFT")
    assert _is_no_drift("  no drift found.\n")
    assert _is_no_drift("No Drift")
    assert not _is_no_drift("- Hook names changed")


# ---------------------------------------------------------------------------
# Short-circuit: no intersection -> None, zero network/LLM calls
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

    with patch(_PATCH_TARGET) as mock_cls:
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

    with patch(_PATCH_TARGET) as mock_cls:
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
        llm_api_key="gw-test-key",
        drift_mappings_file=mappings_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


def test_drift_check_active_false_with_no_api_key(tmp_path: Path) -> None:
    """drift_check_active is False when llm_api_key is missing.

    This gate is separate from summarizer_enabled and returns False silently —
    missing it would leave the drift check permanently and invisibly dead.
    """
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(mappings_path, "docs__en__hooks.md:\n  - https://example.com/\n")

    settings = Settings(
        drift_check_enabled=True,
        llm_api_key="",
        drift_mappings_file=mappings_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is False


def test_drift_check_active_true_with_llm_key(tmp_path: Path) -> None:
    """The gate opens on the gateway key, not the retired Anthropic one."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(mappings_path, "docs__en__hooks.md:\n  - https://example.com/\n")

    settings = Settings(
        drift_check_enabled=True,
        llm_api_key="gw-test-key",
        drift_mappings_file=mappings_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.drift_check_active is True


def test_drift_check_active_false_when_no_mapping_file(tmp_path: Path) -> None:
    """drift_check_active is False when the mapping file doesn't exist."""
    settings = Settings(
        drift_check_enabled=True,
        llm_api_key="gw-test-key",
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

    create = AsyncMock(
        side_effect=[
            _response("- Hook event names changed from X to Y"),
            _response("WRONG: Hook event names changed from X to Y"),
        ]
    )

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is not None
    assert "WRONG" in result
    assert create.call_count == 2


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

    create = AsyncMock(return_value=_response("NO DRIFT"))

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is None
    # Only the map call should have been made (reduce is skipped when no drift)
    assert create.call_count == 1


# ---------------------------------------------------------------------------
# API error -> graceful fallback (None, no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_error_during_map_returns_none(tmp_path: Path, httpx_mock) -> None:
    """An LLM error during map step returns None without crashing."""
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

    create = AsyncMock(side_effect=_rate_limited())

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is None


@pytest.mark.asyncio
async def test_api_error_during_reduce_returns_none(tmp_path: Path, httpx_mock) -> None:
    """An LLM error during reduce step returns None without crashing."""
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

    create = AsyncMock(
        side_effect=[_response("- Some drift item found"), _server_error()]
    )

    with patch(_PATCH_TARGET, return_value=_client(create)):
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

    create = AsyncMock(
        side_effect=[
            _response("- Agent team launch API changed"),
            _response("OUTDATED: Agent team launch API changed"),
        ]
    )

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is not None
    assert "OUTDATED" in result


# ---------------------------------------------------------------------------
# Client is built with the configured retry budget (issue #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_client_built_with_max_retries(tmp_path: Path, httpx_mock) -> None:
    """AsyncOpenAI is constructed with base_url, key, and retries from settings."""
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__hooks.md", "# Hooks\nContent.")
    httpx_mock.add_response(
        url="https://raw.example.com/hooks-skill.md", text="# Skill\nContent."
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    create = AsyncMock(return_value=_response("NO DRIFT"))

    with patch(_PATCH_TARGET, return_value=_client(create)) as mock_cls:
        await check_drift(diff, settings)

    mock_cls.assert_called_once_with(
        base_url=settings.llm_base_url,
        api_key="gw-test-key",
        max_retries=5,
    )


# ---------------------------------------------------------------------------
# Per-pair isolation: one failed pair is skipped; others still produce a digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_pair_does_not_abort_others(tmp_path: Path, httpx_mock) -> None:
    """A 429 on one pair skips it; the surviving pair's drift still synthesizes.

    _check_pair has no try/except of its own — this asserts the re-raise from
    `llm.complete` still lands in bounded_gather(return_exceptions=True).
    """
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__good.md:\n  - https://raw.example.com/good-skill.md\n"
        "docs__en__bad.md:\n  - https://raw.example.com/bad-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__good.md", "# Good\nContent.")
    _write_snapshot(tmp_path, "docs__en__bad.md", "# Bad\nContent.")
    httpx_mock.add_response(
        url="https://raw.example.com/good-skill.md", text="# Good skill\nContent."
    )
    httpx_mock.add_response(
        url="https://raw.example.com/bad-skill.md", text="# Bad skill\nContent."
    )

    settings = _settings(tmp_path, drift_mappings_file=mappings_path)
    diff = DiffResult(modified_pages=["docs__en__good.md", "docs__en__bad.md"])

    async def create(**kwargs):
        # The map prompt names a single pair; the reduce prompt receives the
        # assembled findings. Dispatch on which pair the user message carries.
        content = kwargs["messages"][1]["content"]
        if "ECOSYSTEM FILE:" not in content:
            return _response("WRONG: good page drifted")
        if "docs__en__bad.md" in content:
            raise _rate_limited()
        return _response("- good page drifted")

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is not None
    assert "WRONG" in result


# ---------------------------------------------------------------------------
# Output budgets are threaded through, not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budgets_reach_the_llm_call(tmp_path: Path, httpx_mock) -> None:
    """The map and reduce budgets come from settings, not from literals.

    Both were hardcoded (512 / 1024) and sized against a provider that emitted
    no reasoning tokens. Under a reasoning model those caps truncated output
    mid-sentence, so a silent revert to a literal here is a real regression.
    """
    mappings_path = tmp_path / "drift-mappings.yaml"
    _write_mappings(
        mappings_path,
        "docs__en__hooks.md:\n  - https://raw.example.com/hooks-skill.md\n",
    )
    _write_snapshot(tmp_path, "docs__en__hooks.md", "# Hooks\nContent.")
    httpx_mock.add_response(
        url="https://raw.example.com/hooks-skill.md", text="# Skill\nContent."
    )

    settings = _settings(
        tmp_path,
        drift_mappings_file=mappings_path,
        llm_map_max_tokens=777,
        llm_reduce_max_tokens=8888,
    )
    diff = DiffResult(modified_pages=["docs__en__hooks.md"])

    seen: list[int] = []

    async def create(**kwargs):
        seen.append(kwargs["max_tokens"])
        # First call is the map step; drift found, so the reduce step follows.
        return _response("- something drifted" if len(seen) == 1 else "WRONG: drift")

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is not None
    assert seen == [777, 8888]
