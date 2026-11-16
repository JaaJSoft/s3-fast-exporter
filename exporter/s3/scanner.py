"""Extracts what the exporter needs from ListObjectsV2 pages without building an XML tree.

The listing is requested with encoding-type=url, so keys and prefixes never contain "<":
locating elements by their tags is exact, and much cheaper than a generic XML parser.
"""

import html
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from urllib.parse import unquote_plus
from xml.sax.saxutils import unescape

LAST_MODIFIED_WIDTH = len(b"2009-10-12T17:50:30.000Z")
# Each appears once in each <Contents>, and nowhere else in a listing.
SIZE = re.compile(rb"<Size>([0-9]+)</Size>")
LAST_MODIFIED = re.compile(rb"<LastModified>([^<]*)</LastModified>")
STORAGE_CLASS = re.compile(rb"<StorageClass>([^<]*)</StorageClass>")
MAX_CODE_POINT = 0x10FFFF
FIRST_SURROGATE, AFTER_SURROGATES = 0xD800, 0xE000


class ProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ListPage:
    sizes: list[int]
    # S3 timestamps all have the same width, so their byte order is their time order.
    last_modified: list[bytes]
    common_prefixes: list[bytes]
    next_token: str | None
    storage_classes: list[bytes] = field(default_factory=list)
    # Keys stay in the body: only a partitioned listing reads a few of them, at its bounds.
    body: bytes = field(default=b"", repr=False, compare=False)

    def start(self) -> str | None:
        """The first key or common prefix of the page."""
        starts = []
        if self.sizes:
            starts.append(self._key(self.body.find(b"<Key>") + 5))
        if self.common_prefixes:
            starts.append(decode(self.common_prefixes[0]))
        return min(starts, default=None)

    def last(self) -> tuple[str, bool] | None:
        """The last key or common prefix of the page, and whether it is a common prefix."""
        key = self._key(self.body.rfind(b"<Key>") + 5) if self.sizes else None
        group = decode(self.common_prefixes[-1]) if self.common_prefixes else None
        if group is not None and (key is None or group > key):
            return group, True
        return None if key is None else (key, False)

    def up_to(self, bound: str) -> "ListPage":
        """This page without the keys and groups listed after bound."""
        keys = bisect_right(self._key_starts(), bound, key=self._key)
        groups = bisect_right(self.common_prefixes, bound, key=decode)
        return ListPage(
            self.sizes[:keys],
            self.last_modified[:keys],
            self.common_prefixes[:groups],
            None,
            self.storage_classes[:keys],
            self.body,
        )

    def _key(self, start: int) -> str:
        if start < 5:
            raise ProtocolError("a listed object has no <Key>")
        return decode(self.body[start : self.body.find(b"<", start)])

    def _key_starts(self) -> list[int]:
        find = self.body.find
        starts = []
        start = find(b"<Key>")
        while start >= 0:
            starts.append(start + 5)
            start = find(b"<Key>", start + 5)
        if len(starts) != len(self.sizes):
            raise ProtocolError("<Key> elements do not match the listed objects")
        return starts


def decode(listed: bytes) -> str:
    """A key or prefix as an encoding-type=url listing gives it."""
    try:
        text = listed.decode()
        if "&" in text:
            # Valid XML text only holds the predefined and numeric references, which
            # html.unescape decodes as an XML parser does.
            text = html.unescape(text)
        return unquote_plus(text, errors="strict")
    except UnicodeDecodeError:
        raise ProtocolError("a listed name is not UTF-8") from None


def group_end(prefix: str) -> str | None:
    """The smallest string above every string that starts with prefix, if there is one.

    Code point order is UTF-8 byte order, the order S3 lists keys in.
    """
    stem = prefix.rstrip(chr(MAX_CODE_POINT))
    if not stem:
        return None
    code = ord(stem[-1]) + 1
    return stem[:-1] + chr(AFTER_SURROGATES if code == FIRST_SURROGATE else code)


def scan_list_page(body: bytes) -> ListPage:
    if body.find(b"<EncodingType>url</EncodingType>") < 0:
        raise ProtocolError("the listing is not URL-encoded")
    try:
        sizes, last_modified = _scan_contents(body)
        storage_classes = _scan_storage_classes(body, len(sizes))
        key_count = int(_element(body, b"KeyCount") or b"")
    except ValueError:
        raise ProtocolError("malformed listing") from None
    if last_modified and not (
        min(map(len, last_modified)) == LAST_MODIFIED_WIDTH == max(map(len, last_modified))
    ):
        raise ProtocolError("unexpected <LastModified> format")
    common_prefixes = _scan_common_prefixes(body)
    if key_count != len(sizes) + len(common_prefixes):
        raise ProtocolError("<KeyCount> does not match the listed entries")
    return ListPage(sizes, last_modified, common_prefixes, _next_token(body), storage_classes, body)


def scan_error(body: bytes) -> tuple[str, str]:
    code, message = _element(body, b"Code"), _element(body, b"Message")
    return (
        unescape(code.decode()) if code else "",
        unescape(message.decode()) if message else "",
    )


def _scan_contents(body: bytes) -> tuple[list[int], list[bytes]]:
    """The size and the date of each object, in the order of the listing."""
    sizes = list(map(int, SIZE.findall(body)))
    last_modified = LAST_MODIFIED.findall(body)
    objects = body.count(b"<Contents>")
    if not len(sizes) == len(last_modified) == objects == body.count(b"</Contents>"):
        raise ValueError
    return sizes, last_modified


def _scan_storage_classes(body: bytes, objects: int) -> list[bytes]:
    """The storage class of each object, in the order of the listing."""
    if not objects:
        return []
    start = body.find(b"<StorageClass>") + 14
    if start < 14:
        raise ValueError
    first = body[start : body.find(b"<", start)]
    # Pages of one class are the common case: one count, in C, checks every object.
    if body.count(b"<StorageClass>" + first + b"</StorageClass>") == objects:
        return [first] * objects
    storage_classes: list[bytes] = STORAGE_CLASS.findall(body)
    if len(storage_classes) != objects:
        raise ValueError
    return storage_classes


def _scan_common_prefixes(body: bytes) -> list[bytes]:
    find = body.find
    prefixes: list[bytes] = []
    start = find(b"<CommonPrefixes>")
    while start >= 0:
        end = find(b"</CommonPrefixes>", start)
        prefix = find(b"<Prefix>", start, end) + 8
        if end < 0 or prefix < 8:
            raise ProtocolError("malformed <CommonPrefixes>")
        prefixes.append(body[prefix : find(b"<", prefix)])
        start = find(b"<CommonPrefixes>", end)
    return prefixes


def _next_token(body: bytes) -> str | None:
    if body.find(b"<IsTruncated>false</IsTruncated>") >= 0:
        return None
    if body.find(b"<IsTruncated>true</IsTruncated>") < 0:
        raise ProtocolError("missing <IsTruncated>")
    token = _element(body, b"NextContinuationToken")
    if not token:
        raise ProtocolError("a truncated listing must carry a <NextContinuationToken>")
    return unescape(token.decode())


def _element(body: bytes, name: bytes) -> bytes | None:
    start = body.find(b"<" + name + b">")
    if start < 0:
        return None
    start += len(name) + 2
    end = body.find(b"</" + name + b">", start)
    return body[start:end] if end >= 0 else None
