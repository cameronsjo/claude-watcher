# claude-watcher

Self-hosted documentation watcher for Claude Code. Polls all Claude Code documentation pages, diffs against last known state (stored as git commits), summarizes changes on the estate LLM gateway, and delivers digests to Discord and email.

## Why

Claude Code releases multiple times per week with no RSS feed or structured change notifications. As a plugin/skill developer and security engineer, staying on top of changes to permissions, hooks, plugin schemas, security settings, and new features matters — without manually checking docs every day.

## How It Works

```
Fetch all pages → git diff → LLM summary → Discord + Email → git commit
```

- **Source discovery**: Auto-fetches all pages from each source's `llms.txt` index:
  - **Claude Code docs** — `code.claude.com/docs/llms.txt` → committed flat in `snapshots/`
  - **Anthropic API docs** — `platform.claude.com/llms.txt` (~1,500 pages) → committed under `snapshots/api-docs/`. Set `WATCHER_API_DOCS_BASE_URL=""` to disable this source.
- **State store**: Git repo — snapshots committed after each run, `git log` = history, `git diff HEAD~1` = last changes. Set `WATCHER_GIT_REMOTE_URL` to also push that history to a remote (e.g. Gitea/GitHub) after every commit; leave it unset to keep commits local to the volume
- **Smart scheduling**: Polls based on Anthropic's publishing patterns (peak hours more frequent)
- **Categorized digests**: Security, breaking changes, plugin impact, new features.
  Digests longer than one Discord embed are split into ordered numbered parts —
  nothing is truncated
- **Summarization**: an OpenAI-compatible endpoint (`WATCHER_LLM_BASE_URL`), which in
  the estate is agentgateway fronting free local inference. A per-file diff that
  provably changed no prose — whitespace, a retargeted link, a moved anchor — is
  summarized mechanically and never reaches the model. When the endpoint is
  unreachable the digest degrades to the plain changed-page list; it never falls
  back to a paid provider

### Adding / seeding a source

The first time a source is enabled, every page looks "new" — for the ~1,500-page
API docs index that would mean one ~1,500-file diff and a summarization call per
page on the first scheduled run. Seed the baseline quietly instead:

```bash
make seed   # fetch all sources + commit, no summary, no delivery
```

Run this once when first enabling the API docs source, **before** `make start`.
After seeding, the next `make run` reports "No changes detected" until the docs
actually change, and steady-state runs only diff genuine changes.

## Getting Started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Discord webhook URL
- SMTP credentials for email delivery
- An OpenAI-compatible LLM endpoint and its API key (optional — without a key the
  digest is the plain changed-page list)

### Install

```bash
make dev
```

### Configure

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

### Run

```bash
# Single check cycle
make run

# Start the scheduler
make start
```

### Docker

```bash
make docker-build
make docker-run
```

### LLM endpoint

The summarizer speaks the OpenAI chat-completions dialect to whatever
`WATCHER_LLM_BASE_URL` points at, so it can be routed through any compatible
gateway or proxy (cost attribution, central rate limits, one credential to
rotate, provider failover) without a code change. Unset (not empty), it uses the
default below.

| Variable | Default | Description |
|---|---|---|
| `WATCHER_LLM_BASE_URL` | `http://agentgateway:8082/v1` | Base URL of the OpenAI-compatible endpoint |
| `WATCHER_LLM_API_KEY` | _(empty)_ | Key for that endpoint; empty disables summarization |
| `WATCHER_LLM_MAP_MODEL` / `WATCHER_LLM_REDUCE_MODEL` | `local/Qwen3.8-27B-Heretic-RVN-Q4_K_M` | Model ids, as the endpoint names them |

`ANTHROPIC_BASE_URL` is deliberately **not** read. It names an Anthropic
Messages-API endpoint, which this client does not speak, and it is often
exported globally for other tools — honoring it would silently reroute the
watcher.

To use Anthropic's own OpenAI-compatible endpoint, change the model ids as well
as the URL — the default ids name a local preset Anthropic does not serve, so
leaving them in place makes every call 404 and the digest silently degrades to
the plain changed-page list:

```bash
WATCHER_LLM_BASE_URL=https://api.anthropic.com/v1/
WATCHER_LLM_API_KEY=sk-ant-...
WATCHER_LLM_MAP_MODEL=claude-sonnet-4-6
WATCHER_LLM_REDUCE_MODEL=claude-sonnet-4-6
```

Pick a model that accepts `temperature` below 1: the summarizer sends a fixed
`temperature=0.2`, and Anthropic's endpoint rejects that with a 400 on Claude
Opus 4.7 and every later model, so `claude-sonnet-4-6` (or
`claude-haiku-4-5`) works where `claude-sonnet-5-5` does not. The endpoint
ignores `reasoning_effort`, so `WATCHER_LLM_REASONING_EFFORT` can stay as is.
Keep `WATCHER_LLM_BASE_URL` unset or set, never empty: an empty value is an
invalid endpoint, not the default. Use `https://` for any endpoint off the
cluster, and keep credentials out of the URL itself.

## Schedule

| Source | Peak (Mon–Fri 10AM–9PM CST) | Off-peak |
|---|---|---|
| CHANGELOG.md | Every 1 hour | Every 4 hours |
| Full docs site | Once daily at midnight CST | Once daily at midnight CST |

## Drift Check

After each full docs run, the watcher can compare changed upstream pages against your local ecosystem files (skills, guides, plugin references) and deliver a separate digest when it detects contradictions or gaps.

### How it works

```text
Changed upstream pages → intersect with drift-mappings.yaml
  → fetch raw ecosystem files
  → the map model checks each pair (does this contradict or omit?)
  → the reduce model synthesizes a prioritized digest
  → Deliver as a separate digest (WRONG / OUTDATED items)
```

### Enable

Set `WATCHER_DRIFT_CHECK_ENABLED=true` in your `.env`. Requires `WATCHER_LLM_API_KEY`.

The `NO DRIFT` sentinel is a string match on the model's reply. It is untested
against a local model, which is one reason the check ships disabled.

The mapping file (`drift-mappings.yaml` at repo root by default) links upstream doc filenames to raw GitHub URLs of your ecosystem files. The filename convention matches the snapshot filenames produced by the fetcher: `docs__en__<page>.md`.

```yaml
docs__en__hooks.md:
  - https://raw.githubusercontent.com/yourorg/yourrepo/main/skills/writing-hooks.md
```

The mapping file is itself drift-prone — review entries whenever upstream docs are restructured.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `WATCHER_DRIFT_CHECK_ENABLED` | `false` | Enable drift checking |
| `WATCHER_DRIFT_MAPPINGS_FILE` | `drift-mappings.yaml` | Path to the mapping file |
| `WATCHER_DRIFT_REVIEW_MODEL` | `WATCHER_LLM_REDUCE_MODEL` | Model for drift synthesis (optional) |

## License

MIT
