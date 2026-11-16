import asyncio

import aiohttp
from botocore.session import Session

from exporter.ranges import LocalWorker
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.regions import Regions
from exporter.s3.throttle import Throttle


def test_a_worker_keeps_the_pace_of_the_buckets_probed_last(aws_session: Session) -> None:
    def new_presigner(region: str) -> Presigner:
        return Presigner(aws_session, region=region, endpoint=Endpoint("http://s3.invalid"))

    async def run() -> tuple[Throttle, Throttle, Throttle]:
        async with aiohttp.ClientSession() as http:
            local = LocalWorker(http, Regions(new_presigner), concurrency=8, remembered=2)
            first = local.throttle("a")
            first.slowed_down(0)
            local.throttle("b")
            kept = local.throttle("a")
            local.throttle("c")
            local.throttle("d")
            return first, kept, local.throttle("a")

    first, kept, forgotten = asyncio.run(run())
    assert kept is first
    assert forgotten is not first
    assert (first.limit, forgotten.limit) == (4, 8)
