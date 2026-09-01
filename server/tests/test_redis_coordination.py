import asyncio

import fakeredis.aioredis
import pytest

from lili_voice_input.config import Settings
from lili_voice_input.services.admission import (
    AdmissionRejected,
    HybridAdmissionController,
    LocalAdmissionController,
    RedisAdmissionController,
)
from lili_voice_input.services.distributed_limiter import RedisLeaseLimiter
from lili_voice_input.services.limiter import StageCapacityError
from lili_voice_input.services.runtime import RedisRuntime, RuntimeUnavailable


def fake_runtime() -> RedisRuntime:
    settings = Settings(redis_enabled=True, redis_url="redis://unused", polish_enabled=False)
    runtime = RedisRuntime(settings)
    runtime._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    runtime.error = None
    return runtime


@pytest.mark.asyncio
async def test_redis_provider_leases_are_atomic_and_reusable() -> None:
    runtime = fake_runtime()
    limiter = RedisLeaseLimiter(runtime, "test:leases", maximum=1, lease_ttl_seconds=10)
    first = await limiter.acquire(0.1)
    with pytest.raises(StageCapacityError):
        await limiter.acquire(0.02)
    await first.release()
    second = await limiter.acquire(0.1)
    await second.release()
    await runtime.close()


@pytest.mark.asyncio
async def test_redis_admission_queue_releases_and_rejects_overflow() -> None:
    runtime = fake_runtime()
    controller = RedisAdmissionController(
        runtime,
        "test",
        maximum=1,
        queue_size=1,
        wait_seconds=0.2,
        subject_maximum=1,
        lease_ttl_seconds=30,
    )
    first = await controller.acquire("one", "websocket")
    queued = asyncio.create_task(controller.acquire("two", "websocket"))
    await asyncio.sleep(0.02)

    with pytest.raises(AdmissionRejected, match="capacity_reached"):
        await controller.acquire("three", "websocket")

    await first.release()
    second = await queued
    await second.release()
    await runtime.close()


class BrokenRedis:
    async def eval(self, *args: object) -> object:
        raise ConnectionError("redis unavailable")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_provider_work_fails_open_but_new_admission_fails_closed_on_redis_loss() -> None:
    runtime = fake_runtime()
    runtime._client = BrokenRedis()  # type: ignore[assignment]
    limiter = RedisLeaseLimiter(runtime, "test:leases", maximum=1, lease_ttl_seconds=10)
    lease = await limiter.acquire(0.1)
    assert lease.runtime is None
    assert not runtime.ready

    runtime.error = None
    controller = RedisAdmissionController(runtime, "test", 1, 1, 0.1, 1, 30)
    with pytest.raises(RuntimeUnavailable):
        await controller.acquire("one", "websocket")


@pytest.mark.asyncio
async def test_hybrid_admission_has_one_global_five_second_deadline_and_releases_global_lease() -> None:
    runtime = fake_runtime()
    distributed = RedisAdmissionController(runtime, "test", 2, 2, 0.05, 2, 30)
    hybrid = HybridAdmissionController(
        LocalAdmissionController(1, 1, 0.2, 2),
        distributed,
    )
    first = await hybrid.acquire("one", "websocket")

    with pytest.raises(AdmissionRejected, match="queue_timeout"):
        await hybrid.acquire("two", "websocket")

    active = await runtime.client.zcard("test:admission:active")
    assert active == 1
    await first.release()
    await runtime.close()
