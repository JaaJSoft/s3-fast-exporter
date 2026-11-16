"""Lists a prefix as consecutive key ranges, read all at once, each from its own StartAfter.

S3 lists entries (keys, and common prefixes standing for their groups) in UTF-8 byte order, and
starts a listing after any string. A listing therefore splits into ranges (after, until], each
read page by page until a page reaches its upper bound: merged in key order, the ranges give
exactly what a single listing gives.
"""

import asyncio
from bisect import bisect_left
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Protocol

from exporter.s3.scanner import ListPage, ProtocolError, decode, group_end
from exporter.stats import ListingStats

# A range costs at most one page more than a single listing would: a few pages per range keep
# that overhead small.
MIN_PAGES_PER_RANGE = 4
DISCOVERY_DEPTH = 3

type Sample = tuple[str, int]


class PageSource(Protocol):
    def pages(
        self, bucket: str, *, prefix: str, delimiter: str, start_after: str
    ) -> AsyncGenerator[ListPage, None]: ...


@dataclass(frozen=True, slots=True)
class Listing:
    bucket: str
    prefix: str = ""
    delimiter: str = ""


@dataclass(slots=True)
class Scan:
    stats: ListingStats = field(default_factory=ListingStats)
    # Where pages ended, each with the number of entries listed up to there.
    samples: list[Sample] = field(default_factory=list)
    entries: int = 0
    pages: int = 0

    def add(self, page: ListPage) -> None:
        self.stats.add(page)
        self.entries += len(page.sizes) + len(page.common_prefixes)
        self.pages += 1

    def extend(self, later: "Scan") -> None:
        self.stats.merge(later.stats)
        self.samples += [(end, self.entries + count) for end, count in later.samples]
        self.entries += later.entries
        self.pages += later.pages


async def scan(source: PageSource, listing: Listing, bounds: Sequence[str] = ()) -> Scan:
    """Lists the ranges between the bounds concurrently, and merges them."""
    # Overlapping ranges would count entries twice: the bounds must be sorted and distinct.
    ordered = sorted(set(bounds))
    try:
        async with asyncio.TaskGroup() as group:
            ranges = [
                group.create_task(_scan_range(source, listing, after, until))
                for after, until in zip(["", *ordered], [*ordered, None], strict=True)
            ]
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    total = Scan()
    for part in ranges:
        total.extend(part.result())
    return total


async def _scan_range(source: PageSource, listing: Listing, after: str, until: str | None) -> Scan:
    part = Scan()
    pages = source.pages(
        listing.bucket, prefix=listing.prefix, delimiter=listing.delimiter, start_after=after
    )
    async with aclosing(pages):
        async for page in pages:
            start = page.start()
            if start is not None and start <= after:
                # A server ignoring StartAfter would make every range count the same entries.
                raise ProtocolError("the listing does not start after its StartAfter")
            last = page.last()
            if until is not None and last is not None and _reaches(*last, until):
                part.add(page.up_to(until) if last[0] > until else page)
                break
            part.add(page)
            if last is not None and (bound := _bound_after(*last)) is not None:
                part.samples.append((bound, part.entries))
    if until is not None:
        part.samples.append((until, part.entries))
    return part


def _reaches(name: str, group: bool, until: str) -> bool:
    """Whether no entry listed after this one can be at or below until."""
    if not group:
        return name >= until
    # The entries after a group start at its end, which a key may be named exactly.
    end = group_end(name)
    return end is None or end > until


def _bound_after(name: str, group: bool) -> str | None:
    """A bound for ranges to split at this entry.

    S3 does not document whether StartAfter equal to a common prefix lists its group again:
    after a group, ranges split at its end, which no group contains.
    """
    return group_end(name) if group else name


def balanced_bounds(scan: Scan, ranges: int) -> list[str]:
    """Bounds for ranges of as many entries each, chosen among the page ends of a scan."""
    ranges = min(ranges, scan.pages // MIN_PAGES_PER_RANGE)
    counts = [count for _, count in scan.samples]
    bounds: list[str] = []
    for index in range(1, ranges):
        target = scan.entries * index // ranges
        nearest = bisect_left(counts, target)
        if nearest == len(counts) or (
            nearest > 0 and target - counts[nearest - 1] <= counts[nearest] - target
        ):
            nearest -= 1
        if nearest < 0:
            continue
        end, count = scan.samples[nearest]
        if 0 < count < scan.entries and (not bounds or end > bounds[-1]):
            bounds.append(end)
    return bounds


async def discover_bounds(source: PageSource, listing: Listing, ranges: int) -> list[str]:
    """Bounds for a first scan: the groups at the top of the prefix's "/" hierarchy, spread out."""
    prefix = listing.prefix
    for _ in range(DISCOVERY_DEPTH):
        pages = source.pages(listing.bucket, prefix=prefix, delimiter="/", start_after="")
        async with aclosing(pages):
            page = await anext(pages)
        groups = [decode(group) for group in page.common_prefixes]
        if len(groups) != 1 or page.next_token is not None:
            ranges = min(ranges, len(groups))
            return [groups[len(groups) * index // ranges] for index in range(1, ranges)]
        prefix = groups[0]
    return []
