from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


PolishStatus = Literal["applied", "disabled", "fallback"]
PolishReason = Literal[
    "empty_input",
    "configuration_error",
    "rate_limited",
    "timeout",
    "network_error",
    "provider_error",
    "invalid_output",
    "empty_output",
]
DegradedStage = Literal["asr", "polish"]


class TranscriptionResponse(BaseModel):
    type: Literal["final"] = "final"
    text: str
    polished: bool
    polish_status: PolishStatus
    polish_reason: PolishReason | None = None
    degraded: bool
    degraded_stage: DegradedStage | None = None
    segment_count: int = Field(ge=0)
    failed_segment_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    polish_latency_ms: int = Field(ge=0)
    total_latency_ms: int = Field(ge=0)


class ErrorResponse(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "not_ready"]
    version: str
    errors: list[str] = Field(default_factory=list)
