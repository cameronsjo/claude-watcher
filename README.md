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
- Anthropic API key

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
