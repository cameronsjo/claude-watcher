"""Tests for drift_checker module."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.drift_checker import (
    VerdictKind,
    _fetch_ecosystem_file,
    _load_mappings,
    check_drift,
    check_mapping_reachability,
    parse_verdict,
)

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
# Verdict parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "kind", "body"),
    [
        ("CLEAN", VerdictKind.CLEAN, ""),
        ("clean.", VerdictKind.CLEAN, ""),
        ("**CLEAN**", VerdictKind.CLEAN, ""),
        ("  CLEAN\nNothing contradicts upstream.\n", VerdictKind.CLEAN, ""),
        ("NO DRIFT", VerdictKind.CLEAN, ""),
        ("  no drift found.\n", VerdictKind.CLEAN, ""),
        ("DRIFT: x", VerdictKind.DRIFT, "x"),
        ("drift:\n- a\n- b", VerdictKind.DRIFT, "- a\n- b"),
        ("**DRIFT:** x", VerdictKind.DRIFT, "x"),
        ("```\nCLEAN\n```", VerdictKind.CLEAN, ""),
        ("```markdown\nDRIFT: x\n```", VerdictKind.DRIFT, "x"),
        ("There is no drift.", VerdictKind.UNPARSEABLE, "There is no drift."),
        ("- Hook names changed", VerdictKind.UNPARSEABLE, "- Hook names changed"),
        ("Clean code matters", VerdictKind.UNPARSEABLE, "Clean code matters"),
        ("", VerdictKind.UNPARSEABLE, ""),
        ("```\n```", VerdictKind.UNPARSEABLE, "```\n```"),
    ],
)
def test_parse_verdict(reply: str, kind: VerdictKind, body: str) -> None:
    """The map reply parses to clean, drift with a body, or unparseable."""
    verdict = parse_verdict(reply)
    assert verdict.kind is kind
    assert verdict.body == body


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
            _response("DRIFT:\n- Hook event names changed from X to Y"),
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


# ---------------------------------------------------------------------------
# GitHub auth on ecosystem fetches (issue #15)
# ---------------------------------------------------------------------------

_TOKEN = "github_pat_test_SECRET_value"
_CONTENTS_URL = (
    "https://api.github.com/repos/cameronsjo/cadence/contents/"
    "plugins/cadence/skills/writing-hooks/SKILL.md?ref=main"
)


@pytest.mark.asyncio
async def test_token_is_sent_as_bearer_with_the_raw_media_type(
    tmp_path: Path, httpx_mock
) -> None:
    httpx_mock.add_response(
        url=_CONTENTS_URL,
        match_headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Accept": "application/vnd.github.raw+json",
        },
        text="# Writing Hooks",
    )
    settings = _settings(tmp_path, github_token=_TOKEN)

    async with httpx.AsyncClient() as client:
        text = await _fetch_ecosystem_file(client, _CONTENTS_URL, settings)

    assert text == "# Writing Hooks"


@pytest.mark.asyncio
async def test_raw_githubusercontent_also_gets_the_token(
    tmp_path: Path, httpx_mock
) -> None:
    url = "https://raw.githubusercontent.com/cameronsjo/cadence/main/README.md"
    httpx_mock.add_response(
        url=url, match_headers={"Authorization": f"Bearer {_TOKEN}"}, text="ok"
    )
    settings = _settings(tmp_path, github_token=_TOKEN)

    async with httpx.AsyncClient() as client:
        assert await _fetch_ecosystem_file(client, url, settings) == "ok"


@pytest.mark.asyncio
async def test_token_is_never_sent_to_a_non_github_host(
    tmp_path: Path, httpx_mock
) -> None:
    """A mapping entry pointing elsewhere must not collect the credential."""
    url = "https://raw.example.com/hooks-skill.md"
    httpx_mock.add_response(url=url, text="body")
    settings = _settings(tmp_path, github_token=_TOKEN)

    async with httpx.AsyncClient() as client:
        await _fetch_ecosystem_file(client, url, settings)

    (request,) = httpx_mock.get_requests()
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_no_token_sends_no_authorization(tmp_path: Path, httpx_mock) -> None:
    httpx_mock.add_response(url=_CONTENTS_URL, text="public")
    settings = _settings(tmp_path)

    async with httpx.AsyncClient() as client:
        await _fetch_ecosystem_file(client, _CONTENTS_URL, settings)

    (request,) = httpx_mock.get_requests()
    assert "Authorization" not in request.headers
    assert request.headers["Accept"] == "application/vnd.github.raw+json"


@pytest.mark.asyncio
async def test_token_never_reaches_logs_or_repr(
    tmp_path: Path, httpx_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(url=_CONTENTS_URL, status_code=404)
    settings = _settings(tmp_path, github_token=_TOKEN)
    (tmp_path / "drift-mappings.yaml").write_text(
        f"docs__en__hooks.md:\n  - {_CONTENTS_URL}\n"
    )

    async with httpx.AsyncClient() as client:
        assert await _fetch_ecosystem_file(client, _CONTENTS_URL, settings) is None
    httpx_mock.add_response(url=_CONTENTS_URL, method="HEAD", status_code=404)
    await check_mapping_reachability(settings)

    captured = capsys.readouterr()
    # Prove the log lines were captured at all, or the next assert is vacuous.
    assert "unreachable" in captured.out + captured.err
    assert _TOKEN not in captured.out + captured.err
    assert _TOKEN not in repr(settings)


def test_github_token_defaults_empty_and_reads_the_watcher_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("WATCHER_GITHUB_TOKEN", raising=False)
    assert Settings(_env_file=None).github_token.get_secret_value() == ""  # type: ignore[call-arg]
    monkeypatch.setenv("WATCHER_GITHUB_TOKEN", _TOKEN)
    assert Settings(_env_file=None).github_token.get_secret_value() == _TOKEN  # type: ignore[call-arg]


def test_token_whitespace_is_stripped_at_load() -> None:
    """A secret mounted from a file ends in a newline; it must not survive."""
    for raw in (f"{_TOKEN}\n", f"{_TOKEN} ", f"  {_TOKEN}\r\n"):
        settings = Settings(github_token=raw, _env_file=None)  # type: ignore[call-arg]
        assert settings.github_token.get_secret_value() == _TOKEN


@pytest.mark.parametrize(
    "bad", [f"{_TOKEN}\u2019", f"ghp_mid\nline{_TOKEN}", f"{_TOKEN}\x00x"]
)
def test_bad_token_fails_settings_load_without_echoing_it(bad: str) -> None:
    """Non-ASCII or an inner control char is a clear startup error, value hidden."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(github_token=bad, _env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "WATCHER_GITHUB_TOKEN" in message
    assert _TOKEN not in message
    assert "ghp_mid" not in message


@pytest.mark.asyncio
async def test_trailing_newline_token_never_reaches_any_log_record(
    tmp_path: Path,
    httpx_mock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression for the h11 "Illegal header value b'Bearer <token>'" leak.

    Two layers: the env value is cleaned at load, and — should a dirty token
    ever reach a request anyway — the failure is logged without its message.
    The transport is mocked, so the h11 error is raised as h11 would raise it.
    """
    settings = _settings(tmp_path, github_token=f"{_TOKEN}\n")
    assert settings.github_token.get_secret_value() == _TOKEN

    # Force the dirty value past validation to exercise the logging sink.
    settings.github_token = SecretStr(f"{_TOKEN}\n")
    leaky = httpx.LocalProtocolError(f"Illegal header value b'Bearer {_TOKEN}\\n'")
    httpx_mock.add_exception(leaky, url=_CONTENTS_URL)
    httpx_mock.add_exception(leaky, url=_CONTENTS_URL, method="HEAD")
    (tmp_path / "drift-mappings.yaml").write_text(
        f"docs__en__hooks.md:\n  - {_CONTENTS_URL}\n"
    )

    with capture_logs() as records:
        async with httpx.AsyncClient() as client:
            assert await _fetch_ecosystem_file(client, _CONTENTS_URL, settings) is None
        assert await check_mapping_reachability(settings) == [_CONTENTS_URL]

    # Prove records were captured, or the absence check below is vacuous.
    assert any(r.get("error_type") == "LocalProtocolError" for r in records)
    assert _TOKEN not in repr(records)
    assert _TOKEN not in caplog.text
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out + captured.err


@pytest.mark.asyncio
async def test_non_ascii_token_does_not_crash_the_startup_probe(
    tmp_path: Path, httpx_mock
) -> None:
    """Load-time validation rejects this; the probe must survive it regardless.

    httpx raises UnicodeEncodeError building the header — not an HTTPError —
    which used to escape the probe and stop the scheduler from starting.
    """
    good = "https://raw.example.com/good.md"
    (tmp_path / "drift-mappings.yaml").write_text(
        f"docs__en__a.md:\n  - {_CONTENTS_URL}\n  - {good}\n"
    )
    httpx_mock.add_response(url=good, method="HEAD", status_code=200)
    settings = _settings(tmp_path)
    settings.github_token = SecretStr(f"{_TOKEN}\u2019")

    with capture_logs() as records:
        unreachable = await check_mapping_reachability(settings)

    assert unreachable == [_CONTENTS_URL]
    assert any(r.get("error_type") == "UnicodeEncodeError" for r in records)
    assert _TOKEN not in repr(records)


@pytest.mark.asyncio
async def test_token_is_never_sent_over_plain_http(tmp_path: Path, httpx_mock) -> None:
    url = "http://api.github.com/repos/o/r/contents/x.md?ref=main"
    httpx_mock.add_response(url=url, text="body")
    settings = _settings(tmp_path, github_token=_TOKEN)

    async with httpx.AsyncClient() as client:
        await _fetch_ecosystem_file(client, url, settings)

    (request,) = httpx_mock.get_requests()
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_token_is_never_sent_to_a_non_default_port(
    tmp_path: Path, httpx_mock
) -> None:
    url = "https://api.github.com:8443/repos/o/r/contents/x.md?ref=main"
    httpx_mock.add_response(url=url, text="body")
    settings = _settings(tmp_path, github_token=_TOKEN)

    async with httpx.AsyncClient() as client:
        await _fetch_ecosystem_file(client, url, settings)

    (request,) = httpx_mock.get_requests()
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
async def test_redirect_does_not_carry_the_token_to_another_host(
    tmp_path: Path, httpx_mock
) -> None:
    """A 3xx to another host is not followed, and would not carry the token."""
    evil = "https://evil.example/collect"
    httpx_mock.add_response(
        url=_CONTENTS_URL, status_code=301, headers={"Location": evil}
    )
    settings = _settings(tmp_path, github_token=_TOKEN)

    # The client this module uses does not follow redirects at all.
    async with httpx.AsyncClient() as client:
        assert await _fetch_ecosystem_file(client, _CONTENTS_URL, settings) is None
    assert [str(r.url) for r in httpx_mock.get_requests()] == [_CONTENTS_URL]

    # And if following is ever turned on, httpx drops Authorization cross-origin.
    httpx_mock.add_response(
        url=_CONTENTS_URL, status_code=301, headers={"Location": evil}
    )
    httpx_mock.add_response(url=evil, text="gotcha")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        await _fetch_ecosystem_file(client, _CONTENTS_URL, settings)
    hop = httpx_mock.get_requests()[-1]
    assert str(hop.url) == evil
    assert "Authorization" not in hop.headers


# ---------------------------------------------------------------------------
# Startup reachability probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reachability_reports_only_the_unreachable(
    tmp_path: Path, httpx_mock
) -> None:
    good = "https://api.github.com/repos/o/r/contents/good.md?ref=main"
    gone = "https://api.github.com/repos/o/r/contents/gone.md?ref=main"
    down = "https://raw.example.com/down.md"
    (tmp_path / "drift-mappings.yaml").write_text(
        f"docs__en__a.md:\n  - {good}\n  - {gone}\n"
        f"docs__en__b.md:\n  - {good}\n  - {down}\n"
    )
    httpx_mock.add_response(url=good, method="HEAD", status_code=200)
    httpx_mock.add_response(url=gone, method="HEAD", status_code=404)
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=down, method="HEAD")

    unreachable = await check_mapping_reachability(_settings(tmp_path))

    assert sorted(unreachable) == sorted([gone, down])
    # Each distinct URL is probed once even when several pages map to it.
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_reachability_with_no_mappings_makes_no_requests(
    tmp_path: Path, httpx_mock
) -> None:
    (tmp_path / "drift-mappings.yaml").write_text("")
    assert await check_mapping_reachability(_settings(tmp_path)) == []
    assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# The shipped mapping file
# ---------------------------------------------------------------------------


def test_shipped_mappings_point_at_the_cadence_monorepo() -> None:
    """Every target is a contents-API URL into cameronsjo/cadence plugins/.

    Paths were verified to exist on cadence main when written; the startup
    probe is what catches them moving later.
    """
    shipped = Path(__file__).resolve().parents[1] / "drift-mappings.yaml"
    mappings = _load_mappings(shipped)

    assert mappings
    prefix = "https://api.github.com/repos/cameronsjo/cadence/contents/plugins/"
    for urls in mappings.values():
        for url in urls:
            assert url.startswith(prefix), url
            assert url.endswith("?ref=main"), url
    # Pre-monorepo targets with no counterpart were dropped, not left to 404.
    assert "docs__en__tools-reference.md" not in mappings


# ---------------------------------------------------------------------------
# Verdict outcomes at the drift-check level
# ---------------------------------------------------------------------------


def _single_pair(tmp_path: Path, httpx_mock) -> tuple[Settings, DiffResult]:
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
    return settings, DiffResult(modified_pages=["docs__en__hooks.md"])


@pytest.mark.asyncio
async def test_clean_verdict_returns_none(tmp_path: Path, httpx_mock) -> None:
    """A CLEAN map verdict skips the reduce call and yields no digest."""
    settings, diff = _single_pair(tmp_path, httpx_mock)
    create = AsyncMock(return_value=_response("**CLEAN**"))

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result is None
    assert create.call_count == 1


@pytest.mark.asyncio
async def test_drift_verdict_sends_body_to_reduce(tmp_path: Path, httpx_mock) -> None:
    """A DRIFT verdict passes its findings, without the token, to reduce."""
    settings, diff = _single_pair(tmp_path, httpx_mock)
    create = AsyncMock(
        side_effect=[
            _response("DRIFT: hook renamed"),
            _response("WRONG: hook renamed"),
        ]
    )

    with patch(_PATCH_TARGET, return_value=_client(create)):
        result = await check_drift(diff, settings)

    assert result == "WRONG: hook renamed"
    reduce_input = create.call_args_list[1].kwargs["messages"][1]["content"]
    assert "hook renamed" in reduce_input
    assert "DRIFT:" not in reduce_input
    assert "unparseable" not in reduce_input


@pytest.mark.asyncio
async def test_unparseable_verdict_surfaces_as_drift(
    tmp_path: Path, httpx_mock
) -> None:
    """A wordy reply is treated as drift, logged, and marked in the digest.

    The reduce model says CLEAN here, so the mark alone carries the pair: an
    unparseable verdict must never vanish silently.
    """
    settings, diff = _single_pair(tmp_path, httpx_mock)
    reply = "There is no drift. " + "x" * 400
    create = AsyncMock(side_effect=[_response(reply), _response("CLEAN")])

    with patch(_PATCH_TARGET, return_value=_client(create)):
        with capture_logs() as logs:
            result = await check_drift(diff, settings)

    assert result is not None
    assert "(verdict unparseable)" in result
    assert "docs__en__hooks.md vs https://raw.example.com/hooks-skill.md" in result
    reduce_input = create.call_args_list[1].kwargs["messages"][1]["content"]
    assert "(verdict unparseable)" in reduce_input

    warnings = [e for e in logs if "unparseable" in e["event"]]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["upstream_page"] == "docs__en__hooks.md"
    assert warnings[0]["ecosystem_url"] == "https://raw.example.com/hooks-skill.md"
    assert warnings[0]["reply_head"] == reply[:200]
