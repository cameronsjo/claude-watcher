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


def test_email_enabled() -> None:
    settings = Settings(
        smtp_host="smtp.test.com",
        email_to=["test@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is True


def test_email_enabled_multiple_recipients() -> None:
    settings = Settings(
        smtp_host="smtp.test.com",
        email_to=["a@test.com", "b@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is True
    assert len(settings.email_to) == 2


def test_email_disabled_when_no_host() -> None:
    settings = Settings(
        smtp_host="",
        email_to=["test@test.com"],
        _env_file=None,  # type: ignore[call-arg]
    )
    assert settings.email_enabled is False
