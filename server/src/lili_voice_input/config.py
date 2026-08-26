from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    asr_api_key: str = ""
    asr_base_url: str = "https://openrouter.ai/api/v1"
    asr_model: str = ""
    asr_timeout_seconds: float = Field(default=30.0, gt=0)

    polish_enabled: bool = True
    polish_api_key: str = ""
    polish_base_url: str = "https://api.openai.com/v1"
    polish_model: str = ""
    polish_enable_thinking: bool | None = None
    polish_temperature: float = Field(default=0.1, ge=0, le=2)
    polish_max_tokens: int = Field(default=1500, ge=100)
    polish_timeout_seconds: float = Field(default=15.0, gt=0)
    polish_max_retries: int = Field(default=0, ge=0, le=5)

    server_host: str = "127.0.0.1"
    server_port: int = Field(default=9100, ge=1, le=65535)
    allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    service_token: str = ""
    static_root: Path | None = None

    ffmpeg_binary: str = "ffmpeg"
    ffmpeg_timeout_seconds: float = Field(default=30.0, gt=0)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    stt_segment_target_seconds: int = Field(default=30, ge=1)
    stt_segment_max_seconds: int = Field(default=45, ge=1)
    stt_segment_overlap_seconds: float = Field(default=1.0, ge=0)
    stt_segment_silence_ms: int = Field(default=600, ge=100)
    stt_segment_max_in_flight: int = Field(default=2, ge=1)
    stt_segment_max_retries: int = Field(default=2, ge=0, le=5)
    stt_max_concurrency: int = Field(default=3, ge=1)
    stt_request_timeout_seconds: float = Field(default=30.0, gt=0)
    stt_finalization_timeout_seconds: float = Field(default=120.0, gt=0)
    stt_max_duration_seconds: int = Field(default=600, ge=1, le=3600)
    stt_max_active_sessions: int = Field(default=10, ge=1)

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_segment_settings(self) -> "Settings":
        if self.stt_segment_max_seconds < self.stt_segment_target_seconds:
            raise ValueError("STT_SEGMENT_MAX_SECONDS must be >= STT_SEGMENT_TARGET_SECONDS")
        if self.stt_segment_overlap_seconds >= self.stt_segment_target_seconds:
            raise ValueError("STT_SEGMENT_OVERLAP_SECONDS must be shorter than the target segment")
        return self

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.asr_api_key.strip():
            errors.append("ASR_API_KEY is missing")
        if not self.asr_model.strip():
            errors.append("ASR_MODEL is missing")
        if self.polish_enabled and not self.polish_api_key.strip():
            errors.append("POLISH_API_KEY is missing while POLISH_ENABLED=true")
        if self.polish_enabled and not self.polish_model.strip():
            errors.append("POLISH_MODEL is missing while POLISH_ENABLED=true")
        return errors

    def stream_options(self) -> dict[str, object]:
        return {
            "segment_target_seconds": self.stt_segment_target_seconds,
            "segment_max_seconds": self.stt_segment_max_seconds,
            "segment_overlap_ms": round(self.stt_segment_overlap_seconds * 1000),
            "segment_silence_ms": self.stt_segment_silence_ms,
            "segment_max_in_flight": self.stt_segment_max_in_flight,
            "segment_max_retries": self.stt_segment_max_retries,
            "request_timeout_seconds": self.stt_request_timeout_seconds,
            "finalization_timeout_seconds": self.stt_finalization_timeout_seconds,
            "max_duration_seconds": self.stt_max_duration_seconds,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
