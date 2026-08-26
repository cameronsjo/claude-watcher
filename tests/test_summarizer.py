"""Tests for summarizer module."""

import asyncio
from unittest.mock import MagicMock, patch

import openai
import pytest

from claude_watcher.config import Settings
from claude_watcher.differ import DiffResult
from claude_watcher.summarizer import (
    _FILE_SUMMARY_PROMPT,
    _fallback_summary,
    _is_trivial_hunk,
    summarize_diff,
)

# ---------------------------------------------------------------------------
# Helpers for the fan-out hardening tests (issue #4)
# ---------------------------------------------------------------------------


def _raw_diff(*filenames: str, body: str = "+added line") -> str:
    """Build a unified diff with one section per filename."""
    return "".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n{body}\n"
        for f in filenames
    )


def _response(text: str = "DIGEST") -> MagicMock:
    """An OpenAI-shaped chat completion with the usage fields the code logs."""
    msg = MagicMock()
    msg.choices = [MagicMock(message=MagicMock(content=text))]
    msg.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return msg


def _reduce_message(text: str = "DIGEST") -> MagicMock:
    return _response(text)


def _map_message(text: str = "- a change") -> MagicMock:
    return _response(text)


def _is_map(kwargs) -> bool:
    """Map (per-file) calls carry the per-file system prompt.

    Both tiers now share one model id, so dispatch on the call's role rather
    than on the model name.
    """
    return kwargs["messages"][0]["content"] == _FILE_SUMMARY_PROMPT


def _rate_limited() -> openai.RateLimitError:
    response = MagicMock(status_code=429, headers={}, request=MagicMock())
    return openai.RateLimitError("rate limited", response=response, body=None)


def _mock_openai(create):
    """Patch the SDK class so `llm.complete` builds our stub client."""
    client = MagicMock()
    client.chat.completions.create = create
    return patch("claude_watcher.llm.openai.AsyncOpenAI", return_value=client)


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("llm_api_key", "gw-test-key")
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_fallback_summary() -> None:
    """Fallback summary includes all change categories with counts."""
    diff = DiffResult(
        new_pages=["new-page.md"],
        removed_pages=["old-page.md"],
        modified_pages=["changed-page.md"],
        raw_diff="diff content",
    )
    summary = _fallback_summary(diff)

    assert "new-page.md" in summary
    assert "old-page.md" in summary
    assert "changed-page.md" in summary
    assert "3 page(s) changed" in summary
    assert "summarizer unavailable" in summary
    assert "**New Pages**" in summary
    assert "**Removed Pages**" in summary
    assert "**Modified Pages**" in summary


def test_fallback_summary_custom_reason() -> None:
    """Fallback summary displays the provided reason."""
    diff = DiffResult(
        modified_pages=["page.md"],
        raw_diff="diff",
    )
    summary = _fallback_summary(diff, reason="API error")

    assert "API error" in summary
    assert "1 page(s) changed" in summary


@pytest.mark.asyncio
async def test_summarize_diff_without_api_key() -> None:
    """Without an API key, falls back to plain-text summary."""
    settings = _settings(llm_api_key="")
    diff = DiffResult(
        new_pages=["page.md"],
        modified_pages=[],
        removed_pages=[],
        raw_diff="some diff",
    )
    summary = await summarize_diff(diff, settings)
    assert "page.md" in summary


# ---------------------------------------------------------------------------
# Per-file isolation: one bad file becomes a stub; the digest is still produced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_file_does_not_abort_digest() -> None:
    """A 429 on a single file yields a stub, not a whole-digest plain fallback."""
    settings = _settings()
    diff = DiffResult(
        modified_pages=["good.md", "bad.md"],
        raw_diff=_raw_diff("good.md", "bad.md"),
    )

    reduce_inputs: list[str] = []

    async def create(**kwargs):
        content = kwargs["messages"][1]["content"]
        if _is_map(kwargs):
            if "bad.md" in content:
                raise _rate_limited()
            return _map_message()
        reduce_inputs.append(content)
        return _reduce_message("SYNTHESIZED DIGEST")

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    # Synthesis ran and produced the digest — not the plain-text fallback.
    assert "SYNTHESIZED DIGEST" in result
    assert "page(s) changed" not in result
    # The failed file was synthesized as a stub, not dropped.
    assert reduce_inputs
    assert "(summary unavailable — bad.md changed)" in reduce_inputs[0]


# ---------------------------------------------------------------------------
# Concurrency cap: peak in-flight map calls never exceed the configured bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_respects_concurrency_cap() -> None:
    """No more than summarizer_max_concurrency map calls run at once."""
    settings = _settings(summarizer_max_concurrency=2)
    files = [f"doc{i}.md" for i in range(6)]
    diff = DiffResult(modified_pages=files, raw_diff=_raw_diff(*files))

    state = {"in_flight": 0, "peak": 0}

    async def create(**kwargs):
        if _is_map(kwargs):
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            for _ in range(3):
                await asyncio.sleep(0)
            state["in_flight"] -= 1
            return _map_message()
        return _reduce_message()

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert state["peak"] <= 2
    assert "DIGEST" in result


# ---------------------------------------------------------------------------
# Truncation: oversized single-file input is clamped to the budget + marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_file_is_truncated() -> None:
    """A single oversized file is truncated to summarizer_max_input_chars."""
    settings = _settings(summarizer_max_input_chars=500)
    diff = DiffResult(
        modified_pages=["big.md"],
        raw_diff=_raw_diff("big.md", body="+" + "x" * 5000),
    )

    captured: dict[str, str] = {}

    async def create(**kwargs):
        content = kwargs["messages"][1]["content"]
        if _is_map(kwargs):
            captured["content"] = content
            return _map_message()
        return _reduce_message()

    with _mock_openai(create):
        await summarize_diff(diff, settings)

    assert len(captured["content"]) <= 500
    assert "[... diff truncated ...]" in captured["content"]


# ---------------------------------------------------------------------------
# max_retries wiring: the SDK client is built with the configured retry budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_built_with_max_retries() -> None:
    """AsyncOpenAI is constructed with base_url, key, and retries from settings."""
    settings = _settings(summarizer_max_retries=5)
    diff = DiffResult(modified_pages=["doc.md"], raw_diff=_raw_diff("doc.md"))

    async def create(**kwargs):
        return _map_message() if _is_map(kwargs) else _reduce_message()

    with _mock_openai(create) as mock_cls:
        await summarize_diff(diff, settings)

    mock_cls.assert_called_once_with(
        base_url=settings.llm_base_url,
        api_key="gw-test-key",
        max_retries=5,
    )


# ---------------------------------------------------------------------------
# Per-run cap: only the first N files fire; the rest surface as deferred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_run_cap_defers_excess_files() -> None:
    """With summarizer_max_files=N, exactly N map calls fire; the rest are deferred."""
    settings = _settings(summarizer_max_files=2)
    files = ["a.md", "b.md", "c.md"]
    diff = DiffResult(modified_pages=files, raw_diff=_raw_diff(*files))

    state = {"map_calls": 0}

    async def create(**kwargs):
        if _is_map(kwargs):
            state["map_calls"] += 1
            return _map_message()
        return _reduce_message()

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert state["map_calls"] == 2
    assert "Deferred" in result
    assert "c.md" in result


# ---------------------------------------------------------------------------
# Triviality filter: prose-free hunks cost no API call at all
# ---------------------------------------------------------------------------


def _hunk(*lines: str, filename: str = "f.md") -> str:
    """A single-file diff chunk with a real header block and hunk marker."""
    body = "\n".join(lines)
    return (
        f"diff --git a/{filename} b/{filename}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        f"@@ -1,3 +1,3 @@\n"
        f"{body}\n"
    )


def test_trivial_whitespace_only_hunk() -> None:
    """Re-wrapped whitespace changes no prose."""
    assert _is_trivial_hunk(_hunk("-Run   the    hook.", "+Run the hook."))


def test_trivial_same_host_link_repoint() -> None:
    """A docs site reshuffling its own paths is noise."""
    assert _is_trivial_hunk(
        _hunk(
            "-See the [hooks guide](https://docs.example.com/hooks).",
            "+See the [hooks guide](https://docs.example.com/guides/hooks).",
        )
    )


def test_link_repointed_to_a_different_host_is_not_trivial() -> None:
    """A retarget to another host is exactly what must reach the model.

    This audience is plugin developers and security engineers; an install
    script or download link that starts pointing somewhere else is the change
    a "formatting/link changes only" one-liner would actively hide.
    """
    assert not _is_trivial_hunk(
        _hunk(
            "-Install with [this script](https://claude.example.com/install.sh).",
            "+Install with [this script](https://evil.example/install.sh).",
        )
    )


def test_prose_smuggled_inside_link_parens_is_not_trivial() -> None:
    r"""`](...)` with no opening bracket is prose, not a link target.

    An unanchored `\]\(.*?\)` erased free text written inside the parens, so a
    substantive sentence classified as trivial and was never summarized.
    """
    assert not _is_trivial_hunk(
        _hunk(
            "-Hooks run sandboxed.](TRUE)",
            "+Hooks run sandboxed.](FALSE - hooks now run unsandboxed as root)",
        )
    )


def test_trivial_anchor_move() -> None:
    """An anchor fragment moving on a bare URL is not prose."""
    assert _is_trivial_hunk(
        _hunk(
            "-Docs: https://example.com/ref#old-anchor",
            "+Docs: https://example.com/ref#new-anchor",
        )
    )


def test_trivial_reference_link_definition() -> None:
    """A reference definition repointed within its host keeps its label."""
    assert _is_trivial_hunk(
        _hunk(
            "-[hooks]: https://docs.example.com/h",
            "+[hooks]: https://docs.example.com/guides/h",
        )
    )


def test_reference_definition_to_another_host_is_not_trivial() -> None:
    assert not _is_trivial_hunk(
        _hunk(
            "-[dl]: https://claude.example.com/a.sh", "+[dl]: https://evil.example/a.sh"
        )
    )


def test_one_word_prose_change_is_not_trivial() -> None:
    """A single changed word must still reach the model."""
    assert not _is_trivial_hunk(
        _hunk("-The hook runs before the tool.", "+The hook runs after the tool.")
    )


def test_pure_reordering_is_not_trivial() -> None:
    """Reordering is a real change; an ordered comparison catches it."""
    assert not _is_trivial_hunk(_hunk("-alpha", "-beta", "+beta", "+alpha"))


def test_header_plus_minus_lines_do_not_defeat_the_filter() -> None:
    """The `--- a/f` / `+++ b/f` header lines must not count as content.

    A naive scan puts them in the removed/added sets, so every hunk looks
    non-trivial and the filter fires on nothing, forever.
    """
    chunk = _hunk("-Run   the hook.", "+Run the hook.")
    assert "--- a/f.md" in chunk and "+++ b/f.md" in chunk
    assert _is_trivial_hunk(chunk)


def test_hunkless_diff_is_not_trivial() -> None:
    """A rename or binary diff carries no hunk — unclassified goes to the model."""
    assert not _is_trivial_hunk(
        "diff --git a/a.md b/b.md\nsimilarity index 100%\nrename from a.md\n"
    )


@pytest.mark.asyncio
async def test_trivial_hunk_makes_zero_api_calls() -> None:
    """Positive control: a provably prose-free file never reaches the model.

    Without this, a header-parsing bug and an over-conservative filter both
    read as "skipped: 0".
    """
    settings = _settings()
    diff = DiffResult(
        modified_pages=["trivial.md"],
        raw_diff=_hunk(
            "-See [guide](https://docs.example.com/g).",
            "+See [guide](https://docs.example.com/guides/g).",
            filename="trivial.md",
        ),
    )

    state = {"map_calls": 0}

    async def create(**kwargs):
        if _is_map(kwargs):
            state["map_calls"] += 1
            return _map_message()
        return _reduce_message()

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert state["map_calls"] == 0
    assert "DIGEST" in result


@pytest.mark.asyncio
async def test_substantive_hunk_still_makes_one_call() -> None:
    """A one-word prose change still costs exactly one map call."""
    settings = _settings()
    diff = DiffResult(
        modified_pages=["real.md"],
        raw_diff=_hunk(
            "-The hook runs before the tool.",
            "+The hook runs after the tool.",
            filename="real.md",
        ),
    )

    state = {"map_calls": 0}

    async def create(**kwargs):
        if _is_map(kwargs):
            state["map_calls"] += 1
            return _map_message()
        return _reduce_message()

    with _mock_openai(create):
        await summarize_diff(diff, settings)

    assert state["map_calls"] == 1


# ---------------------------------------------------------------------------
# Reduce budget: whole sections drop, never a mid-section cut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reduce_input_drops_whole_sections() -> None:
    """An oversized assembled block sheds whole sections and says how many."""
    settings = _settings(summarizer_max_reduce_chars=400)
    files = [f"doc{i}.md" for i in range(10)]
    diff = DiffResult(modified_pages=files, raw_diff=_raw_diff(*files))

    reduce_inputs: list[str] = []

    async def create(**kwargs):
        if _is_map(kwargs):
            return _map_message("x" * 200)
        reduce_inputs.append(kwargs["messages"][1]["content"])
        return _reduce_message()

    with _mock_openai(create):
        await summarize_diff(diff, settings)

    assert reduce_inputs
    block = reduce_inputs[0]
    assert len(block) <= 400
    assert "more file(s) not synthesized" in block
    # Every section that survived is whole: its summary body is intact.
    for section in block.split("\n\n"):
        if section.startswith("### "):
            assert section.endswith("x" * 200)


# ---------------------------------------------------------------------------
# Graceful degrade: an unreachable gateway yields the plain file list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_gateway_degrades_to_file_list() -> None:
    """A connection error on every call falls back to the plain page list."""
    settings = _settings()
    diff = DiffResult(
        modified_pages=["doc.md"],
        raw_diff=_raw_diff("doc.md"),
    )

    async def create(**kwargs):
        raise openai.APIConnectionError(request=MagicMock())

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert "1 page(s) changed" in result
    assert "doc.md" in result


# ---------------------------------------------------------------------------
# An empty synthesis part must not render as a rule above nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_doc_synthesis_does_not_leave_a_leading_rule() -> None:
    """A blank doc section must not ship as `---` with nothing above it.

    Observed live: the doc reduce spent its whole budget on reasoning, returned
    "", and the delivered digest opened with a horizontal rule.
    """
    settings = _settings()
    diff = DiffResult(
        modified_pages=["CHANGELOG.md", "guide.md"],
        raw_diff=_raw_diff("CHANGELOG.md", "guide.md"),
    )

    async def create(**kwargs):
        if _is_map(kwargs):
            return _map_message()
        system = kwargs["messages"][0]["content"]
        if "digest writer" in system:
            # The doc synthesis comes back empty, as it did in production.
            raise openai.OpenAIError("empty completion")
        return _reduce_message("RELEASE NOTES")

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert not result.lstrip().startswith("---")
    assert "\n\n---\n\n---" not in result
    # The changelog synthesis succeeded and must survive the doc failure — one
    # shared try/except previously discarded it and fell back to the page list.
    assert "RELEASE NOTES" in result
    assert "page(s) changed" not in result


@pytest.mark.asyncio
async def test_all_synthesis_failing_falls_back_to_the_page_list() -> None:
    """A partial failure keeps what worked; a total one degrades."""
    settings = _settings()
    diff = DiffResult(
        modified_pages=["CHANGELOG.md", "guide.md"],
        raw_diff=_raw_diff("CHANGELOG.md", "guide.md"),
    )

    async def create(**kwargs):
        if _is_map(kwargs):
            return _map_message()
        raise openai.OpenAIError("both reduces down")

    with _mock_openai(create):
        result = await summarize_diff(diff, settings)

    assert "2 page(s) changed" in result
    assert "guide.md" in result
