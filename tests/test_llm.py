"""Tests for the LLM shim's prompt-budgeting and client construction."""

import re
from unittest.mock import MagicMock, patch

import pytest

from claude_watcher.config import Settings
from claude_watcher.llm import complete, fit_sections


def _sections(n: int, body_chars: int = 100) -> list[str]:
    return [f"### f{i}.md\n{'x' * body_chars}" for i in range(n)]


def test_fit_sections_keeps_everything_that_fits() -> None:
    sections = _sections(3)
    result = fit_sections(sections, 10_000)

    assert result == "\n\n".join(sections)
    assert "not synthesized" not in result


def test_fit_sections_drops_whole_sections_and_says_how_many() -> None:
    result = fit_sections(_sections(10), 400)

    assert len(result) <= 400
    assert "more file(s) not synthesized" in result
    # Whatever survived is whole — no section was cut mid-body.
    for section in result.split("\n\n"):
        if section.startswith("### "):
            assert section.endswith("x" * 100)


def test_fit_sections_respects_the_budget_with_an_oversized_prefix() -> None:
    """An unbounded prefix must not defeat the budget.

    `page_meta` is built from `diff.new_pages`/`removed_pages` and is not
    bounded by the caller — a first run over ~1,500 pages produces a prefix
    longer than the whole reduce budget. Nothing in the section loop inspects
    the prefix, so without an explicit clamp every section drops and the
    result still overflows.
    """
    prefix = "PAGE LIST: " + ", ".join(f"page{i}.md" for i in range(2000))
    assert len(prefix) > 10_000

    result = fit_sections(_sections(5), 200, prefix=prefix)

    assert len(result) <= 200
    assert "page list truncated" in result


def test_fit_sections_keeps_a_prefix_that_fits() -> None:
    result = fit_sections(_sections(2), 10_000, prefix="NEW PAGES:\n  - a.md")

    assert result.startswith("NEW PAGES:\n  - a.md")
    assert "page list truncated" not in result


def test_overflow_notice_counts_only_the_dropped() -> None:
    result = fit_sections(_sections(10), 400)
    match = re.search(r"\*\*(\d+) more file\(s\) not synthesized\*\*", result)
    kept = sum(1 for s in result.split("\n\n") if s.startswith("### "))

    assert match is not None
    assert int(match.group(1)) + kept == 10


@pytest.mark.asyncio
async def test_client_is_built_with_the_unwrapped_secret() -> None:
    """`llm_api_key` is a SecretStr; the SDK still receives the plain value."""
    settings = Settings(
        llm_api_key="gw-test-key",
        _env_file=None,  # type: ignore[call-arg]
    )

    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="hi"))]
    response.usage = MagicMock(prompt_tokens=3, completion_tokens=4)

    async def create(**kwargs):
        return response

    client = MagicMock()
    client.chat.completions.create = create

    with patch("claude_watcher.llm.openai.AsyncOpenAI", return_value=client) as cls:
        text, tokens_in, tokens_out = await complete(
            "sys", "user", 16, model="local/x", settings=settings
        )

    cls.assert_called_once_with(
        base_url=settings.llm_base_url,
        api_key="gw-test-key",
        max_retries=settings.summarizer_max_retries,
    )
    assert (text, tokens_in, tokens_out) == ("hi", 3, 4)


def test_secret_key_does_not_render_in_settings_repr() -> None:
    settings = Settings(
        llm_api_key="gw-super-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert "gw-super-secret" not in repr(settings)
    assert settings.llm_api_key.get_secret_value() == "gw-super-secret"
