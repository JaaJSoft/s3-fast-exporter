import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError

from exporter.s3.lister import Lister, RetryPolicy, S3Error
from exporter.s3.proxies import Proxies
from exporter.s3.regions import Regions
from exporter.s3.scanner import ProtocolError
from exporter.scan import Listing, balanced_bounds, discover_bounds, scan
from exporter.stats import ListingStats

log = logging.getLogger(__name__)

LISTING_ERRORS = (
    S3Error,
    ProtocolError,
    aiohttp.ClientError,
    TimeoutError,
    BotoCoreError,
    ClientError,
)
REMEMBERED_LISTINGS = 4096


@dataclass(frozen=True, slots=True)
class ProbeResult:
    listing: Listing
    stats: ListingStats | None
    duration: float


class Prober:
    """Scans listings, each split along the page ends of its previous scan.

    A listing has one scan at a time: probes arriving while it runs share its result.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        regions: Regions,
        retry: RetryPolicy,
        *,
        concurrency: int,
        proxies: Proxies | None = None,
        remembered: int = REMEMBERED_LISTINGS,
    ) -> None:
        self._http = http
        self._regions = regions
        self._retry = retry
        self._proxies = proxies
        self._concurrency = concurrency
        self._remembered = remembered
        self._scans: dict[Listing, asyncio.Task[ProbeResult]] = {}
        self._bounds: OrderedDict[Listing, list[str]] = OrderedDict()

    async def probe(self, listing: Listing) -> ProbeResult:
        running = self._scans.get(listing)
        if running is None:
            running = asyncio.create_task(self._scan(listing))
            self._scans[listing] = running
            running.add_done_callback(lambda _: self._scans.pop(listing, None))
        # shield: a probe that gives up must not cancel the scan others are waiting for.
        return await asyncio.shield(running)

    async def close(self) -> None:
        running = list(self._scans.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def _scan(self, listing: Listing) -> ProbeResult:
        started = time.perf_counter()
        try:
            presigner = await self._regions.presigner_for(listing.bucket)
            lister = Lister(self._http, presigner, self._retry, self._proxies)
            bounds = self._bounds.get(listing)
            if bounds is None and self._concurrency > 1 and not listing.delimiter:
                bounds = await discover_bounds(lister, listing, self._concurrency)
            result = await scan(lister, listing, bounds or ())
        except LISTING_ERRORS as error:
            self._regions.forget(listing.bucket)
            log.warning(
                "listing failed",
                extra={
                    "bucket": listing.bucket,
                    "prefix": listing.prefix,
                    "delimiter": listing.delimiter,
                    "error": str(error),
                },
            )
            return ProbeResult(listing, None, time.perf_counter() - started)
        self._remember(listing, balanced_bounds(result, self._concurrency))
        return ProbeResult(listing, result.stats, time.perf_counter() - started)

    def _remember(self, listing: Listing, bounds: list[str]) -> None:
        self._bounds[listing] = bounds
        self._bounds.move_to_end(listing)
        if len(self._bounds) > self._remembered:
            self._bounds.popitem(last=False)
