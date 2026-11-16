import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import aiohttp
import pytest
from botocore.session import Session
from listings import virtual_stats
from servers import serve_app

from exporter.probe import REMEMBERED_LISTINGS, Prober, ProbeResult
from exporter.ranges import LocalWorker, RangeQueue
from exporter.s3.lister import RetryPolicy
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.regions import Regions
from exporter.scan import Listing
from fakes3.app import InFlight, RequestCounter, SlowDown, create_app
from fakes3.golden import golden_buckets
from fakes3.store import Listable
from fakes3.virtual import VirtualBucket

OBJECTS = 40_000
BENCH = Listing("bench", "data/")


@pytest.fixture(scope="module")
def fakes3() -> Iterator[tuple[str, RequestCounter]]:
    requests = RequestCounter()
    buckets: dict[str, Listable] = {**golden_buckets(), "bench": VirtualBucket("bench", OBJECTS)}
    with serve_app(create_app(buckets, list_requests=requests)) as url:
        yield url, requests


@asynccontextmanager
async def prober(
    session: Session, url: str, remembered: int = REMEMBERED_LISTINGS
) -> AsyncIterator[Prober]:
    def new_presigner(region: str) -> Presigner:
        return Presigner(session, region=region, endpoint=Endpoint(url, path_style=True))

    async with aiohttp.ClientSession() as http:
        regions = Regions(new_presigner)
        local = LocalWorker(http, regions, concurrency=8, retry=RetryPolicy(base_delay=0))
        probing = Prober(regions, local, RangeQueue([(local, 8)]), remembered=remembered)
        yield probing
        await probing.close()


def probes(
    session: Session,
    fakes3: tuple[str, RequestCounter],
    *listings: Listing,
    remembered: int = REMEMBERED_LISTINGS,
) -> list[tuple[ProbeResult, int]]:
    """Each listing probed in turn by one prober, with the requests its probe sent."""
    url, requests = fakes3

    async def run() -> list[tuple[ProbeResult, int]]:
        done = []
        async with prober(session, url, remembered) as probing:
            for listing in listings:
                before = requests.total
                done.append((await probing.probe(listing), requests.total - before))
        return done

    return asyncio.run(run())


def test_each_scan_splits_along_the_page_ends_of_the_previous_one(
    aws_session: Session, fakes3: tuple[str, RequestCounter]
) -> None:
    first, second, third = probes(aws_session, fakes3, BENCH, BENCH, BENCH)
    # One request finds the four groups of data/, then each of the first three ranges reads
    # one page into the next group. Later scans cut 8 ranges at page ends: no request is lost.
    assert [requests for _, requests in (first, second, third)] == [44, 40, 40]
    assert first[0].stats == second[0].stats == third[0].stats == virtual_stats(OBJECTS)


def test_probes_arriving_during_a_scan_share_it(
    aws_session: Session, fakes3: tuple[str, RequestCounter]
) -> None:
    url, requests = fakes3

    async def run() -> tuple[list[ProbeResult], int]:
        async with prober(aws_session, url) as probing:
            before = requests.total
            results = await asyncio.gather(*(probing.probe(BENCH) for _ in range(5)))
            return results, requests.total - before

    results, sent = asyncio.run(run())
    assert sent == 44
    assert all(result is results[0] for result in results)


def test_listings_with_a_delimiter_start_without_discovery(
    aws_session: Session, fakes3: tuple[str, RequestCounter]
) -> None:
    [(result, requests)] = probes(aws_session, fakes3, Listing("bench", "data/", "/"))
    assert requests == 1
    assert result.stats is not None
    assert result.stats.common_prefixes == OBJECTS // 10_000


def test_forgotten_listings_are_discovered_again(
    aws_session: Session, fakes3: tuple[str, RequestCounter]
) -> None:
    done = probes(aws_session, fakes3, BENCH, Listing("single"), BENCH, remembered=1)
    assert [requests for _, requests in done] == [44, 2, 44]


def test_a_failed_listing_has_no_stats(
    aws_session: Session, fakes3: tuple[str, RequestCounter]
) -> None:
    [(result, _)] = probes(aws_session, fakes3, Listing("missing"))
    assert result.listing == Listing("missing")
    assert result.stats is None


def test_a_probe_fails_at_its_timeout_while_the_scan_goes_on(aws_session: Session) -> None:
    requests = RequestCounter()
    slow_s3 = create_app(
        {"bench": VirtualBucket("bench", 3000)}, latency=0.2, list_requests=requests
    )

    async def run() -> tuple[ProbeResult, float, ProbeResult]:
        async with prober(aws_session, url) as probing:
            started = time.perf_counter()
            gave_up = await probing.probe(BENCH, wait=0.1)
            waited = time.perf_counter() - started
            return gave_up, waited, await probing.probe(BENCH)

    with serve_app(slow_s3) as url:
        gave_up, waited, finished = asyncio.run(run())
    assert gave_up.stats is None
    assert waited < 0.2
    # The second probe waited for the scan the first one started: two requests to discover
    # data/, then three pages.
    assert finished.stats is not None
    assert finished.stats.objects == 3000
    assert requests.total == 5


def test_scans_keep_the_pace_s3_accepts(aws_session: Session) -> None:
    # S3 answers 503 SlowDown past 3 requests in flight, and later scans run 8 ranges at once.
    # The bucket's queue keeps the pace from one scan to the next: each scan meets a few 503
    # answers, as the pace probes for room, never a burst of them.
    requests, slow_downs = RequestCounter(), RequestCounter()
    throttled = create_app(
        {"bench": VirtualBucket("bench", OBJECTS)},
        latency=0.02,
        list_requests=requests,
        slow_down=SlowDown(InFlight(3), slow_downs),
    )
    with serve_app(throttled) as url:
        done = probes(aws_session, (url, requests), BENCH, BENCH, BENCH)
    assert all(result.stats == virtual_stats(OBJECTS) for result, _ in done)
    assert 0 < slow_downs.total <= 24


def test_closing_cancels_the_scans_in_flight(aws_session: Session) -> None:
    async def run() -> None:
        async def never_answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()
            writer.close()

        server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        async with server, prober(aws_session, f"http://{host}:{port}") as probing:
            probe = asyncio.ensure_future(probing.probe(Listing("bucket")))
            await asyncio.sleep(0.2)
            await probing.close()
            with pytest.raises(asyncio.CancelledError):
                await probe

    asyncio.run(run())
