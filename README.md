# claude-watcher

Self-hosted documentation watcher for Claude Code. Polls all Claude Code documentation pages, diffs against last known state (stored as git commits), summarizes changes via Claude API, and delivers digests to Discord and email.

## Why

Claude Code releases multiple times per week with no RSS feed or structured change notifications. As a plugin/skill developer and security engineer, staying on top of changes to permissions, hooks, plugin schemas, security settings, and new features matters — without manually checking docs every day.

## How It Works

```
Fetch all pages → git diff → Claude summary → Discord + Email → git commit
```

- **Source discovery**: Auto-fetches all pages from `code.claude.com/docs/llms.txt`
- **State store**: Git repo — snapshots committed after each run, `git log` = history, `git diff HEAD~1` = last changes
- **Smart scheduling**: Polls based on Anthropic's publishing patterns (peak hours more frequent)
- **Categorized digests**: Security, breaking changes, plugin impact, new features

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

## License

MIT
