import pytest

from lili_voice_input.config import Settings


def test_comma_separated_origins_are_parsed() -> None:
    settings = Settings(ALLOWED_ORIGINS="https://a.example, https://b.example/")
    assert settings.allowed_origins == ["https://a.example", "https://b.example"]


def test_comma_separated_origins_are_parsed_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example/")

    settings = Settings(_env_file=None)

    assert settings.allowed_origins == ["https://a.example", "https://b.example"]


def test_polish_thinking_setting_is_optional_and_parses_false() -> None:
    assert Settings(POLISH_ENABLE_THINKING=None).polish_enable_thinking is None
    assert Settings(POLISH_ENABLE_THINKING="false").polish_enable_thinking is False


def test_readiness_requires_enabled_provider_configuration() -> None:
    settings = Settings(ASR_API_KEY="", ASR_MODEL="", POLISH_ENABLED=True, POLISH_API_KEY="", POLISH_MODEL="")
    errors = settings.readiness_errors()
    assert len(errors) == 4


def test_invalid_segment_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(STT_SEGMENT_TARGET_SECONDS=30, STT_SEGMENT_MAX_SECONDS=20)
