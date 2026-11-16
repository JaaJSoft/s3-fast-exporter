import asyncio
from collections.abc import Awaitable, Callable

import pytest

from exporter.s3.throttle import Throttle


def run[T](test: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await test()

    return asyncio.run(main())


async def hold(throttle: Throttle, release: asyncio.Event, started: list[int]) -> int:
    async with throttle.request() as pace:
        started.append(pace)
        await release.wait()
        return pace


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_requests_beyond_the_limit_wait_their_turn() -> None:
    async def test() -> list[int]:
        throttle, release = Throttle(2), asyncio.Event()
        started: list[int] = []
        tasks = [asyncio.create_task(hold(throttle, release, started)) for _ in range(5)]
        await settle()
        counts = [len(started)]
        release.set()
        await asyncio.gather(*tasks)
        return [*counts, len(started)]

    assert run(test) == [2, 5]


def test_a_503_halves_the_limit_once_per_pace() -> None:
    throttle = Throttle(16)
    throttle.slowed_down(0)
    throttle.slowed_down(0)
    assert throttle.limit == 8
    throttle.slowed_down(1)
    assert throttle.limit == 4
    for pace in range(2, 10):
        throttle.slowed_down(pace)
    assert throttle.limit == 1


def test_each_round_of_answers_raises_the_limit_by_one_up_to_the_ceiling() -> None:
    throttle = Throttle(8)
    throttle.slowed_down(0)
    for _ in range(5):
        throttle.answered()
    assert throttle.limit == 5
    for _ in range(100):
        throttle.answered()
    assert throttle.limit == 8


def test_requests_wait_when_the_limit_drops_below_those_in_flight() -> None:
    async def test() -> tuple[list[int], int]:
        throttle, release = Throttle(4), asyncio.Event()
        started: list[int] = []
        tasks = [asyncio.create_task(hold(throttle, release, started)) for _ in range(4)]
        await settle()
        throttle.slowed_down(started[0])
        later = asyncio.create_task(hold(throttle, asyncio.Event(), started))
        await settle()
        waiting = len(started)
        release.set()
        await asyncio.gather(*tasks)
        await settle()
        pace = started[-1]
        later.cancel()
        return [waiting, len(started)], pace

    counts, pace = run(test)
    assert counts == [4, 5]
    assert pace == 1


@pytest.mark.parametrize("handed_room", [False, True])
def test_a_cancelled_request_gives_its_room_back(handed_room: bool) -> None:
    async def test() -> int:
        throttle, released = Throttle(1), asyncio.Event()
        started: list[int] = []
        first = throttle.request()
        await first.__aenter__()
        second = asyncio.create_task(hold(throttle, asyncio.Event(), started))
        await settle()
        if handed_room:
            # The room goes to the second request, cancelled before it runs.
            await first.__aexit__(None, None, None)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        if not handed_room:
            await first.__aexit__(None, None, None)
        released.set()
        async with asyncio.timeout(1):
            await hold(throttle, released, started)
        return len(started)

    assert run(test) == 1
