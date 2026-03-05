FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/cameronsjo/claude-watcher" \
      org.opencontainers.image.description="Self-hosted documentation watcher for Claude Code" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home watcher

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN chown -R watcher:watcher /app

USER watcher

# Git identity for snapshot commits (differ.py auto-inits the repo at runtime)
RUN git config --global user.name "claude-watcher" \
    && git config --global user.email "claude-watcher@localhost" \
    && git config --global init.defaultBranch main

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "print('ok')" || exit 1

CMD ["uv", "run", "python", "-m", "claude_watcher.main"]
