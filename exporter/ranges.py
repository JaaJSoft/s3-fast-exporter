"""Ranges of listings, queued for the workers that list them.

A scan splits its listing into ranges and queues them. Workers take ranges while they have room:
the exporter's own process, or worker processes, each listing a number of ranges at once. In each
process, the requests to a bucket wait in one throttle, the bucket's queue, which keeps the pace S3
accepted from one scan to the next.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

import aiohttp

from exporter.failures import WorkerLostError
from exporter.s3.lister import Lister, RetryPolicy
from exporter.s3.proxies import Proxies
from exporter.s3.regions import Regions
from exporter.s3.throttle import Throttle
from exporter.scan import Listing, Scan, scan, scan_range

# A range whose worker stopped is listed by another one, once: a second loss points at the range.
LOST_ATTEMPTS = 2
# Probes name any bucket: the paces kept are those of the buckets probed last.
REMEMBERED_BUCKETS = 4096


@dataclass(frozen=True, slots=True)
class RangeJob:
    listing: Listing
    region: str
    after: str
    until: str | None


class RangeWorker(Protocol):
    async def list(self, job: RangeJob) -> Scan: ...


class LocalWorker:
    """Lists ranges in this process."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        regions: Regions,
        *,
        concurrency: int,
        retry: RetryPolicy | None = None,
        proxies: Proxies | None = None,
        remembered: int = REMEMBERED_BUCKETS,
    ) -> None:
        """concurrency: most requests in flight to a bucket."""
        self._http = http
        self._regions = regions
        self._concurrency = concurrency
        self._retry = retry or RetryPolicy()
        self._proxies = proxies
        self._remembered = remembered
        self._throttles: OrderedDict[str, Throttle] = OrderedDict()

    def throttle(self, bucket: str) -> Throttle:
        """The bucket's queue."""
        throttle = self._throttles.get(bucket)
        if throttle is None:
            throttle = self._throttles[bucket] = Throttle(self._concurrency)
            self._forget_idle(bucket)
        else:
            self._throttles.move_to_end(bucket)
        return throttle

    async def lister(self, bucket: str, region: str) -> Lister:
        """A lister whose requests wait in the bucket's queue."""
        presigner = await self._regions.presigner(region)
        return Lister(self._http, presigner, self._retry, self._proxies, self.throttle(bucket))

    async def list(self, job: RangeJob) -> Scan:
        lister = await self.lister(job.listing.bucket, job.region)
        return await scan_range(lister, job.listing, job.after, job.until)

    def _forget_idle(self, kept: str) -> None:
        # A throttle with requests in flight or waiting stays: forgetting it would let a second
        # one send to the same bucket.
        while len(self._throttles) > self._remembered:
            idle = (name for name, each in self._throttles.items() if each.idle and name != kept)
            if (oldest := next(idle, None)) is None:
                return
            del self._throttles[oldest]


class RangeQueue:
    """Ranges waiting for room on a worker, first come, first served."""

    def __init__(self, workers: Sequence[tuple[RangeWorker, int]]) -> None:
        """workers: each with the number of ranges it lists at once."""
        self._room: asyncio.Queue[RangeWorker] = asyncio.Queue()
        for worker, ranges in workers:
            for _ in range(ranges):
                self._room.put_nowait(worker)
        self.capacity = self._room.qsize()

    async def scan(self, listing: Listing, region: str, bounds: Sequence[str]) -> Scan:
        """Lists the ranges between the bounds, and merges them in key order."""
        return await scan(partial(self._list, listing, region), bounds)

    async def _list(self, listing: Listing, region: str, after: str, until: str | None) -> Scan:
        job = RangeJob(listing, region, after, until)
        attempt = 1
        while True:
            worker = await self._room.get()
            try:
                return await worker.list(job)
            except WorkerLostError:
                if attempt >= LOST_ATTEMPTS:
                    raise
                attempt += 1
            finally:
                self._room.put_nowait(worker)
