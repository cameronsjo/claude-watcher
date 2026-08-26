# ADR 0002: Summarize on the estate LLM gateway

**Status:** Accepted
**Date:** 2026-08-25
**Supersedes:** parts of [ADR 0001](0001-initial-architecture.md) — the "Claude API
with categorized system prompt" summarization decision and its stated consequence that
"Claude API dependency for summarization adds cost".

## Context

The watcher authenticated with an Anthropic Console pay-as-you-go API key — a billing
plane entirely separate from a Claude Code subscription. The daily full-docs run fetches
~1,700 pages, diffs them, fans out one `claude-haiku-4-5` call per changed file, then
synthesizes with `claude-sonnet-4-6`. That cost roughly $5/day out of pocket, and the
same fan-out caused a rate-limit incident on 2026-06-08.

The estate already runs an OpenAI-compatible proxy (agentgateway) fronting free local
inference — llama.cpp on a MacBook, scope `llm:local`.

## Decision

Route every LLM call through a single shim (`src/claude_watcher/llm.py`) speaking the
OpenAI chat-completions dialect, pointed at the gateway. Summarize on `local/*` only.

- **One preset for both map and reduce.** `llama-server` autoloads presets on demand and
  each load evicts the others, so splitting the tiers across two local model ids would
  force a full model reload mid-run.
- **Degrade, never escalate.** When the endpoint is unreachable the digest falls back to
  the plain changed-page list. There is no automatic path to a paid provider.
- **Skip the model on prose-free hunks.** A whitespace, same-host link-repoint, or
  anchor-only diff gets a mechanical one-liner and costs no call. The comparison is on
  *ordered* normalized `+`/`-` lines, so a pure reordering still counts as a change;
  anything the filter cannot classify goes to the model. **Link targets are compared by
  host, not erased** — a docs site reshuffling its own paths is noise, but a link that
  starts pointing at a different host is exactly what this audience must not have
  filtered out, and a one-liner saying "formatting/link changes only" would have hidden
  it.
- **Budget the reduce step, not just the map.** The per-file budget bounded nothing on
  its own — a hundred-odd per-file summaries overflow the window by themselves.
- **Deliver in ordered numbered parts.** The 4000-character Discord cut is replaced by
  sequential POSTs, and delivery reports success only when every part landed.

## Consequences

- Out-of-pocket summarization cost goes to zero.
- Summary quality drops: a 27B local model is not Sonnet. The digest's structure is
  unchanged; its prose is weaker.
- Runs are slower and serialized against one llama-server process. The existing
  concurrency cap of 3 remains the right bound for a different reason than it was
  written for.
- On days the M5 is down, the digest is the plain page list. That is the accepted cost.
- **The Anthropic key is not retired.** The gateway's own `anthropic/*` route still
  reads `apps.claude_watcher.anthropic_api_key` from SOPS. This removes *the watcher's*
  spend, not the key.
- Delivery is now stricter: `deliver` returns True only when every configured channel
  took the whole digest. A configured-but-failing email channel will therefore hold back
  the snapshot commit and cause a duplicate Discord post next run. Losing a day's real
  changes is the worse failure.

## Things a future session will re-propose, and why not to

**Prompt caching buys exactly zero here.** The minimum cacheable prefix is ~1024 tokens
and a shorter prefix caches nothing while reporting no error. The per-file system prompt
is ~55 tokens and the synthesis prompt ~200; the variable diff hunk is the entire
payload. It is also Anthropic-shaped and does not survive the OpenAI-compatible gateway,
and llama.cpp's automatic prefix-KV reuse has the same ~55 tokens to work with.

**An `azure/*` fallback when local is down.** Declined. It would make this service the
first static API key in the estate to reach a wallet — a long-lived secret in a rendered
compose env, readable by `docker inspect`, on a container making ~1,700 calls
a day. The fallback would also fire automatically and unattended, precisely when nobody
is reading the access log. If it is ever revisited, a security review of the gateway's
scope-enforcement control set is a precondition, not a follow-up.
