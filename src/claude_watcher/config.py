"""Configuration via environment variables using Pydantic Settings."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration loaded from environment variables prefixed with WATCHER_."""

    model_config = {"env_prefix": "WATCHER_"}

    # CHANGELOG polling schedule
    changelog_peak_interval_hours: int = 1
    changelog_offpeak_interval_hours: int = 4

    # Full docs site schedule (midnight CST = 06:00 UTC)
    docs_check_hour_utc: int = 6

    # Delivery: Discord
    discord_webhook_url: str = ""

    # Delivery: Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_to: list[str] = []
    email_from: str = ""

    @field_validator("email_to", mode="before")
    @classmethod
    def split_email_to(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [addr.strip() for addr in v.split(",") if addr.strip()]
        return v

    # Claude API
    anthropic_api_key: str = ""

    # Summarizer fan-out throttling — keeps the per-file Haiku map step under the
    # org rate limit and resilient to a single bad file (see issue #4).
    # Semaphore bound on in-flight fan-out calls (summarizer + drift map step).
    summarizer_max_concurrency: int = 3
    # Passed to AsyncAnthropic(max_retries=...); the SDK does exponential backoff
    # and honors `retry-after` on 429/529.
    summarizer_max_retries: int = 5
    # Per-run cap on files summarized; 0 = unlimited. Excess files are deferred.
    summarizer_max_files: int = 0
    # Per-file input truncation budget (~120k tokens at ~4 chars/tok — headroom
    # under the 200k Haiku context window).
    summarizer_max_input_chars: int = 480_000

    # Source URLs
    docs_base_url: str = "https://code.claude.com/docs"  # Claude Code docs
    # Anthropic API docs — index lives at the domain root (/llms.txt); page URLs
    # in it are absolute (.../docs/en/<path>.md), so base is the root host.
    api_docs_base_url: str = "https://platform.claude.com"
    changelog_url: str = (
        "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
    )

    # Local state
    snapshots_dir: Path = Path("snapshots")

    # Git remote — push snapshots to a remote repo (e.g., Gitea) after each commit
    git_remote_url: str = ""

    # Logging
    log_level: str = "INFO"

    # Drift check — detects when upstream docs contradict ecosystem files
    drift_check_enabled: bool = False
    drift_mappings_file: Path = Path("drift-mappings.yaml")
    # Optional: override the reduce model for drift review (falls back to Sonnet)
    drift_review_model: str = ""

    @property
    def api_docs_enabled(self) -> bool:
        """Whether the Anthropic API docs source is tracked. Empty URL disables."""
        return bool(self.api_docs_base_url)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and len(self.email_to) > 0)

    @property
    def git_remote_enabled(self) -> bool:
        return bool(self.git_remote_url)

    @property
    def summarizer_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def drift_check_active(self) -> bool:
        """Effective gate: toggle on + API key present + mapping file non-empty."""
        if not self.drift_check_enabled:
            return False
        if not self.anthropic_api_key:
            return False
        # Stat atomically (no exists()-then-stat() TOCTOU): a missing or
        # unreadable mapping file raises OSError -> gate closed.
        try:
            if self.drift_mappings_file.stat().st_size == 0:
                return False
        except OSError:
            return False
        return True
