from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from hashlib import md5
from urllib.parse import quote
from xml.sax.saxutils import escape

from fakes3.store import Listable, Page, StoredObject

NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
OWNER_ID = "fakes3"
PROLOG = '<?xml version="1.0" encoding="UTF-8"?>\n'
QUOTE_ENTITY = {'"': "&quot;"}


def list_objects_v2(
    *,
    bucket: str,
    prefix: str,
    delimiter: str,
    max_keys: int,
    url_encoded: bool,
    start_after: str,
    continuation_token: str,
    next_token: str | None,
    fetch_owner: bool,
    page: Page,
    contents: str | None = None,
) -> str:
    """contents: the page's <Contents> elements, when the caller has them rendered already."""
    encode: Callable[[str], str] = _url_encode if url_encoded else _identity
    parts = [
        f'{PROLOG}<ListBucketResult xmlns="{NAMESPACE}">',
        _element("Name", bucket),
        _element("Prefix", encode(prefix)),
    ]
    if next_token is not None:
        parts.append(_element("NextContinuationToken", next_token))
    parts.append(_element("KeyCount", str(len(page.contents) + len(page.common_prefixes))))
    parts.append(_element("MaxKeys", str(max_keys)))
    if delimiter:
        parts.append(_element("Delimiter", encode(delimiter)))
    if url_encoded:
        parts.append(_element("EncodingType", "url"))
    parts.append(_element("IsTruncated", "true" if next_token is not None else "false"))
    if continuation_token:
        parts.append(_element("ContinuationToken", continuation_token))
    if start_after:
        parts.append(_element("StartAfter", encode(start_after)))
    if contents is None:
        parts.extend(_contents(obj, encode, fetch_owner) for obj in page.contents)
    else:
        parts.append(contents)
    parts.extend(
        f"<CommonPrefixes>{_element('Prefix', encode(common))}</CommonPrefixes>"
        for common in page.common_prefixes
    )
    parts.append("</ListBucketResult>")
    return "".join(parts)


def list_all_my_buckets(buckets: Iterable[Listable]) -> str:
    entries = "".join(
        "<Bucket>"
        + _element("Name", bucket.name)
        + _element("CreationDate", timestamp(bucket.created))
        + _element("BucketRegion", bucket.region)
        + "</Bucket>"
        for bucket in sorted(buckets, key=lambda bucket: bucket.name)
    )
    return (
        f'{PROLOG}<ListAllMyBucketsResult xmlns="{NAMESPACE}">'
        f"<Owner>{_element('ID', OWNER_ID)}{_element('DisplayName', OWNER_ID)}</Owner>"
        f"<Buckets>{entries}</Buckets></ListAllMyBucketsResult>"
    )


def error(code: str, message: str, bucket: str, request_id: str) -> str:
    return (
        f"{PROLOG}<Error>"
        + _element("Code", code)
        + _element("Message", message)
        + _element("BucketName", bucket)
        + _element("RequestId", request_id)
        + _element("HostId", OWNER_ID)
        + "</Error>"
    )


def timestamp(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def etag(obj: StoredObject) -> str:
    digest = md5(f"{obj.key}\0{obj.size}".encode(), usedforsecurity=False).hexdigest()
    return f'"{digest}"'


def _contents(obj: StoredObject, encode: Callable[[str], str], fetch_owner: bool) -> str:
    owner = f"<Owner>{_element('ID', OWNER_ID)}</Owner>" if fetch_owner else ""
    return (
        "<Contents>"
        + _element("Key", encode(obj.key))
        + _element("LastModified", timestamp(obj.last_modified))
        + _element("ETag", etag(obj))
        + owner
        + _element("Size", str(obj.size))
        + _element("StorageClass", obj.storage_class)
        + "</Contents>"
    )


def _element(name: str, text: str) -> str:
    return f"<{name}>{escape(text, QUOTE_ENTITY)}</{name}>"


def _url_encode(value: str) -> str:
    return quote(value, safe="/")


def _identity(value: str) -> str:
    return value
