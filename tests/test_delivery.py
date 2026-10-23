"""Tests for delivery module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from claude_watcher import delivery
from claude_watcher.config import Settings
from claude_watcher.delivery import (
    DISCORD_MAX_DESCRIPTION,
    DISCORD_MAX_PARTS,
    _build_embeds,
    _pick_color,
    _split_digest,
    deliver,
    deliver_discord,
)
from claude_watcher.differ import DiffResult

WEBHOOK = "https://discord.com/api/webhooks/test"


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("discord_webhook_url", WEBHOOK)
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def _mock_post(post):
    """Patch httpx.AsyncClient so the webhook POSTs land on `post`."""
    client = MagicMock()
    client.post = post
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("claude_watcher.delivery.httpx.AsyncClient", return_value=ctx)


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    return response


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
    embeds = _build_embeds("Test summary", diff)

    assert len(embeds) == 1
    assert embeds[0]["footer"]["text"] == "+2 new · ~3 modified · -1 removed"


def test_build_embed_footer_partial() -> None:
    """Footer omits categories with zero pages."""
    diff = DiffResult(modified_pages=["x.md"], raw_diff="diff")
    embeds = _build_embeds("Test summary", diff)

    assert embeds[0]["footer"]["text"] == "~1 modified"
    assert "new" not in embeds[0]["footer"]["text"]
    assert "removed" not in embeds[0]["footer"]["text"]


def test_build_embed_no_footer_when_empty() -> None:
    """No footer when there are no page changes — the drift path's shape."""
    embeds = _build_embeds("Test summary", DiffResult(raw_diff="diff"))
    assert "footer" not in embeds[0]


def test_single_chunk_keeps_the_bare_title() -> None:
    """The common case is unchanged: no (n/m) suffix."""
    embeds = _build_embeds("short", DiffResult())
    assert embeds[0]["title"].endswith(tuple("0123456789"))
    assert "(" not in embeds[0]["title"]


# ---------------------------------------------------------------------------
# Chunking: nothing is dropped, and nothing is marked truncated
# ---------------------------------------------------------------------------


def _long_digest(sections: int = 6, body_chars: int = 1500) -> str:
    return "".join(
        f"## Section {i}\n{'word ' * (body_chars // 5)}\n\n" for i in range(sections)
    )


def test_split_digest_is_lossless() -> None:
    """The parts concatenate back to the input exactly."""
    digest = _long_digest()
    chunks = _split_digest(digest)

    assert len(chunks) > 1
    assert "".join(chunks) == digest
    assert all(len(c) <= DISCORD_MAX_DESCRIPTION for c in chunks)


def test_split_digest_short_input_is_one_chunk() -> None:
    assert _split_digest("just a line") == ["just a line"]


def test_split_digest_empty_input_is_no_chunks() -> None:
    assert _split_digest("") == []
    assert _split_digest("   \n ") == []


def test_split_digest_falls_back_to_paragraphs() -> None:
    """With no headings, paragraph breaks are the boundary."""
    digest = "".join(f"{'word ' * 300}\n\n" for _ in range(6))
    chunks = _split_digest(digest)

    assert len(chunks) > 1
    assert "".join(chunks) == digest


def test_hard_split_repairs_code_fences() -> None:
    """A cut inside a fence closes it and reopens it on the next part."""
    digest = "## One\n```\n" + ("x" * 9000) + "\n```\n"
    chunks = _split_digest(digest)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk[:80]
        assert len(chunk) <= DISCORD_MAX_DESCRIPTION


def test_numbered_titles_and_last_chunk_footer() -> None:
    """Parts are numbered n/m; only the last carries the footer."""
    diff = DiffResult(modified_pages=["a.md"], raw_diff="d")
    embeds = _build_embeds(_long_digest(), diff)

    total = len(embeds)
    assert total > 1
    for n, embed in enumerate(embeds, start=1):
        assert embed["title"].endswith(f"({n}/{total})")
        assert ("footer" in embed) is (n == total)
    # One color for the whole digest.
    assert len({e["color"] for e in embeds}) == 1
    assert not any("truncated" in e["description"] for e in embeds)


# ---------------------------------------------------------------------------
# Delivery honesty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_parts_posted_in_order() -> None:
    """Every part POSTs, in order, and delivery reports success."""
    posted: list[str] = []

    async def post(url, json):
        posted.append(json["embeds"][0]["title"])
        return _ok_response()

    with (
        _mock_post(post),
        patch("claude_watcher.delivery.DISCORD_POST_INTERVAL_S", 0),
    ):
        ok = await deliver_discord(_long_digest(), DiffResult(), _settings())

    assert ok is True
    assert len(posted) > 1
    assert posted == sorted(posted, key=lambda t: int(t.split("(")[1].split("/")[0]))


@pytest.mark.asyncio
async def test_partial_post_reports_failure() -> None:
    """A part-way failure is a failed delivery, not a success."""
    state = {"calls": 0}

    async def post(url, json):
        state["calls"] += 1
        if state["calls"] == 2:
            raise httpx.ConnectError("boom")
        return _ok_response()

    with (
        _mock_post(post),
        patch("claude_watcher.delivery.DISCORD_POST_INTERVAL_S", 0),
    ):
        ok = await deliver_discord(_long_digest(), DiffResult(), _settings())

    assert ok is False


@pytest.mark.asyncio
async def test_empty_digest_reports_failure() -> None:
    """Zero parts must not read as 'every part posted'."""
    posted: list[str] = []

    async def post(url, json):
        posted.append(json["embeds"][0]["title"])
        return _ok_response()

    with _mock_post(post):
        ok = await deliver_discord("", DiffResult(), _settings())

    assert ok is False
    assert posted == []


@pytest.mark.asyncio
async def test_deliver_reports_failure_when_discord_partially_failed() -> None:
    """`deliver` must not launder a failed Discord post into an overall success."""

    async def post(url, json):
        raise httpx.ConnectError("boom")

    with (
        _mock_post(post),
        patch("claude_watcher.delivery.DISCORD_POST_INTERVAL_S", 0),
    ):
        # Email is unconfigured, so it reports True — the old `either` rule
        # would have made the whole delivery succeed.
        ok = await deliver("some digest", DiffResult(), _settings())

    assert ok is False


# ---------------------------------------------------------------------------
# Rate limits (429 + Retry-After) — real httpx transport via pytest-httpx
# ---------------------------------------------------------------------------


def _three_part_digest() -> str:
    section = "## Section {n}\n\n" + ("x" * 3000) + "\n\n"
    return "".join(section.format(n=n) for n in range(3))


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every delivery sleep instead of performing it."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(delivery, "_sleep", fake_sleep)
    return recorded


def _titles(httpx_mock: HTTPXMock) -> list[str]:
    return [
        json.loads(req.content)["embeds"][0]["title"]
        for req in httpx_mock.get_requests()
    ]


@pytest.mark.asyncio
async def test_429_waits_retry_after_and_retries_only_that_part(
    httpx_mock: HTTPXMock, waits: list[float]
) -> None:
    """A 429 on part 2 sleeps Retry-After, re-posts part 2, never part 1."""
    httpx_mock.add_response(url=WEBHOOK, status_code=204)
    httpx_mock.add_response(
        url=WEBHOOK, status_code=429, headers={"Retry-After": "1.5"}
    )
    httpx_mock.add_response(url=WEBHOOK, status_code=204)
    httpx_mock.add_response(url=WEBHOOK, status_code=204)

    ok = await deliver_discord(_three_part_digest(), DiffResult(), _settings())

    assert ok is True
    titles = _titles(httpx_mock)
    assert [t.split("(")[1] for t in titles] == ["1/3)", "2/3)", "2/3)", "3/3)"]
    assert 1.5 in waits


@pytest.mark.asyncio
async def test_429_body_retry_after_is_the_fallback(
    httpx_mock: HTTPXMock, waits: list[float]
) -> None:
    """Without the header, Discord's JSON `retry_after` sets the wait."""
    httpx_mock.add_response(
        url=WEBHOOK, status_code=429, json={"retry_after": 0.25, "global": False}
    )
    httpx_mock.add_response(url=WEBHOOK, status_code=204)

    ok = await deliver_discord("short digest", DiffResult(), _settings())

    assert ok is True
    assert waits == [0.25]


@pytest.mark.asyncio
async def test_429_retries_are_bounded_per_part(
    httpx_mock: HTTPXMock, waits: list[float]
) -> None:
    """A part that keeps drawing 429 fails delivery after the retry cap."""
    httpx_mock.add_response(
        url=WEBHOOK,
        status_code=429,
        headers={"Retry-After": "0.1"},
        is_reusable=True,
    )

    ok = await deliver_discord("short digest", DiffResult(), _settings())

    assert ok is False
    assert len(httpx_mock.get_requests()) == delivery.DISCORD_MAX_RETRIES_PER_PART + 1
    assert len(waits) == delivery.DISCORD_MAX_RETRIES_PER_PART


@pytest.mark.asyncio
async def test_429_total_wait_is_bounded(
    httpx_mock: HTTPXMock, waits: list[float]
) -> None:
    """A Retry-After past the total wait budget fails fast instead of sleeping."""
    httpx_mock.add_response(
        url=WEBHOOK,
        status_code=429,
        headers={"Retry-After": str(delivery.DISCORD_MAX_TOTAL_RETRY_WAIT_S + 1)},
    )

    ok = await deliver_discord("short digest", DiffResult(), _settings())

    assert ok is False
    assert waits == []
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_429_failure_log_does_not_leak_the_webhook(
    httpx_mock: HTTPXMock, waits: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    """The webhook URL is the credential; the give-up path must not log it."""
    httpx_mock.add_response(
        url=WEBHOOK,
        status_code=429,
        headers={"Retry-After": "0"},
        is_reusable=True,
    )

    ok = await deliver_discord("short digest", DiffResult(), _settings())

    assert ok is False
    captured = capsys.readouterr()
    assert WEBHOOK not in captured.out + captured.err


@pytest.mark.parametrize("header", ["soon", "nan", "-3"])
@pytest.mark.asyncio
async def test_unparseable_retry_after_uses_the_default_wait(
    httpx_mock: HTTPXMock, waits: list[float], header: str
) -> None:
    httpx_mock.add_response(
        url=WEBHOOK, status_code=429, headers={"Retry-After": header}
    )
    httpx_mock.add_response(url=WEBHOOK, status_code=204)

    ok = await deliver_discord("short digest", DiffResult(), _settings())

    assert ok is True
    assert waits == [delivery.DISCORD_DEFAULT_RETRY_WAIT_S]


# ---------------------------------------------------------------------------
# Part cap
# ---------------------------------------------------------------------------


def _many_sections(n: int) -> str:
    return "".join(f"## S{i}\n\n" + ("y" * 50) + "\n\n" for i in range(n))


def test_split_digest_caps_parts_with_an_omitted_tail() -> None:
    """Past the cap, the last part states how many parts were dropped."""
    digest = _many_sections(10)
    uncapped = _split_digest(digest, max_chars=70, max_parts=0)
    assert len(uncapped) == 10

    capped = _split_digest(digest, max_chars=70, max_parts=4)

    assert len(capped) == 4
    assert capped[:3] == uncapped[:3]
    assert "7 more part(s) omitted" in capped[3]
    assert "4-part" in capped[3]


def test_split_digest_at_the_cap_is_untouched() -> None:
    digest = _many_sections(4)
    assert _split_digest(digest, max_chars=70, max_parts=4) == _split_digest(
        digest, max_chars=70, max_parts=0
    )


def test_default_cap_clears_the_realistic_worst_case() -> None:
    """~17 parts is the known worst case; the default cap must not bind there."""
    assert DISCORD_MAX_PARTS > 17


def test_build_embeds_numbers_against_the_capped_total() -> None:
    digest = "".join(
        f"## S{i}\n\n" + ("z" * (DISCORD_MAX_DESCRIPTION - 20)) + "\n\n"
        for i in range(DISCORD_MAX_PARTS + 5)
    )
    embeds = _build_embeds(digest, DiffResult())

    assert len(embeds) == DISCORD_MAX_PARTS
    assert embeds[-1]["title"].endswith(f"({DISCORD_MAX_PARTS}/{DISCORD_MAX_PARTS})")
    assert "omitted" in embeds[-1]["description"]
