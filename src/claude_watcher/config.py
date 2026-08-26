"""Configuration via environment variables using Pydantic Settings."""

from pathlib import Path

from pydantic import SecretStr, field_validator
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

    # LLM gateway — OpenAI-compatible endpoint fronting free local inference.
    # In-cluster address: keeps the call off Traefik and off the LAN.
    llm_base_url: str = "http://agentgateway:8082/v1"
    # SecretStr so `repr(settings)` and any future `logger.info(..., settings=...)`
    # render `**********` instead of the credential.
    llm_api_key: SecretStr = SecretStr("")
    # Map and reduce deliberately share one preset: llama-server autoloads
    # presets on demand and each load evicts the others, so splitting the two
    # tiers across two local ids would force a full model reload mid-run.
    llm_map_model: str = "local/Qwen3.8-27B-Heretic-RVN-Q4_K_M"
    llm_reduce_model: str = "local/Qwen3.8-27B-Heretic-RVN-Q4_K_M"

    # Summarizer fan-out throttling — keeps the per-file map step under the
    # single-process llama-server's capacity and resilient to a single bad file
    # (see issue #4).
    # Semaphore bound on in-flight fan-out calls (summarizer + drift map step).
    summarizer_max_concurrency: int = 3
    # Passed to AsyncOpenAI(max_retries=...); the SDK does exponential backoff
    # and honors `retry-after`.
    summarizer_max_retries: int = 5
    # Per-run cap on files summarized; 0 = unlimited. Excess files are deferred.
    summarizer_max_files: int = 0
    # Input budgets, sized against the measured local preset: ctx-size 262144
    # tokens (~1.05M chars at ~4 chars/tok), n-predict 32768. Both budgets sit
    # far under that — a single-process llama-server on a laptop pays for every
    # prefilled token in wall-clock, so the window is not the binding limit.
    # Per-file map input (~30k tokens).
    summarizer_max_input_chars: int = 120_000
    # Assembled reduce input (~50k tokens). Without this the map budget bounds
    # nothing: 130 per-file summaries at 512 tokens each overflow on their own.
    summarizer_max_reduce_chars: int = 200_000

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
    # Optional: override the reduce model for drift review (falls back to
    # llm_reduce_model)
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
        return bool(self.llm_api_key.get_secret_value())

    @property
    def drift_check_active(self) -> bool:
        """Effective gate: toggle on + API key present + mapping file non-empty."""
        if not self.drift_check_enabled:
            return False
        if not self.llm_api_key.get_secret_value():
            return False
        # Stat atomically (no exists()-then-stat() TOCTOU): a missing or
        # unreadable mapping file raises OSError -> gate closed.
        try:
            if self.drift_mappings_file.stat().st_size == 0:
                return False
        except OSError:
            return False
        return True
