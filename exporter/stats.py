import calendar
import time
from dataclasses import dataclass

from exporter.s3.scanner import ListPage


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

    def merge(self, later: "ListingStats") -> None:
        """Adds the stats of the entries listed right after these ones."""
        self.objects += later.objects
        self.total_size += later.total_size
        self.biggest_size = max(self.biggest_size, later.biggest_size)
        if later.newest_date > self.newest_date:
            self.newest_date = later.newest_date
            self.newest_size = later.newest_size
        self.common_prefixes += later.common_prefixes

    def newest_epoch(self) -> int:
        """Whole seconds, as upstream truncates them; 0 when nothing was listed."""
        if not self.newest_date:
            return 0
        moment = time.strptime(self.newest_date[:19].decode(), "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(moment)
