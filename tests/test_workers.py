import asyncio
import multiprocessing
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import aiohttp
import pytest
from botocore.session import Session
from listings import virtual_stats
from servers import serve_app

from exporter.config import parse_settings
from exporter.probe import Prober, ProbeResult
from exporter.s3.lister import RetryPolicy
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.regions import Regions
from exporter.scan import Listing
from exporter.workers import Workers
from fakes3.app import RequestCounter, create_app
from fakes3.virtual import VirtualBucket

OBJECTS = 40_000
BENCH = Listing("bench", "data/")


@pytest.fixture(scope="module")
def runner(aws_session: Session) -> Iterator[asyncio.Runner]:
    # Worker processes resolve their own credentials, from the environment here.
    with pytest.MonkeyPatch.context() as patch, asyncio.Runner() as runner:
        patch.setenv("AWS_ACCESS_KEY_ID", "AKIDFAKES3")
        patch.setenv("AWS_SECRET_ACCESS_KEY", "fakes3-secret")
        yield runner


@pytest.fixture(scope="module")
def fakes3() -> Iterator[tuple[str, RequestCounter]]:
    requests = RequestCounter()
    with serve_app(
        create_app({"bench": VirtualBucket("bench", OBJECTS)}, list_requests=requests)
    ) as url:
        yield url, requests


@pytest.fixture(scope="module")
def workers(runner: asyncio.Runner, fakes3: tuple[str, RequestCounter]) -> Iterator[Workers]:
    started = runner.run(start_workers(fakes3[0]))
    yield started
    runner.run(started.close())


async def start_workers(url: str) -> Workers:
    settings = parse_settings(["--s3.endpoint-url", url, "--s3.force-path-style"], {})
    workers = Workers(2, settings, Endpoint(url, path_style=True))
    workers.start()
    return workers


@asynccontextmanager
async def prober(session: Session, url: str, workers: Workers) -> AsyncIterator[Prober]:
    def new_presigner(region: str) -> Presigner:
        return Presigner(session, region=region, endpoint=Endpoint(url, path_style=True))

    async with aiohttp.ClientSession() as http:
        probing = Prober(
            http, Regions(new_presigner), RetryPolicy(base_delay=0), concurrency=8, workers=workers
        )
        yield probing
        await probing.close()


def test_workers_list_as_one_process_does(
    runner: asyncio.Runner,
    aws_session: Session,
    fakes3: tuple[str, RequestCounter],
    workers: Workers,
) -> None:
    url, requests = fakes3

    async def run() -> list[tuple[ProbeResult, int]]:
        done = []
        async with prober(aws_session, url, workers) as probing:
            for _ in range(3):
                before = requests.total
                done.append((await probing.probe(BENCH), requests.total - before))
        return done

    done = runner.run(run())
    # The same requests as one process: the ranges are the same, spread over two workers.
    assert [sent for _, sent in done] == [44, 40, 40]
    assert all(result.stats == virtual_stats(OBJECTS) for result, _ in done)


def test_a_listing_failing_in_a_worker_fails_its_probe(
    runner: asyncio.Runner,
    aws_session: Session,
    fakes3: tuple[str, RequestCounter],
    workers: Workers,
) -> None:
    async def run() -> ProbeResult:
        async with prober(aws_session, fakes3[0], workers) as probing:
            # With a delimiter, the scan goes to the workers without a discovery request.
            return await probing.probe(Listing("missing", delimiter="/"))

    assert runner.run(run()).stats is None


def test_a_worker_that_stops_fails_its_probe_and_is_replaced(
    runner: asyncio.Runner, aws_session: Session
) -> None:
    slow_s3 = create_app({"bench": VirtualBucket("bench", OBJECTS)}, latency=0.2)

    async def run(url: str) -> tuple[ProbeResult, ProbeResult]:
        others = {child.pid for child in multiprocessing.active_children()}
        workers = await start_workers(url)
        try:
            async with prober(aws_session, url, workers) as probing:
                probe = asyncio.ensure_future(probing.probe(BENCH))
                await asyncio.sleep(1)
                ours = [
                    child for child in multiprocessing.active_children() if child.pid not in others
                ]
                ours[0].kill()
                return await probe, await probing.probe(BENCH)
        finally:
            await workers.close()

    with serve_app(slow_s3) as url:
        failed, listed = runner.run(run(url))
    assert failed.stats is None
    assert listed.stats == virtual_stats(OBJECTS)


def test_closing_stops_the_workers_at_once(
    runner: asyncio.Runner, fakes3: tuple[str, RequestCounter]
) -> None:
    others = {child.pid for child in multiprocessing.active_children()}
    workers = runner.run(start_workers(fakes3[0]))
    started = time.perf_counter()
    runner.run(workers.close())
    assert time.perf_counter() - started < 5
    assert [child for child in multiprocessing.active_children() if child.pid not in others] == []
