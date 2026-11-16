import calendar
import logging
import time
from dataclasses import dataclass

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError

from exporter.s3.lister import Lister, RetryPolicy, S3Error
from exporter.s3.regions import Regions
from exporter.s3.scanner import ListPage, ProtocolError

log = logging.getLogger(__name__)

LISTING_ERRORS = (
    S3Error,
    ProtocolError,
    aiohttp.ClientError,
    TimeoutError,
    BotoCoreError,
    ClientError,
)


@dataclass(slots=True)
class ListingStats:
    objects: int = 0
    total_size: int = 0
    biggest_size: int = 0
    newest_date: bytes = b""
    newest_size: int = 0
    common_prefixes: int = 0

    def add(self, page: ListPage) -> None:
        sizes, dates = page.sizes, page.last_modified
        if sizes:
            self.objects += len(sizes)
            self.total_size += sum(sizes)
            self.biggest_size = max(self.biggest_size, max(sizes))
            # max() returns the first maximum, and later pages only win when strictly newer:
            # among objects with the same date, the first listed one wins, as upstream.
            newest = max(range(len(dates)), key=dates.__getitem__)
            if dates[newest] > self.newest_date:
                self.newest_date = dates[newest]
                self.newest_size = sizes[newest]
        self.common_prefixes += len(page.common_prefixes)

    def newest_epoch(self) -> int:
        """Whole seconds, as upstream truncates them; 0 when nothing was listed."""
        if not self.newest_date:
            return 0
        moment = time.strptime(self.newest_date[:19].decode(), "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(moment)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    bucket: str
    prefix: str
    delimiter: str
    stats: ListingStats | None
    duration: float


async def run_probe(
    http: aiohttp.ClientSession,
    regions: Regions,
    retry: RetryPolicy,
    *,
    bucket: str,
    prefix: str,
    delimiter: str,
) -> ProbeResult:
    started = time.perf_counter()
    stats = ListingStats()
    try:
        lister = Lister(http, await regions.presigner_for(bucket), retry)
        async for page in lister.pages(bucket, prefix=prefix, delimiter=delimiter):
            stats.add(page)
    except LISTING_ERRORS as error:
        regions.forget(bucket)
        log.warning(
            "listing failed",
            extra={"bucket": bucket, "prefix": prefix, "delimiter": delimiter, "error": str(error)},
        )
        return ProbeResult(bucket, prefix, delimiter, None, time.perf_counter() - started)
    return ProbeResult(bucket, prefix, delimiter, stats, time.perf_counter() - started)
