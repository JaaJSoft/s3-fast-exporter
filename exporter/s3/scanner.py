"""Extracts what the exporter needs from ListObjectsV2 pages without building an XML tree.

The listing is requested with encoding-type=url, so keys and prefixes never contain "<":
locating elements by their tags is exact, and much cheaper than a generic XML parser.
"""

from dataclasses import dataclass
from xml.sax.saxutils import unescape

LAST_MODIFIED_WIDTH = len(b"2009-10-12T17:50:30.000Z")


class ProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ListPage:
    sizes: list[int]
    # S3 timestamps all have the same width, so their byte order is their time order.
    last_modified: list[bytes]
    common_prefixes: list[bytes]
    next_token: str | None


def scan_list_page(body: bytes) -> ListPage:
    if body.find(b"<EncodingType>url</EncodingType>") < 0:
        raise ProtocolError("the listing is not URL-encoded")
    try:
        sizes, last_modified = _scan_contents(body)
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
    return ListPage(sizes, last_modified, common_prefixes, _next_token(body))


def scan_error(body: bytes) -> tuple[str, str]:
    code, message = _element(body, b"Code"), _element(body, b"Message")
    return (
        unescape(code.decode()) if code else "",
        unescape(message.decode()) if message else "",
    )


def _scan_contents(body: bytes) -> tuple[list[int], list[bytes]]:
    find = body.find
    sizes: list[int] = []
    last_modified: list[bytes] = []
    start = find(b"<Contents>")
    while start >= 0:
        end = find(b"</Contents>", start)
        if end < 0:
            raise ValueError
        size = find(b"<Size>", start, end) + 6
        date = find(b"<LastModified>", start, end) + 14
        if size < 6 or date < 14:
            raise ValueError
        sizes.append(int(body[size : find(b"<", size)]))
        last_modified.append(body[date : find(b"<", date)])
        start = find(b"<Contents>", end)
    return sizes, last_modified


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
