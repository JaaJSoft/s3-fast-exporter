import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from exporter.failures import LISTING_ERRORS
from exporter.ranges import LocalWorker, RangeQueue
from exporter.s3.regions import Regions
from exporter.scan import Listing, balanced_bounds, discover, scan
from exporter.stats import ListingStats

log = logging.getLogger(__name__)

REMEMBERED_LISTINGS = 4096


@dataclass(frozen=True, slots=True)
class ProbeResult:
    listing: Listing
    stats: ListingStats | None
    duration: float
    requests: int = 0
    slow_downs: int = 0


class Prober:
    """Scans listings, each split along the page ends of its previous scan.

    A listing has one scan at a time: probes arriving while it runs share its result. Its ranges
    go through the queue, to the workers that list them.
    """

    def __init__(
        self,
        regions: Regions,
        local: LocalWorker,
        queue: RangeQueue,
        *,
        remembered: int = REMEMBERED_LISTINGS,
    ) -> None:
        """local: lists in this process the first page of a new listing, to split it."""
        self._regions = regions
        self._local = local
        self._queue = queue
        self._remembered = remembered
        self._scans: dict[Listing, asyncio.Task[ProbeResult]] = {}
        self._bounds: OrderedDict[Listing, list[str]] = OrderedDict()

    async def probe(self, listing: Listing, wait: float | None = None) -> ProbeResult:
        """wait: seconds after which the probe fails, while its scan goes on for the others."""
        started = time.perf_counter()
        running = self._scans.get(listing)
        if running is None:
            running = asyncio.create_task(self._scan(listing))
            self._scans[listing] = running
            running.add_done_callback(lambda _: self._scans.pop(listing, None))
        try:
            async with asyncio.timeout(wait) as deadline:
                # shield: a probe that gives up must not cancel the scan others are waiting for.
                return await asyncio.shield(running)
        except TimeoutError:
            if not deadline.expired():
                raise
        log.warning("listing outlasted the scrape timeout", extra=_fields(listing, wait=wait))
        return ProbeResult(listing, None, time.perf_counter() - started)

    async def close(self) -> None:
        running = list(self._scans.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def _scan(self, listing: Listing) -> ProbeResult:
        started = time.perf_counter()
        try:
            region = await self._regions.region_of(listing.bucket)
            list_range = self._queue.lister(listing, region)
            bounds = self._bounds.get(listing)
            ranges = self._queue.capacity
            if bounds is None and ranges > 1 and not listing.delimiter:
                lister = await self._local.lister(listing.bucket, region)
                result = await discover(list_range, lister, listing, ranges)
                result.requests += lister.requests
                result.slow_downs += lister.slow_downs
            else:
                result = await scan(list_range, bounds or ())
        except LISTING_ERRORS as error:
            self._regions.forget(listing.bucket)
            log.warning("listing failed", extra=_fields(listing, error=str(error)))
            return ProbeResult(listing, None, time.perf_counter() - started)
        self._remember(listing, balanced_bounds(result, self._queue.capacity))
        duration = time.perf_counter() - started
        return ProbeResult(listing, result.stats, duration, result.requests, result.slow_downs)

    def _remember(self, listing: Listing, bounds: list[str]) -> None:
        self._bounds[listing] = bounds
        self._bounds.move_to_end(listing)
        if len(self._bounds) > self._remembered:
            self._bounds.popitem(last=False)


def _fields(listing: Listing, **fields: object) -> dict[str, object]:
    return {
        "bucket": listing.bucket,
        "prefix": listing.prefix,
        "delimiter": listing.delimiter,
        **fields,
    }
