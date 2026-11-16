from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime
    storage_class: str = "STANDARD"


class Bucket:
    __slots__ = ("created", "name", "objects", "region", "sort_keys")

    def __init__(
        self,
        name: str,
        objects: Iterable[StoredObject],
        *,
        region: str = "us-east-1",
        created: datetime = DEFAULT_CREATED,
    ) -> None:
        self.name = name
        self.region = region
        self.created = created
        # S3 orders keys by their UTF-8 bytes, not by code points or locale.
        self.objects = tuple(sorted(objects, key=lambda obj: obj.key.encode()))
        self.sort_keys = tuple(obj.key.encode() for obj in self.objects)
        if len(set(self.sort_keys)) != len(self.sort_keys):
            raise ValueError(f"duplicate keys in bucket {name}")


@dataclass(frozen=True, slots=True)
class Page:
    contents: tuple[StoredObject, ...]
    common_prefixes: tuple[str, ...]
    resume_after: str | None


def list_page(bucket: Bucket, *, prefix: str, delimiter: str, after: str, max_keys: int) -> Page:
    keys = bucket.sort_keys
    encoded_prefix = prefix.encode()
    encoded_after = after.encode()
    index = bisect_left(keys, encoded_prefix)
    if after:
        index = max(index, bisect_right(keys, encoded_after))
    contents: list[StoredObject] = []
    common_prefixes: list[str] = []
    last_key: str | None = None
    while index < len(keys) and keys[index].startswith(encoded_prefix):
        if len(contents) + len(common_prefixes) >= max_keys:
            return Page(tuple(contents), tuple(common_prefixes), last_key)
        obj = bucket.objects[index]
        common = common_prefix(obj.key, prefix, delimiter)
        if common is None:
            contents.append(obj)
            index += 1
        else:
            encoded_common = common.encode()
            # A listing returns the entries sorting after `after`, and a common prefix is an
            # entry: one that sorts before it (after points inside its group) is not returned.
            if encoded_common > encoded_after:
                common_prefixes.append(common)
            while index < len(keys) and keys[index].startswith(encoded_common):
                index += 1
        last_key = bucket.objects[index - 1].key
    return Page(tuple(contents), tuple(common_prefixes), None)


def common_prefix(key: str, prefix: str, delimiter: str) -> str | None:
    if not delimiter:
        return None
    position = key.find(delimiter, len(prefix))
    if position < 0:
        return None
    return key[: position + len(delimiter)]
