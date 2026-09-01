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


def test_stage_specific_mocks_override_legacy_mock_switch() -> None:
    settings = Settings(
        MOCK_PROVIDERS_ENABLED=False,
        MOCK_ASR_ENABLED=True,
        MOCK_POLISH_ENABLED=False,
        ASR_API_KEY="",
        ASR_MODEL="",
        POLISH_ENABLED=True,
        POLISH_API_KEY="",
        POLISH_MODEL="",
    )

    assert settings.use_mock_asr is True
    assert settings.use_mock_polish is False
    assert settings.readiness_errors() == [
        "POLISH_API_KEY is missing while POLISH_ENABLED=true",
        "POLISH_MODEL is missing while POLISH_ENABLED=true",
    ]


def test_legacy_mock_switch_still_controls_both_stages() -> None:
    settings = Settings(MOCK_PROVIDERS_ENABLED=True, ASR_API_KEY="", ASR_MODEL="")

    assert settings.use_mock_asr is True
    assert settings.use_mock_polish is True
    assert settings.readiness_errors() == []


def test_invalid_segment_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(STT_SEGMENT_TARGET_SECONDS=30, STT_SEGMENT_MAX_SECONDS=20)
