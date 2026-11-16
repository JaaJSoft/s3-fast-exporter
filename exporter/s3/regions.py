import asyncio
from collections.abc import Callable

from exporter.s3.presigner import Presigner


class Regions:
    """One presigner per region. With a lookup, each bucket is signed for its own region."""

    def __init__(
        self,
        new_presigner: Callable[[str], Presigner],
        default_region: str,
        lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._new_presigner = new_presigner
        self._default_region = default_region
        self._lookup = lookup
        self._presigners: dict[str, Presigner] = {}
        self._bucket_regions: dict[str, asyncio.Future[str | None]] = {}
        self._lock = asyncio.Lock()

    async def presigner_for(self, bucket: str) -> Presigner:
        region = await self._region_of(bucket)
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

    async def _region_of(self, bucket: str) -> str:
        if self._lookup is None:
            return self._default_region
        lookup = self._bucket_regions.get(bucket)
        if lookup is None:
            # Concurrent probes of a new bucket share one lookup; a failed one is retried later.
            lookup = asyncio.ensure_future(asyncio.to_thread(self._lookup, bucket))
            self._bucket_regions[bucket] = lookup
            lookup.add_done_callback(lambda done: self._discard_failed(bucket, done))
        # shield: a cancelled probe must not cancel the lookup the others are waiting for.
        return await asyncio.shield(lookup) or self._default_region

    def _discard_failed(self, bucket: str, lookup: asyncio.Future[str | None]) -> None:
        failed = lookup.cancelled() or lookup.exception() is not None
        if failed and self._bucket_regions.get(bucket) is lookup:
            del self._bucket_regions[bucket]
