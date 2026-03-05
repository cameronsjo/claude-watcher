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

    # Source URLs
    docs_base_url: str = "https://code.claude.com/docs"
    changelog_url: str = (
        "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
    )

    # Local state
    snapshots_dir: Path = Path("snapshots")

    # Logging
    log_level: str = "INFO"

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and len(self.email_to) > 0)

    @property
    def summarizer_enabled(self) -> bool:
        return bool(self.anthropic_api_key)
