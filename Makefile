.DEFAULT_GOAL := help

## Development
.PHONY: dev
# Install dependencies
dev:
	uv sync

.PHONY: run
# Run a single fetch+diff+digest cycle
run:
	uv run python -m claude_watcher.main --once

.PHONY: start
# Start the scheduler
start:
	uv run python -m claude_watcher.main

## Quality
.PHONY: check
# Run linting and format checks
check:
	uv run ruff check . && uv run ruff format --check .

.PHONY: fix
# Auto-fix linting and formatting issues
fix:
	uv run ruff check --fix . && uv run ruff format .

.PHONY: test
# Run test suite
test:
	uv run pytest

## Docker
.PHONY: docker-build
# Build Docker image
docker-build:
	docker build -t claude-watcher .

.PHONY: docker-run
# Run Docker container
docker-run:
	docker run --env-file .env claude-watcher

## Help
.PHONY: help
# Show available targets
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?# "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
