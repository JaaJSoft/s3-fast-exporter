"""Lists a prefix as consecutive key ranges, read all at once, each from its own StartAfter.

S3 lists entries (keys, and common prefixes standing for their groups) in UTF-8 byte order, and
starts a listing after any string. A listing therefore splits into ranges (after, until], each
read page by page until a page reaches its upper bound: merged in key order, the ranges give
exactly what a single listing gives.
"""

import asyncio
from bisect import bisect_left
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    # Requests sent, retries included, and those S3 answered 503 SlowDown.
    requests: int = 0
    slow_downs: int = 0

    def add(self, page: ListPage) -> None:
        self.stats.add(page)
        self.entries += len(page.sizes) + len(page.common_prefixes)
        self.pages += 1

    def extend(self, later: "Scan") -> None:
        self.stats.merge(later.stats)
        self.samples += [(end, self.entries + count) for end, count in later.samples]
        self.entries += later.entries
        self.pages += later.pages
        self.requests += later.requests
        self.slow_downs += later.slow_downs


# Lists the range (after, until] of a listing; until None is the end of the listing.
type RangeLister = Callable[[str, str | None], Coroutine[Any, Any, Scan]]


async def scan(list_range: RangeLister, bounds: Sequence[str] = ()) -> Scan:
    """Lists the ranges between the bounds concurrently, and merges them in key order."""
    # Overlapping ranges would count entries twice: the bounds must be sorted and distinct.
    ordered = sorted(set(bounds))
    try:
        async with asyncio.TaskGroup() as group:
            ranges = [
                group.create_task(list_range(after, until))
                for after, until in zip(["", *ordered], [*ordered, None], strict=True)
            ]
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return _merged(ranges)


async def discover(
    list_range: RangeLister, source: PageSource, listing: Listing, ranges: int
) -> Scan:
    """Lists a prefix met for the first time, split along the groups of its "/" hierarchy.

    The groups come a page at a time: the ranges of a page are listed while the next page is
    read, so that a level of many thousands of groups is split all along.
    """
    page, level = await _top_level(source, listing)
    parts: list[asyncio.Task[Scan]] = []
    try:
        async with asyncio.TaskGroup() as group:
            after = ""
            while True:
                groups = [decode(name) for name in page.common_prefixes]
                last = page.last()
                # A level goes on while its pages hold groups; past them, the rest is one range.
                end = None
                if page.next_token is not None and last is not None and len(groups) > 1:
                    end = _bound_after(*last)
                bounds = _spread(groups, ranges)
                parts += [
                    group.create_task(list_range(start, stop))
                    for start, stop in zip([after, *bounds], [*bounds, end], strict=True)
                ]
                if end is None:
                    break
                after = end
                page = await _first_page(source, listing.bucket, level, after)
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return _merged(parts)


def _merged(parts: list[asyncio.Task[Scan]]) -> Scan:
    total = Scan()
    for part in parts:
        total.extend(part.result())
    return total


async def scan_range(source: PageSource, listing: Listing, after: str, until: str | None) -> Scan:
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
        # A range that ends inside a page adds a sample at its bound, with the count of the page
        # end before it: a range ending at that page end needs no page past its bound.
        nearest = bisect_left(counts, counts[nearest])
        end, count = scan.samples[nearest]
        if 0 < count < scan.entries and (not bounds or end > bounds[-1]):
            bounds.append(end)
    return bounds


async def _top_level(source: PageSource, listing: Listing) -> tuple[ListPage, str]:
    """The first page of the first level under the prefix that holds more than one group."""
    level = listing.prefix
    for _ in range(DISCOVERY_DEPTH - 1):
        page = await _first_page(source, listing.bucket, level, "")
        if len(page.common_prefixes) != 1 or page.next_token is not None:
            return page, level
        level = decode(page.common_prefixes[0])
    return await _first_page(source, listing.bucket, level, ""), level


async def _first_page(source: PageSource, bucket: str, prefix: str, after: str) -> ListPage:
    pages = source.pages(bucket, prefix=prefix, delimiter="/", start_after=after)
    async with aclosing(pages):
        return await anext(pages)


def _spread(groups: list[str], ranges: int) -> list[str]:
    """Bounds among the groups, for as many ranges at most, each over as many groups."""
    ranges = min(ranges, len(groups))
    return [groups[len(groups) * index // ranges] for index in range(1, ranges)]
