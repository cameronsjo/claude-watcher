# Contributing to claude-watcher

Thank you for considering contributing to claude-watcher.

## Code of Conduct

Be respectful, constructive, and inclusive.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/<you>/claude-watcher.git`
3. Install dependencies: `make dev`
4. Create a branch: `git checkout -b feat/your-feature`

## Development Setup

```bash
make dev    # Install dependencies
make check  # Run linting and format checks
make fix    # Auto-fix linting and formatting
make test   # Run test suite
```

## Commit Format

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Ensure `make check` passes
- Write a clear description of what changed and why
- Reference related issues: `Closes #123`
