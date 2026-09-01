import base64
import json

import httpx
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.providers.dashscope_asr import DashScopeAsrProvider, extract_transcript
from lili_voice_input.providers.openrouter_asr import AsrProviderError


@pytest.mark.asyncio
async def test_dashscope_provider_submits_polls_and_fetches_transcript() -> None:
    captured: dict[str, object] = {}
    poll_count = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST":
            captured["submit_path"] = request.url.path
            captured["authorization"] = request.headers.get("authorization")
            captured["async_header"] = request.headers.get("x-dashscope-async")
            captured["body"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "request_id": "dashscope-submit-1",
                    "output": {"task_id": "task-1", "task_status": "PENDING"},
                },
            )
        captured["poll_path"] = request.url.path
        poll_count += 1
        status = "RUNNING" if poll_count == 1 else "SUCCEEDED"
        output: dict[str, object] = {"task_id": "task-1", "task_status": status}
        if status == "SUCCEEDED":
            output["transcription_url"] = "https://results.example/transcript.json?signature=secret"
        return httpx.Response(200, json={"request_id": f"dashscope-poll-{poll_count}", "output": output})

    def result_handler(request: httpx.Request) -> httpx.Response:
        captured["result_authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {"channel_id": 0, "text": "第一句。"},
                    {"channel_id": 1, "text": "第二句。"},
                ]
            },
        )

    settings = Settings(
        ASR_PROVIDER="dashscope",
        ASR_API_KEY="test-secret",
        ASR_BASE_URL="https://dashscope.example/api/v1",
        ASR_MODEL="qwen-audio-3.0-asr-flash-filetrans",
        ASR_TIMEOUT_SECONDS=2,
        POLISH_ENABLED=False,
    )
    api_client = httpx.AsyncClient(
        base_url=settings.asr_base_url,
        headers={"Authorization": f"Bearer {settings.asr_api_key}"},
        transport=httpx.MockTransport(api_handler),
    )
    result_client = httpx.AsyncClient(transport=httpx.MockTransport(result_handler))
    provider = DashScopeAsrProvider(settings, client=api_client, result_client=result_client, poll_interval_seconds=0)
    try:
        result = await provider.transcribe(b"wav-bytes", audio_format="wav", language="zh")
    finally:
        await api_client.aclose()
        await result_client.aclose()

    assert result == "第一句。\n第二句。"
    assert captured["submit_path"] == "/api/v1/services/audio/asr/transcription"
    assert captured["poll_path"] == "/api/v1/tasks/task-1"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["async_header"] == "enable"
    assert captured["result_authorization"] is None
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen-audio-3.0-asr-flash-filetrans"
    expected_data_url = "data:audio/wav;base64," + base64.b64encode(b"wav-bytes").decode("ascii")
    assert body["input"] == {"file_url": expected_data_url}


def test_extract_transcript_reads_file_transcription_payload() -> None:
    assert extract_transcript(
        {"transcripts": [{"text": "第一句。"}, {"text": "第二句。"}, {"text": " "}]}
    ) == "第一句。\n第二句。"


@pytest.mark.asyncio
async def test_dashscope_provider_preserves_safe_error_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "1", "X-Request-ID": "dashscope-request-2"},
            json={"code": "Throttling", "message": "do not log this message"},
        )

    settings = Settings(
        ASR_PROVIDER="dashscope",
        ASR_API_KEY="test-secret",
        ASR_BASE_URL="https://dashscope.example/api/v1",
        ASR_MODEL="qwen-audio-3.0-asr-flash-filetrans",
        POLISH_ENABLED=False,
    )
    client = httpx.AsyncClient(base_url=settings.asr_base_url, transport=httpx.MockTransport(handler))
    provider = DashScopeAsrProvider(settings, client=client)
    try:
        with caplog.at_level("WARNING"), pytest.raises(AsrProviderError) as raised:
            await provider.transcribe(b"wav-bytes", audio_format="wav")
    finally:
        await client.aclose()

    error = raised.value
    assert error.error_type == "rate_limited"
    assert error.provider_code == "Throttling"
    assert error.request_id == "dashscope-request-2"
    assert error.retry_after_seconds == 1
    assert "test-secret" not in caplog.text
    assert "do not log this message" not in caplog.text


@pytest.mark.asyncio
async def test_paraformer_uploads_with_temporary_policy_and_submits_file_urls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {"submit_bodies": [], "oss_bodies": []}
    policy_requests = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal policy_requests
        if request.method == "GET" and request.url.path.endswith("/uploads"):
            policy_requests += 1
            assert request.headers.get("authorization") == "Bearer dashscope-secret"
            assert dict(request.url.params) == {"action": "getPolicy", "model": "paraformer-v2"}
            return httpx.Response(
                200,
                json={
                    "request_id": "policy-request",
                    "data": {
                        "upload_host": "https://uploads.example",
                        "upload_dir": "temporary/audio",
                        "oss_access_key_id": "temporary-access-key",
                        "signature": "temporary-signature",
                        "policy": "temporary-policy",
                        "x_oss_object_acl": "private",
                        "x_oss_forbid_overwrite": "true",
                        "expire_in_seconds": 300,
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/services/audio/asr/transcription"):
            body = json.loads(request.read())
            submit_bodies = captured["submit_bodies"]
            assert isinstance(submit_bodies, list)
            submit_bodies.append(body)
            task_number = len(submit_bodies)
            return httpx.Response(
                200,
                json={
                    "request_id": f"submit-{task_number}",
                    "output": {
                        "task_id": f"task-{task_number}",
                        "task_status": "SUCCEEDED",
                        "results": [{"transcription_url": f"https://results.example/{task_number}.json"}],
                    },
                },
            )
        raise AssertionError(f"unexpected API request: {request.method} {request.url.path}")

    def upload_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        assert request.url.host == "uploads.example"
        body = request.read()
        oss_bodies = captured["oss_bodies"]
        assert isinstance(oss_bodies, list)
        oss_bodies.append(body)
        for expected in (
            b'temporary-access-key',
            b'temporary-signature',
            b'temporary-policy',
            b'name="key"',
            b'name="file"',
            b'wav-bytes',
        ):
            assert expected in body
        return httpx.Response(204)

    def result_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        result_number = request.url.path.removesuffix(".json").rsplit("/", 1)[-1]
        return httpx.Response(200, json={"transcripts": [{"text": f"transcript {result_number}"}]})

    settings = Settings(
        ASR_PROVIDER="dashscope",
        ASR_API_KEY="dashscope-secret",
        ASR_BASE_URL="https://workspace.cn-beijing.maas.aliyuncs.com/api/v1",
        ASR_MODEL="paraformer-v2",
        ASR_TIMEOUT_SECONDS=2,
        POLISH_ENABLED=False,
    )
    api_client = httpx.AsyncClient(
        base_url=settings.asr_base_url,
        headers={"Authorization": f"Bearer {settings.asr_api_key}"},
        transport=httpx.MockTransport(api_handler),
    )
    upload_client = httpx.AsyncClient(transport=httpx.MockTransport(upload_handler))
    result_client = httpx.AsyncClient(transport=httpx.MockTransport(result_handler))
    provider = DashScopeAsrProvider(
        settings,
        client=api_client,
        result_client=result_client,
        upload_client=upload_client,
    )
    try:
        with caplog.at_level("INFO"):
            first = await provider.transcribe(b"wav-bytes-1", audio_format="wav")
            second = await provider.transcribe(b"wav-bytes-2", audio_format="wav")
    finally:
        await api_client.aclose()
        await upload_client.aclose()
        await result_client.aclose()

    assert first == "transcript 1"
    assert second == "transcript 2"
    assert policy_requests == 1
    submit_bodies = captured["submit_bodies"]
    assert isinstance(submit_bodies, list)
    submitted_urls = [body["input"]["file_urls"][0] for body in submit_bodies]
    assert all(url.startswith("oss://temporary/audio/") and url.endswith(".wav") for url in submitted_urls)
    assert submitted_urls[0] != submitted_urls[1]
    for secret in (
        "dashscope-secret",
        "temporary-access-key",
        "temporary-signature",
        "temporary-policy",
        *submitted_urls,
    ):
        assert secret not in caplog.text
    for timing_field in (
        "credential_ms=",
        "upload_ms=",
        "submit_ms=",
        "poll_count=",
        "poll_wait_ms=",
        "result_fetch_ms=",
        "latency_ms=",
    ):
        assert timing_field in caplog.text


@pytest.mark.asyncio
async def test_paraformer_refreshes_rejected_upload_credential_once() -> None:
    policy_requests = 0
    upload_requests = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal policy_requests
        if request.method == "GET" and request.url.path.endswith("/uploads"):
            policy_requests += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "upload_host": "https://uploads.example",
                        "upload_dir": f"temporary/audio-{policy_requests}",
                        "oss_access_key_id": f"access-{policy_requests}",
                        "signature": f"signature-{policy_requests}",
                        "policy": f"policy-{policy_requests}",
                        "expire_in_seconds": 300,
                    }
                },
            )
        if request.method == "POST":
            body = json.loads(request.read())
            assert body["input"]["file_urls"][0].startswith("oss://temporary/audio-2/")
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_id": "task-refreshed",
                        "task_status": "SUCCEEDED",
                        "transcription_url": "https://results.example/refreshed.json",
                    }
                },
            )
        raise AssertionError(f"unexpected API request: {request.method} {request.url.path}")

    def upload_handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_requests
        upload_requests += 1
        assert request.headers.get("authorization") is None
        return httpx.Response(403 if upload_requests == 1 else 204)

    settings = Settings(
        ASR_PROVIDER="dashscope",
        ASR_API_KEY="test-secret",
        ASR_BASE_URL="https://workspace.cn-beijing.maas.aliyuncs.com/api/v1",
        ASR_MODEL="paraformer-v2",
        ASR_TIMEOUT_SECONDS=2,
        POLISH_ENABLED=False,
    )
    api_client = httpx.AsyncClient(base_url=settings.asr_base_url, transport=httpx.MockTransport(api_handler))
    upload_client = httpx.AsyncClient(transport=httpx.MockTransport(upload_handler))
    result_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"transcripts": [{"text": "refreshed transcript"}]})
        )
    )
    provider = DashScopeAsrProvider(
        settings,
        client=api_client,
        result_client=result_client,
        upload_client=upload_client,
    )
    try:
        result = await provider.transcribe(b"wav-bytes", audio_format="wav")
    finally:
        await api_client.aclose()
        await upload_client.aclose()
        await result_client.aclose()

    assert result == "refreshed transcript"
    assert policy_requests == 2
    assert upload_requests == 2


def test_dashscope_polls_once_per_second_by_default() -> None:
    settings = Settings(ASR_API_KEY="test", ASR_MODEL="paraformer-v2", POLISH_ENABLED=False)
    provider = DashScopeAsrProvider(settings)
    assert [provider._poll_delay(index) for index in range(12)] == [1.0] * 12
