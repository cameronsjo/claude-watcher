"""Tests for config module."""

from claude_watcher.config import Settings


def test_defaults() -> None:
    """Settings loads with defaults when no env vars set."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.changelog_peak_interval_hours == 1
    assert settings.changelog_offpeak_interval_hours == 4
    assert settings.docs_check_hour_utc == 6
    assert settings.snapshots_dir.name == "snapshots"


def test_discord_enabled() -> None:
    settings = Settings(
        discord_webhook_url="https://discord.com/api/webhooks/test",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.discord_enabled is True


def test_discord_disabled_when_empty() -> None:
    settings = Settings(
        discord_webhook_url="",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.discord_enabled is False


def test_api_docs_enabled_by_default() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.api_docs_base_url == "https://platform.claude.com"
    assert settings.api_docs_enabled is True


def test_api_docs_disabled_when_empty() -> None:
    settings = Settings(
        api_docs_base_url="",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.api_docs_enabled is False


def test_email_enabled() -> None:
    settings = Settings(
        smtp_host="smtp.test.com",
        email_to=["test@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is True


def test_email_to_comma_separated() -> None:
    settings = Settings(
        smtp_host="smtp.test.com",
        email_to="a@test.com, b@test.com",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is True
    assert settings.email_to == ["a@test.com", "b@test.com"]


def test_email_to_list_passthrough() -> None:
    settings = Settings(
        smtp_host="smtp.test.com",
        email_to=["a@test.com", "b@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_to == ["a@test.com", "b@test.com"]


def test_email_disabled_when_no_host() -> None:
    settings = Settings(
        smtp_host="",
        email_to=["test@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is False


def test_summarizer_throttle_defaults() -> None:
    """The fan-out throttle knobs carry safe defaults."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.summarizer_max_concurrency == 3
    assert settings.summarizer_max_retries == 5
    assert settings.summarizer_max_files == 0
    # Sized against the measured local preset (ctx-size 262144 tokens), not
    # the retired provider's 200k window.
    assert settings.summarizer_max_input_chars == 120_000
    assert settings.summarizer_max_reduce_chars == 200_000


def test_llm_gateway_defaults() -> None:
    """The gateway endpoint defaults to the in-cluster address, key unset."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.llm_base_url == "http://agentgateway:8082/v1"
    assert settings.llm_api_key.get_secret_value() == ""
    assert settings.summarizer_enabled is False
    # Map and reduce share one preset: a preset switch evicts the loaded model.
    assert settings.llm_map_model == settings.llm_reduce_model
    assert settings.llm_map_model.startswith("local/")


def test_summarizer_enabled_reads_the_gateway_key() -> None:
    settings = Settings(
        llm_api_key="gw-test-key",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.summarizer_enabled is True


def test_anthropic_api_key_is_gone() -> None:
    """The retired field must not be re-addable without a code change."""
    assert "anthropic_api_key" not in Settings.model_fields
