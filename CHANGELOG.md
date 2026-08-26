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

- `WATCHER_SUMMARIZER_MAX_REDUCE_CHARS` — budget for the assembled synthesis
  input. The per-file budget bounded nothing on its own.
- Per-file diffs that provably changed no prose (whitespace, retargeted links,
  moved anchors) are summarized mechanically and make no model call.

### Fixed

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
