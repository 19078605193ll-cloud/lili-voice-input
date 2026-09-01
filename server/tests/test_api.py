import time
from array import array

from fastapi.testclient import TestClient

from lili_voice_input.audio.pcm import SAMPLE_RATE, pcm16_to_wav
from lili_voice_input.main import app
from lili_voice_input.providers.openai_polisher import PolishProviderError
from lili_voice_input.services.admission import AdmissionRejected, LocalAdmissionController
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.streaming import FinalResult
from lili_voice_input.services.transcription import TranscriptionService


class FakeAsr:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio: bytes, *, audio_format: str, language: str | None = None) -> str:
        self.calls += 1
        return "修改点 ENV 后等待八秒"

    async def close(self) -> None:
        return None


class FakeUploadService:
    def __init__(self, result: FinalResult | None = None) -> None:
        self.result = result or FinalResult(
            text="最终文本",
            polished=False,
            polish_status="disabled",
            polish_reason=None,
            degraded=False,
            degraded_stage=None,
            latency_ms=10,
            polish_latency_ms=0,
            total_latency_ms=10,
            segment_count=1,
            failed_segment_count=0,
        )

    async def transcribe_upload(
        self,
        content: bytes,
        *,
        language: str | None = "zh",
        admission_wait_ms: int = 0,
    ) -> FinalResult:
        return self.result


class FakePolisher:
    def __init__(self, output: str | Exception) -> None:
        self.output = output

    async def polish(self, transcript: str) -> str:
        if isinstance(self.output, Exception):
            raise self.output
        return self.output

    async def close(self) -> None:
        return None


class PassthroughWavConverter:
    async def convert_to_wav(self, content: bytes) -> bytes:
        return content


class RejectAdmission:
    async def acquire(self, subject: str, transport: str) -> None:
        raise AdmissionRejected("queue_timeout", 5000)


def tone(milliseconds: int = 200) -> bytes:
    return array("h", [5000] * (SAMPLE_RATE * milliseconds // 1000)).tobytes()


def test_live_health_and_http_response_contract() -> None:
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "ok"
        assert "voice_active_sessions" in client.get("/metrics").text
        client.app.state.transcription_service = FakeUploadService()
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("recording.wav", b"audio", "audio/wav")},
            data={"language": "zh"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "type": "final",
            "text": "最终文本",
            "polished": False,
            "polish_status": "disabled",
            "polish_reason": None,
            "degraded": False,
            "degraded_stage": None,
            "segment_count": 1,
            "failed_segment_count": 0,
            "latency_ms": 10,
            "polish_latency_ms": 0,
            "total_latency_ms": 10,
            "admission_wait_ms": 0,
            "asr_queue_wait_ms": 0,
        }
        assert "polish_reason_codes" not in response.json()
        assert response.json()["degraded_stage"] != "polish_partial"


def test_http_response_exposes_polish_fallback_reason() -> None:
    result = FinalResult(
        text="原始转写",
        polished=False,
        polish_status="fallback",
        polish_reason="timeout",
        degraded=True,
        degraded_stage="polish",
        latency_ms=10,
        polish_latency_ms=15,
        total_latency_ms=25,
        segment_count=1,
        failed_segment_count=0,
    )
    with TestClient(app) as client:
        client.app.state.transcription_service = FakeUploadService(result)
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("recording.wav", b"audio", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["polish_status"] == "fallback"
    assert response.json()["polish_reason"] == "timeout"
    assert response.json()["degraded_stage"] == "polish"


def test_http_silence_returns_empty_audio_without_calling_asr() -> None:
    asr = FakeAsr()
    with TestClient(app) as client:
        client.app.state.transcription_service = TranscriptionService(
            client.app.state.settings,
            PassthroughWavConverter(),
            asr,
            PolishingService(None, enabled=False),
        )
        response = client.post(
            "/v1/transcriptions",
            files={
                "file": (
                    "recording.wav",
                    pcm16_to_wav(array("h", [0] * (SAMPLE_RATE // 2)).tobytes()),
                    "audio/wav",
                )
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "type": "error",
        "code": "EMPTY_AUDIO",
        "message": "没有检测到语音，或说话时间太短，请重试",
        "recoverable": False,
    }
    assert asr.calls == 0


def test_websocket_ready_then_final_without_partial() -> None:
    with TestClient(app) as client:
        client.app.state.asr_provider = FakeAsr()
        client.app.state.polishing_service = PolishingService(None, enabled=False)
        with client.websocket_connect("/v1/transcriptions/stream") as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "protocol_version": "1",
                    "format": "pcm16",
                    "sample_rate": 16000,
                    "language": "zh",
                }
            )
            ready = websocket.receive_json()
            assert ready["type"] == "ready"
            assert ready["capabilities"]["partial"] is False
            websocket.send_bytes(tone())
            websocket.send_json({"type": "commit"})
            final = websocket.receive_json()
            assert final["type"] == "final"
            assert final["text"] == "修改点 ENV 后等待八秒"
            assert final["polish_status"] == "disabled"
            assert final["polish_reason"] is None
            assert "polish_reason_codes" not in final


def test_websocket_silence_returns_empty_audio_without_calling_asr() -> None:
    asr = FakeAsr()
    with TestClient(app) as client:
        client.app.state.asr_provider = asr
        client.app.state.polishing_service = PolishingService(None, enabled=False)
        with client.websocket_connect("/v1/transcriptions/stream") as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "protocol_version": "1",
                    "format": "pcm16",
                    "sample_rate": 16000,
                    "language": "zh",
                }
            )
            websocket.receive_json()
            websocket.send_bytes(array("h", [0] * (SAMPLE_RATE // 2)).tobytes())
            websocket.send_json({"type": "commit"})
            error = websocket.receive_json()

    assert error == {
        "type": "error",
        "code": "EMPTY_AUDIO",
        "message": "没有检测到语音，或说话时间太短，请重试",
        "recoverable": False,
    }
    assert asr.calls == 0


def test_websocket_returns_polished_plain_text() -> None:
    with TestClient(app) as client:
        client.app.state.asr_provider = FakeAsr()
        client.app.state.polishing_service = PolishingService(FakePolisher("修改 .env 后等待 8 秒。"), enabled=True)
        with client.websocket_connect("/v1/transcriptions/stream") as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "protocol_version": "1",
                    "format": "pcm16",
                    "sample_rate": 16000,
                    "language": "zh",
                }
            )
            websocket.receive_json()
            websocket.send_bytes(tone())
            websocket.send_json({"type": "commit"})
            final = websocket.receive_json()

    assert final["text"] == "修改 .env 后等待 8 秒。"
    assert final["polish_status"] == "applied"
    assert final["polish_reason"] is None
    assert final["degraded_stage"] is None


def test_websocket_polish_failure_returns_original_asr() -> None:
    with TestClient(app) as client:
        client.app.state.asr_provider = FakeAsr()
        failure = PolishProviderError("rate_limited")
        client.app.state.polishing_service = PolishingService(FakePolisher(failure), enabled=True)
        with client.websocket_connect("/v1/transcriptions/stream") as websocket:
            websocket.send_json(
                {
                    "type": "start",
                    "protocol_version": "1",
                    "format": "pcm16",
                    "sample_rate": 16000,
                    "language": "zh",
                }
            )
            websocket.receive_json()
            websocket.send_bytes(tone())
            websocket.send_json({"type": "commit"})
            final = websocket.receive_json()

    assert final["text"] == "修改点 ENV 后等待八秒"
    assert final["polish_status"] == "fallback"
    assert final["polish_reason"] == "rate_limited"
    assert final["degraded"] is True
    assert final["degraded_stage"] == "polish"


def test_http_service_token_is_enforced() -> None:
    with TestClient(app) as client:
        original = client.app.state.settings.service_token
        client.app.state.settings.service_token = "test-secret"
        client.app.state.transcription_service = FakeUploadService()
        try:
            unauthorized = client.post(
                "/v1/transcriptions",
                files={"file": ("recording.wav", b"audio", "audio/wav")},
            )
            authorized = client.post(
                "/v1/transcriptions",
                headers={"Authorization": "Bearer test-secret"},
                files={"file": ("recording.wav", b"audio", "audio/wav")},
            )
        finally:
            client.app.state.settings.service_token = original
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {
            "type": "error",
            "code": "UNAUTHORIZED",
            "message": "访问令牌无效",
            "recoverable": False,
        }
        assert authorized.status_code == 200


def test_websocket_start_timeout_does_not_consume_capacity() -> None:
    with TestClient(app) as client:
        settings = client.app.state.settings
        original_timeout = settings.stt_start_timeout_seconds
        settings.stt_start_timeout_seconds = 0.02
        try:
            with client.websocket_connect("/v1/transcriptions/stream") as websocket:
                error = websocket.receive_json()
                assert error["code"] == "START_TIMEOUT"
            assert client.app.state.admission.active == 0
        finally:
            settings.stt_start_timeout_seconds = original_timeout


def test_websocket_invalid_auth_does_not_consume_capacity() -> None:
    with TestClient(app) as client:
        settings = client.app.state.settings
        original_anonymous = settings.anonymous_tokens_enabled
        settings.anonymous_tokens_enabled = True
        try:
            with client.websocket_connect("/v1/transcriptions/stream") as websocket:
                websocket.send_json(
                    {
                        "type": "start",
                        "protocol_version": "1",
                        "format": "pcm16",
                        "sample_rate": 16000,
                        "auth_token": "invalid",
                    }
                )
                error = websocket.receive_json()
                assert error["code"] == "UNAUTHORIZED"
            assert client.app.state.admission.active == 0
        finally:
            settings.anonymous_tokens_enabled = original_anonymous


def test_websocket_queue_event_timeout_and_release() -> None:
    with TestClient(app) as client:
        client.app.state.admission = LocalAdmissionController(1, 1, 0.03, 2)
        with client.websocket_connect("/v1/transcriptions/stream") as first:
            first.send_json(
                {
                    "type": "start",
                    "protocol_version": "1",
                    "format": "pcm16",
                    "sample_rate": 16000,
                }
            )
            assert first.receive_json()["type"] == "ready"
            with client.websocket_connect("/v1/transcriptions/stream") as second:
                second.send_json(
                    {
                        "type": "start",
                        "protocol_version": "1",
                        "format": "pcm16",
                        "sample_rate": 16000,
                    }
                )
                queued = second.receive_json()
                error = second.receive_json()
                assert queued["type"] == "queued"
                assert queued["position"] == 1
                assert error["code"] == "QUEUE_TIMEOUT"
                assert error["retry_after_ms"] == 30
            assert client.app.state.admission.active == 1
            first.send_json({"type": "cancel"})
        for _ in range(20):
            if client.app.state.admission.active == 0:
                break
            time.sleep(0.005)
        assert client.app.state.admission.active == 0


def test_internal_drain_requires_service_token_and_updates_readiness() -> None:
    with TestClient(app) as client:
        settings = client.app.state.settings
        original = settings.service_token
        settings.service_token = "drain-secret"
        try:
            assert client.post("/internal/drain").status_code == 401
            response = client.post("/internal/drain", headers={"Authorization": "Bearer drain-secret"})
            assert response.status_code == 200
            assert response.json()["status"] == "draining"
            assert client.get("/health/ready").status_code == 503
        finally:
            settings.service_token = original


def test_http_capacity_error_uses_stable_retry_contract() -> None:
    with TestClient(app) as client:
        client.app.state.admission = RejectAdmission()
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("recording.wav", b"audio", "audio/wav")},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.json()["code"] == "QUEUE_TIMEOUT"
    assert response.json()["retry_after_ms"] == 5000
    assert response.json()["recoverable"] is True
