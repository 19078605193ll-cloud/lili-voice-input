import asyncio
from collections import defaultdict

import fakeredis.aioredis
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.providers.openrouter_asr import AsrProviderError
from lili_voice_input.services.asr_scheduler import AsrScheduler
from lili_voice_input.services.distributed_limiter import RedisLeaseLimiter
from lili_voice_input.services.limiter import BoundedLimiter
from lili_voice_input.services.polishing import PolishingService
from lili_voice_input.services.runtime import RedisRuntime


class TrackingAsr:
    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.maximum = 0
        self.by_session: defaultdict[str, int] = defaultdict(int)
        self.session_maximum: defaultdict[str, int] = defaultdict(int)
        self.order: list[str] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.block_first = False

    async def transcribe(self, audio: bytes, *, audio_format: str, language: str | None = None) -> str:
        session = audio.decode()
        self.order.append(session)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.by_session[session] += 1
        self.session_maximum[session] = max(self.session_maximum[session], self.by_session[session])
        try:
            if self.block_first and len(self.order) == 1:
                self.first_started.set()
                await self.release_first.wait()
            else:
                await asyncio.sleep(self.delay)
            return session
        finally:
            self.active -= 1
            self.by_session[session] -= 1


def scheduler(provider: TrackingAsr, **overrides: object) -> AsrScheduler:
    config = Settings(polish_enabled=False)
    limiter = RedisLeaseLimiter(RedisRuntime(config), "test:asr", 20, 60)
    options: dict[str, object] = {
        "concurrency": 3,
        "queue_size": 30,
        "queue_timeout_seconds": 1,
        "request_timeout_seconds": 1,
        "global_limiter": limiter,
        "session_max_in_flight": 2,
    }
    options.update(overrides)
    return AsrScheduler(provider, **options)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_asr_scheduler_enforces_global_and_per_session_concurrency() -> None:
    provider = TrackingAsr()
    queue = scheduler(provider)
    await queue.start()
    try:
        tasks = [queue.submit("a", b"a", audio_format="wav", language="zh") for _ in range(6)]
        tasks += [queue.submit("b", b"b", audio_format="wav", language="zh") for _ in range(6)]
        await asyncio.gather(*tasks)
    finally:
        await queue.close()

    assert provider.maximum <= 3
    assert provider.session_maximum["a"] <= 2
    assert provider.session_maximum["b"] <= 2


@pytest.mark.asyncio
async def test_asr_scheduler_round_robin_prevents_long_session_starvation() -> None:
    provider = TrackingAsr(delay=0)
    provider.block_first = True
    queue = scheduler(provider, concurrency=1)
    await queue.start()
    try:
        first = asyncio.create_task(queue.submit("a", b"a", audio_format="wav", language=None))
        await provider.first_started.wait()
        a2 = asyncio.create_task(queue.submit("a", b"a", audio_format="wav", language=None))
        a3 = asyncio.create_task(queue.submit("a", b"a", audio_format="wav", language=None))
        b1 = asyncio.create_task(queue.submit("b", b"b", audio_format="wav", language=None))
        await asyncio.sleep(0)
        provider.release_first.set()
        await asyncio.gather(first, a2, a3, b1)
    finally:
        await queue.close()

    assert provider.order.index("b") < 3


@pytest.mark.asyncio
async def test_asr_provider_timeout_starts_after_queue_admission() -> None:
    provider = TrackingAsr(delay=0.05)
    queue = scheduler(provider, concurrency=1, request_timeout_seconds=0.01, queue_timeout_seconds=1)
    await queue.start()
    try:
        with pytest.raises(AsrProviderError) as raised:
            await queue.submit("a", b"a", audio_format="wav", language=None)
    finally:
        await queue.close()
    assert raised.value.error_type == "timeout"


@pytest.mark.asyncio
async def test_cancelled_asr_job_releases_redis_lease() -> None:
    config = Settings(redis_enabled=True, redis_url="redis://unused", polish_enabled=False)
    runtime = RedisRuntime(config)
    runtime._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    runtime.error = None
    limiter = RedisLeaseLimiter(runtime, "test:asr:leases", 1, 10)
    provider = TrackingAsr()
    provider.block_first = True
    queue = AsrScheduler(
        provider,
        concurrency=1,
        queue_size=2,
        queue_timeout_seconds=1,
        request_timeout_seconds=1,
        global_limiter=limiter,
    )
    await queue.start()
    task = asyncio.create_task(queue.submit("a", b"a", audio_format="wav", language=None))
    await provider.first_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert await runtime.client.zcard("test:asr:leases") == 0
    await queue.close()
    await runtime.close()


class BlockingPolisher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def polish(self, transcript: str) -> str:
        self.started.set()
        await self.release.wait()
        return transcript.upper()


@pytest.mark.asyncio
async def test_polish_capacity_falls_back_to_original_transcript() -> None:
    provider = BlockingPolisher()
    service = PolishingService(
        provider,
        enabled=True,
        limiter=BoundedLimiter(1, 0, 0.02),
    )
    first = asyncio.create_task(service.polish("first"))
    await provider.started.wait()
    second = await service.polish("second")
    provider.release.set()
    await first

    assert second.text == "second"
    assert second.status == "fallback"
    assert second.fallback_reason == "capacity_reached"
