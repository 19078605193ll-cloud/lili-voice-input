import asyncio

import pytest

from lili_voice_input.services.admission import AdmissionRejected, LocalAdmissionController


@pytest.mark.asyncio
async def test_admission_queues_and_releases_in_fifo_order() -> None:
    controller = LocalAdmissionController(maximum=1, queue_size=2, wait_seconds=0.5, subject_maximum=2)
    first = await controller.acquire("one", "websocket")
    queued_events: list[tuple[int, int]] = []

    waiter = asyncio.create_task(
        controller.acquire("two", "websocket", lambda position, estimate: _record(queued_events, position, estimate))
    )
    await asyncio.sleep(0)

    assert controller.active == 1
    assert queued_events and queued_events[0][0] == 1
    await first.release()
    second = await waiter
    assert controller.active == 1
    await second.release()
    await second.release()
    assert controller.active == 0


@pytest.mark.asyncio
async def test_admission_cancel_and_timeout_remove_queue_positions() -> None:
    controller = LocalAdmissionController(maximum=1, queue_size=1, wait_seconds=0.02, subject_maximum=1)
    first = await controller.acquire("one", "http")

    with pytest.raises(AdmissionRejected, match="queue_timeout"):
        await controller.acquire("two", "http")
    assert len(controller._queue) == 0

    pending = asyncio.create_task(controller.acquire("two", "http"))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(controller._queue) == 0
    await first.release()


@pytest.mark.asyncio
async def test_admission_enforces_subject_limit_and_queue_limit() -> None:
    controller = LocalAdmissionController(maximum=3, queue_size=1, wait_seconds=0.1, subject_maximum=1)
    first = await controller.acquire("same", "websocket")
    queued = asyncio.create_task(controller.acquire("same", "websocket"))
    await asyncio.sleep(0)

    with pytest.raises(AdmissionRejected, match="capacity_reached"):
        await controller.acquire("third", "websocket")

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await first.release()


@pytest.mark.asyncio
async def test_production_capacity_queues_101_th_through_120_th_and_rejects_121_th() -> None:
    controller = LocalAdmissionController(maximum=100, queue_size=20, wait_seconds=1, subject_maximum=2)
    active = [await controller.acquire(f"active-{index}", "websocket") for index in range(100)]
    queued = [asyncio.create_task(controller.acquire(f"queued-{index}", "websocket")) for index in range(20)]
    await asyncio.sleep(0)

    assert len(controller._queue) == 20
    with pytest.raises(AdmissionRejected, match="capacity_reached"):
        await controller.acquire("connection-121", "websocket")

    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    await asyncio.gather(*(lease.release() for lease in active))
    assert controller.active == 0
    assert len(controller._queue) == 0


async def _record(events: list[tuple[int, int]], position: int, estimate: int) -> None:
    events.append((position, estimate))
