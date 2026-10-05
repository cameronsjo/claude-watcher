"""OpenAI-compatible completion shim for the estate LLM gateway.

Every LLM call in this service goes through :func:`complete`. The service used
to talk to the Anthropic Console API directly; it now talks to agentgateway,
which speaks the OpenAI chat-completions dialect and fronts free local
inference. Keeping one shim means the next provider swap touches one file.
"""

import hashlib

import openai
import structlog

from claude_watcher.config import Settings

logger = structlog.get_logger()

# Every error the SDK raises derives from this. Callers catch it instead of
# importing `openai` themselves.
LLMError = openai.OpenAIError


class EmptyCompletionError(LLMError):
    """The model returned no content — usually its whole budget went to reasoning.

    A reasoning model bills its thinking against `max_tokens` but returns it in
    a separate `reasoning_content` field, so a budget that is merely tight
    yields `content == ""` with no error and `finish_reason == "length"`.
    Callers degrade on this the same way they degrade on a transport failure;
    the alternative is shipping an empty section, which reads as a real digest
    with nothing in it.
    """


# Summarization is an extraction task, not a creative one. No call site set a
# temperature before, so all five silently inherited the previous provider's
# default of 1.0 — that is not a dependency worth carrying to a new backend.
_TEMPERATURE = 0.2

# One client per distinct endpoint config. A full-docs run makes ~130 calls;
# constructing a client (and its connection pool) per call would leak pools in
# a process that runs for weeks.
_client_cache: dict[tuple[str, str, int], openai.AsyncOpenAI] = {}


def reset_client_cache() -> None:
    """Drop cached clients. Tests use this; production never needs it."""
    _client_cache.clear()


def _get_client(settings: Settings) -> openai.AsyncOpenAI:
    api_key = settings.llm_api_key.get_secret_value()
    # Digest, not the key itself. The cache dict is never logged today, but one
    # added structlog processor that renders frame locals would print every
    # dict key it walks — and the key is the credential.
    cache_key = (
        settings.llm_base_url,
        hashlib.sha256(api_key.encode()).hexdigest(),
        settings.summarizer_max_retries,
    )
    client = _client_cache.get(cache_key)
    if client is None:
        client = openai.AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key,
            max_retries=settings.summarizer_max_retries,
        )
        _client_cache[cache_key] = client
    return client


async def complete(
    system: str,
    user: str,
    max_tokens: int,
    *,
    model: str,
    settings: Settings,
) -> tuple[str, int, int]:
    """Run one chat completion. Returns (text, input_tokens, output_tokens).

    Raises on failure — never returns a sentinel. Callers own the degrade:
    the summarizer falls back to a per-file stub or the plain file list, and
    the drift checker drops the pair. Both rely on the exception propagating
    into ``bounded_gather(return_exceptions=True)`` or their own ``except``.
    """
    client = _get_client(settings)

    # A reasoning model spends `max_tokens` on thinking it returns in a separate
    # field, so a summarization prompt can burn its whole budget and emit
    # nothing. Summarizing a diff is extraction, not deduction — there is
    # nothing here worth thinking about. Sent through `extra_body` because the
    # value is provider-specific and the SDK's own typing rejects "none".
    extra_body: dict = {}
    if settings.llm_reasoning_effort:
        extra_body["reasoning_effort"] = settings.llm_reasoning_effort

    try:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=_TEMPERATURE,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=extra_body,
        )
    # NotFoundError and RateLimitError are both APIStatusError subclasses, so
    # they must be caught first. A 404 from a mistyped or unrouted model id is
    # permanent — logging it as a rate limit would send a future reader hunting
    # for throttling that never happened.
    except openai.NotFoundError as exc:
        logger.error(
            "LLM model not found — permanent error, check the model id.",
            model=model,
            base_url=settings.llm_base_url,
            error=str(exc),
        )
        raise
    except openai.RateLimitError as exc:
        logger.warning(
            "LLM rate limited after the SDK's retry budget.",
            model=model,
            error=str(exc),
        )
        raise
    except openai.APIStatusError as exc:
        logger.warning(
            "LLM returned an error status.",
            model=model,
            status_code=exc.status_code,
            error=str(exc),
        )
        raise
    except openai.APIConnectionError as exc:
        logger.warning(
            "LLM endpoint unreachable — the summarizer will degrade.",
            model=model,
            base_url=settings.llm_base_url,
            error=str(exc),
        )
        raise

    choice = response.choices[0]
    text = choice.message.content or ""
    usage = response.usage

    # `finish_reason == "length"` is the model saying it was cut off, and it is
    # the ONLY signal that a digest ends mid-sentence — the response is a
    # well-formed 200 either way. Ignoring it shipped two truncated digests to
    # Discord before anyone noticed the last sentence just stopped.
    if choice.finish_reason == "length":
        logger.warning(
            "LLM output hit max_tokens — this response is cut off mid-text.",
            model=model,
            max_tokens=max_tokens,
            content_chars=len(text),
        )

    if not text.strip():
        # Distinct from a transport failure and worth its own message: the call
        # succeeded, the tokens were spent, and nothing came back.
        logger.error(
            "LLM returned an empty completion — the whole budget went to "
            "reasoning or the model emitted nothing.",
            model=model,
            max_tokens=max_tokens,
            finish_reason=choice.finish_reason,
            output_tokens=usage.completion_tokens if usage else 0,
        )
        raise EmptyCompletionError(
            f"{model} returned no content "
            f"(finish_reason={choice.finish_reason}, max_tokens={max_tokens})"
        )

    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    return text, input_tokens, output_tokens


OVERFLOW_NOTICE = "**{n} more file(s) not synthesized**"
PREFIX_TRUNCATION_NOTICE = "\n[... page list truncated ...]"


def fit_sections(sections: list[str], max_chars: int, prefix: str = "") -> str:
    """Assemble a reduce-step input under `max_chars`, dropping WHOLE sections.

    The map step's per-file budget bounds nothing on its own — a hundred-odd
    per-file summaries overflow the window by themselves. This is the budget
    for the assembled block.

    Sections are dropped from the tail and the count is stated. A mid-section
    character cut would hand the model a truncated summary it cannot tell is
    truncated; a dropped section it is told about cannot mislead it.
    """
    # The prefix is a page list, not a summary, and it is NOT bounded by the
    # caller: a 1,500-page first run builds one long enough to defeat the whole
    # budget on its own. Dropping every section then still overflows, because
    # nothing in the loop below ever inspects the prefix. Clamp it first.
    reserve = len(OVERFLOW_NOTICE.format(n=len(sections))) + 2
    prefix_budget = max(0, max_chars - reserve - len(PREFIX_TRUNCATION_NOTICE))
    if len(prefix) > prefix_budget:
        logger.warning(
            "Reduce input prefix over budget; truncating the page list.",
            prefix_chars=len(prefix),
            budget=prefix_budget,
        )
        prefix = prefix[:prefix_budget] + PREFIX_TRUNCATION_NOTICE

    parts: list[str] = [prefix] if prefix else []
    used = len(prefix)
    kept = 0
    for section in sections:
        cost = len(section) + (2 if parts else 0)
        # Reserve room for the notice we may still owe. Sizing it on the
        # worst case (everything remaining is dropped) only ever under-fills.
        reserved = len(OVERFLOW_NOTICE.format(n=len(sections) - kept)) + 2
        if used + cost + reserved > max_chars:
            break
        parts.append(section)
        used += cost
        kept += 1

    dropped = len(sections) - kept
    if dropped:
        parts.append(OVERFLOW_NOTICE.format(n=dropped))
        logger.warning(
            "Reduce input over budget; dropped whole sections from the tail.",
            dropped=dropped,
            kept=kept,
            max_chars=max_chars,
        )
    return "\n\n".join(parts)
