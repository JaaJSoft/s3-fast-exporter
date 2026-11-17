import calendar
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import compress

from exporter.s3.scanner import ListPage

# Upper bounds of the size buckets: empty objects, 1 KiB, 128 KiB (the smallest size the
# infrequent access and Glacier Instant Retrieval classes bill), 1 MiB, 16 MiB, 256 MiB, 1 GiB, and
# 5 GiB (the largest single upload).
SIZE_BUCKETS = (0, 1 << 10, 128 << 10, 1 << 20, 16 << 20, 256 << 20, 1 << 30, 5 << 30)


@dataclass(slots=True)
class ListingStats:
    objects: int = 0
    total_size: int = 0
    biggest_size: int = 0
    newest_date: bytes = b""
    newest_size: int = 0
    oldest_date: bytes = b""
    common_prefixes: int = 0
    # Objects at or under each bound of SIZE_BUCKETS.
    size_buckets: list[int] = field(default_factory=lambda: [0] * len(SIZE_BUCKETS))
    # Objects and their total size, by storage class.
    storage_classes: dict[bytes, list[int]] = field(default_factory=dict)

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
            oldest = min(dates)
            if not self.oldest_date or oldest < self.oldest_date:
                self.oldest_date = oldest
            ordered = sorted(sizes)
            for index, bound in enumerate(SIZE_BUCKETS):
                self.size_buckets[index] += bisect_right(ordered, bound)
            self._add_storage_classes(sizes, page.storage_classes)
        self.common_prefixes += len(page.common_prefixes)

    def merge(self, later: "ListingStats") -> None:
        """Adds the stats of the entries listed right after these ones."""
        self.objects += later.objects
        self.total_size += later.total_size
        self.biggest_size = max(self.biggest_size, later.biggest_size)
        if later.newest_date > self.newest_date:
            self.newest_date = later.newest_date
            self.newest_size = later.newest_size
        if later.oldest_date and (not self.oldest_date or later.oldest_date < self.oldest_date):
            self.oldest_date = later.oldest_date
        self.common_prefixes += later.common_prefixes
        self.size_buckets = [
            mine + theirs
            for mine, theirs in zip(self.size_buckets, later.size_buckets, strict=True)
        ]
        for storage_class, (objects, size) in later.storage_classes.items():
            self._add_storage_class(storage_class, objects, size)

    def newest_epoch(self) -> int:
        """Whole seconds, as upstream truncates them; 0 when nothing was listed."""
        return _epoch(self.newest_date)

    def oldest_epoch(self) -> int:
        """Whole seconds; 0 when nothing was listed."""
        return _epoch(self.oldest_date)

    def _add_storage_classes(self, sizes: list[int], storage_classes: list[bytes]) -> None:
        # Pages of one class are the common case: sum() and count() then read each object once,
        # in C.
        first = storage_classes[0]
        if storage_classes.count(first) == len(storage_classes):
            self._add_storage_class(first, len(sizes), sum(sizes))
            return
        for storage_class in set(storage_classes):
            chosen = map(storage_class.__eq__, storage_classes)
            self._add_storage_class(
                storage_class, storage_classes.count(storage_class), sum(compress(sizes, chosen))
            )

    def _add_storage_class(self, storage_class: bytes, objects: int, size: int) -> None:
        totals = self.storage_classes.setdefault(storage_class, [0, 0])
        totals[0] += objects
        totals[1] += size


def _epoch(date: bytes) -> int:
    if not date:
        return 0
    moment = time.strptime(date[:19].decode(), "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(moment)
