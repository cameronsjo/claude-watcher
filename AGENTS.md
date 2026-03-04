# claude-watcher

Self-hosted documentation watcher for Claude Code. Polls all Claude Code documentation pages, diffs against last known state (stored as git commits), summarizes changes via Claude API, and delivers digests to Discord and email.

## Stack

- **Language**: Python 3.13
- **Package manager**: uv
- **Linting/Formatting**: Ruff
- **Testing**: pytest + pytest-asyncio + pytest-httpx
- **Issue tracking**: Beads (`bd`)

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

## Beads Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
