import asyncio
from collections import OrderedDict
from collections.abc import Callable

from exporter.s3.presigner import Presigner

# Probes name any bucket: what is kept per bucket is kept for the buckets probed last.
REMEMBERED_BUCKETS = 4096


class Regions:
    """One presigner per region. With a lookup, each bucket is signed for its own region."""

    def __init__(
        self,
        new_presigner: Callable[[str], Presigner],
        default_region: str = "us-east-1",
        lookup: Callable[[str], str | None] | None = None,
        remembered: int = REMEMBERED_BUCKETS,
    ) -> None:
        self._new_presigner = new_presigner
        self._default_region = default_region
        self._lookup = lookup
        self._remembered = remembered
        self._presigners: dict[str, Presigner] = {}
        self._bucket_regions: OrderedDict[str, asyncio.Future[str | None]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def presigner_for(self, bucket: str) -> Presigner:
        return await self.presigner(await self.region_of(bucket))

    async def presigner(self, region: str) -> Presigner:
        presigner = self._presigners.get(region)
        if presigner is None:
            async with self._lock:
                presigner = self._presigners.get(region)
                if presigner is None:
                    presigner = await asyncio.to_thread(self._new_presigner, region)
                    self._presigners[region] = presigner
        return presigner

    def forget(self, bucket: str) -> None:
        self._bucket_regions.pop(bucket, None)

    async def region_of(self, bucket: str) -> str:
        if self._lookup is None:
            return self._default_region
        lookup = self._bucket_regions.get(bucket)
        if lookup is None:
            # Concurrent probes of a new bucket share one lookup; a failed one is retried later.
            lookup = asyncio.ensure_future(asyncio.to_thread(self._lookup, bucket))
            self._bucket_regions[bucket] = lookup
            lookup.add_done_callback(lambda done: self._discard_failed(bucket, done))
            if len(self._bucket_regions) > self._remembered:
                self._bucket_regions.popitem(last=False)
        else:
            self._bucket_regions.move_to_end(bucket)
        # shield: a cancelled probe must not cancel the lookup the others are waiting for.
        return await asyncio.shield(lookup) or self._default_region

    def _discard_failed(self, bucket: str, lookup: asyncio.Future[str | None]) -> None:
        failed = lookup.cancelled() or lookup.exception() is not None
        if failed and self._bucket_regions.get(bucket) is lookup:
            del self._bucket_regions[bucket]
