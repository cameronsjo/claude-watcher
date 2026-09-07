# claude-watcher

Self-hosted documentation watcher for Claude Code. Polls all Claude Code documentation pages, diffs against last known state (stored as git commits), summarizes changes via Claude API, and delivers digests to Discord and email.

## Stack

- **Language**: Python 3.13
- **Package manager**: uv
- **Linting/Formatting**: Ruff
- **Testing**: pytest + pytest-asyncio + pytest-httpx
- **Issue tracking**: GitHub issues

## Commands

```bash
make dev      # Install dependencies
make check    # Run linting and format checks
make fix      # Auto-fix linting and formatting
make test     # Run test suite
make run      # Single fetch+diff+digest cycle
make start    # Start the scheduler
```

## Project Structure

```
src/claude_watcher/
├── __init__.py
├── main.py           # Entry point, scheduler setup
├── fetcher.py        # Async page fetching from llms.txt + raw GitHub
├── differ.py         # Git diff operations, detect new/removed pages
├── summarizer.py     # Claude API digest generation
├── delivery.py       # Discord webhook + email sending
└── config.py         # Pydantic Settings, env var config
snapshots/            # Git-tracked fetched pages (the state store)
tests/                # Test suite
```

## Conventions

- All code uses type annotations
- Async/await for I/O operations
- Structured JSON logging via structlog
- Config via environment variables (Pydantic Settings)
- Conventional Commits: `type(scope): description`

## Issue Tracking

Issues live on GitHub, not in this repo. `bd` (Beads) was retired per
cadence-groundwork#138 — `.beads/` is dormant historical data, not a work
queue; don't delete it.

## Session Completion

Use `cadence:outro` to close out a work session.
