"""A bucket of made-up objects for benchmarks, cheap to hold and to list whatever its size.

Object i is data/{i // 10_000:05d}/{i:010d}.bin, with a size and a time of day derived from i.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import md5
from typing import overload

from fakes3.store import DEFAULT_CREATED, Page, StoredObject, past

GROUP_SIZE = 10_000
DAY = datetime(2026, 10, 31, tzinfo=UTC)
DATE = f"{DAY:%Y-%m-%d}"


def key_of(index: int) -> str:
    return f"data/{index // GROUP_SIZE:05d}/{index:010d}.bin"


def size_of(index: int) -> int:
    return index * 2_654_435_761 % 1_000_003


def second_of(index: int) -> int:
    # Many objects share a time of day: the first one listed is the newest, as for any tie.
    return index * 7_919 % 86_400


class Keys:
    __slots__ = ("_indexes",)

    def __init__(self, indexes: range) -> None:
        self._indexes = indexes

    def __len__(self) -> int:
        return len(self._indexes)

    def __getitem__(self, index: int) -> bytes:
        return key_of(self._indexes[index]).encode()


class Objects(Sequence[StoredObject]):
    __slots__ = ("_indexes",)

    def __init__(self, indexes: range) -> None:
        self._indexes = indexes

    def __len__(self) -> int:
        return len(self._indexes)

    @overload
    def __getitem__(self, index: int) -> StoredObject: ...

    @overload
    def __getitem__(self, index: slice) -> "Objects": ...

    def __getitem__(self, index: int | slice) -> "StoredObject | Objects":
        if isinstance(index, slice):
            return Objects(self._indexes[index])
        number = self._indexes[index]
        return StoredObject(
            key_of(number), size_of(number), DAY + timedelta(seconds=second_of(number))
        )


class VirtualBucket:
    __slots__ = ("count", "created", "name", "objects", "region", "sort_keys")

    def __init__(
        self,
        name: str,
        count: int,
        *,
        region: str = "us-east-1",
        created: datetime = DEFAULT_CREATED,
    ) -> None:
        self.name = name
        self.count = count
        self.region = region
        self.created = created
        self.objects = Objects(range(count))
        self.sort_keys = Keys(range(count))

    def fast_page(self, *, prefix: str, after: str, max_keys: int) -> tuple[Page, str]:
        """The page store.list_page gives without delimiter, with its <Contents> rendered.

        Both come out as fakes3 renders stored objects, many times faster.
        """
        first = bisect_left(self.sort_keys, prefix.encode())
        end = bisect_left(self.sort_keys, past(prefix.encode())) if prefix else self.count
        if after:
            first = max(first, bisect_right(self.sort_keys, after.encode()))
        stop = min(end, first + max_keys)
        resume_after = key_of(stop - 1) if first < stop < end else None
        return Page(self.objects[first:stop], (), resume_after), _contents(range(first, stop))


def _contents(indexes: range) -> str:
    parts = []
    for index in indexes:
        key, size, second = key_of(index), size_of(index), second_of(index)
        etag = md5(f"{key}\0{size}".encode(), usedforsecurity=False).hexdigest()
        parts.append(
            f"<Contents><Key>{key}</Key><LastModified>{DATE}T{second // 3600:02d}:"
            f"{second // 60 % 60:02d}:{second % 60:02d}.000Z</LastModified>"
            f"<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size>"
            f"<StorageClass>STANDARD</StorageClass></Contents>"
        )
    return "".join(parts)
