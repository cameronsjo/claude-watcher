# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Summarization now runs on an OpenAI-compatible LLM gateway
  (`WATCHER_LLM_BASE_URL` / `WATCHER_LLM_API_KEY`) instead of the Anthropic
  Console API. All five call sites route through a new `claude_watcher.llm`
  shim. See [ADR 0002](docs/adr/0002-summarize-on-the-estate-gateway.md).
- **Breaking:** `WATCHER_ANTHROPIC_API_KEY` is removed. Set `WATCHER_LLM_API_KEY`
  instead — it gates the summarizer *and* the drift check.
- `WATCHER_DRIFT_REVIEW_MODEL` now defaults to `WATCHER_LLM_REDUCE_MODEL`.
- `WATCHER_SUMMARIZER_MAX_INPUT_CHARS` lowered to `120000`, sized against the
  local preset's measured context window rather than a 200k one.
- Discord digests longer than one embed are delivered as ordered numbered parts
  instead of being truncated at 4000 characters.
- `deliver` now reports success only when every configured channel took the
  whole digest. A partial Discord post no longer commits the snapshot.

### Added

- `WATCHER_LLM_REASONING_EFFORT` (default `none`) — sent as `reasoning_effort`.
  Summarizing a diff is extraction, not deduction, and thinking tokens come out
  of the same budget as the answer. Set to `""` for a backend that rejects it.
- `WATCHER_LLM_MAP_MAX_TOKENS` (1024), `WATCHER_LLM_REDUCE_MAX_TOKENS` (4096),
  and `WATCHER_LLM_CHANGELOG_MAX_TOKENS` (2048) replace the hardcoded values.
- `WATCHER_SUMMARIZER_MAX_REDUCE_CHARS` — budget for the assembled synthesis
  input. The per-file budget bounded nothing on its own.
- Per-file diffs that provably changed no prose (whitespace, same-host link
  repointing, moved anchors) are summarized mechanically and make no model
  call. A link repointed to a *different* host still reaches the model.

### Fixed

- The synthesis prompts' character target now derives from the token budget
  instead of being hardcoded. The doc prompt asked for "under 3500 characters"
  against a 1024-token cap that could not emit that much — the instruction and
  the cap disagreed, and the cap won, mid-sentence. The changelog prompt had no
  target at all, so `max_tokens` was its only bound. Both are now rendered from
  the live budget at the one place that holds both.
- Digests were ending mid-sentence, and sometimes arriving as a `---` above
  nothing. The reduce steps carried `max_tokens` of 1024 and 512, sized against
  a provider that did not emit reasoning tokens; the local model bills its
  thinking against the same budget but returns it in a separate
  `reasoning_content` field. Reasoning is now disabled for these calls and the
  budgets are far larger, so truncation is much less likely — but a
  long-enough response can still hit the cap. What changed categorically is
  that it no longer passes silently: `finish_reason == "length"` is logged, and
  an empty completion raises instead of shipping a blank section.
- `finish_reason == "length"` is now logged. It is the only signal that a
  response was truncated; the HTTP call is a well-formed 200 either way.
- An empty completion raises `EmptyCompletionError` instead of returning `""`.
- The doc and changelog syntheses degrade **independently**. One shared
  `try`/`except` meant a failed doc synthesis also discarded a changelog digest
  that had already been produced. Only a total failure falls back to the page
  list now.

- An empty digest is now a delivery failure. It previously reported success and
  committed the snapshot, consuming the day's changes for a digest nobody got.
- Discord delivery no longer logs the exception string on an HTTP error. `httpx`
  renders the full request URL in that message, and the webhook URL is itself a
  credential.
- The triviality filter no longer classifies a link repointed to a *different
  host* as trivial, and no longer erases arbitrary text written inside `](...)`
  — an unanchored pattern matched a bare `](` with no `[` before it, so prose
  could be laundered through the parens and suppressed.
- `WATCHER_LLM_API_KEY` is held as a `SecretStr`, so `repr(Settings)` cannot
  render it; the client cache keys on a digest of the key rather than the key.
- `fit_sections` clamps an oversized `prefix`. The page list is built from the
  diff and is not bounded by the caller, so a large first run could defeat the
  reduce budget even after every section was dropped.
- The HTML email body escapes the summary and the raw diff.
- Email delivery logs a recipient count instead of the address list.

