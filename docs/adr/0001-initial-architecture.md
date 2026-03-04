# ADR 0001: Initial Architecture

## Status

Accepted

## Context

Claude Code releases multiple times per week with no structured change notifications. We need a way to monitor documentation changes and deliver categorized digests.

## Decision

- **State store**: Git repo itself — snapshots committed after each run, no database
- **Source discovery**: Auto-fetch page list from `code.claude.com/docs/llms.txt`
- **Scheduling**: APScheduler in-process with two tiers (changelog hourly during peak, full docs daily)
- **Summarization**: Claude API with categorized system prompt
- **Delivery**: Discord webhook + SMTP email, independent failure domains
- **Container**: Docker, non-root, health endpoint

## Consequences

- Git as state store means no database dependencies but requires git in the container image
- Two-tier polling balances freshness against being respectful of doc site resources
- Claude API dependency for summarization adds cost but provides high-quality categorized digests
