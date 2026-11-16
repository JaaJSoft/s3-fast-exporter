from datetime import UTC, datetime
from urllib.parse import unquote_plus

import pytest
from botocore import parsers
from botocore.handlers import decode_list_object_v2
from botocore.session import get_session
from hypothesis import given, settings
from hypothesis import strategies as st

from exporter.s3.scanner import ListPage, ProtocolError, scan_error, scan_list_page
from fakes3 import responses
from fakes3.app import encode_token
from fakes3.golden import PROBES, Probe, golden_buckets
from fakes3.store import Bucket, StoredObject, list_page

OPERATION = get_session().get_service_model("s3").operation_model("ListObjectsV2")

type View = tuple[list[int], list[str], list[str], str | None]


def render_pages(bucket: Bucket, *, prefix: str, delimiter: str, max_keys: int) -> list[bytes]:
    bodies = []
    after = ""
    while True:
        page = list_page(bucket, prefix=prefix, delimiter=delimiter, after=after, max_keys=max_keys)
        token = None if page.resume_after is None else encode_token(page.resume_after)
        body = responses.list_objects_v2(
            bucket=bucket.name,
            prefix=prefix,
            delimiter=delimiter,
            max_keys=max_keys,
            url_encoded=True,
            start_after="",
            continuation_token="",
            next_token=token,
            fetch_owner=False,
            page=page,
        )
        bodies.append(body.encode())
        if page.resume_after is None:
            return bodies
        after = page.resume_after


def botocore_view(body: bytes) -> View:
    response = {"body": body, "headers": {}, "status_code": 200}
    parsed = parsers.RestXMLParser().parse(response, OPERATION.output_shape)
    decode_list_object_v2(parsed, context={"encoding_type_auto_set": True})
    contents = parsed.get("Contents", [])
    return (
        [obj["Size"] for obj in contents],
        [responses.timestamp(obj["LastModified"]) for obj in contents],
        [common["Prefix"] for common in parsed.get("CommonPrefixes", [])],
        parsed.get("NextContinuationToken"),
    )


def scanner_view(page: ListPage) -> View:
    return (
        page.sizes,
        [date.decode() for date in page.last_modified],
        [unquote_plus(prefix.decode()) for prefix in page.common_prefixes],
        page.next_token,
    )


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_golden_pages_read_like_botocore(probe: Probe) -> None:
    bucket = golden_buckets()[probe.bucket]
    for body in render_pages(bucket, prefix=probe.prefix, delimiter=probe.delimiter, max_keys=1000):
        assert scanner_view(scan_list_page(body)) == botocore_view(body)


keys = st.text(st.characters(codec="utf-8"), min_size=1, max_size=20)
objects = st.lists(
    st.tuples(
        keys,
        st.integers(0, 5 * 2**40),
        st.datetimes(datetime(1990, 1, 1), datetime(2100, 1, 1), timezones=st.just(UTC)),
    ),
    max_size=25,
    unique_by=lambda obj: obj[0],
)


@settings(max_examples=300, deadline=None)
@given(
    objects,
    st.sampled_from(["", "a", "é", "/"]),
    st.sampled_from(["", "/", "a", "é"]),
    st.integers(1, 8),
)
def test_random_pages_read_like_botocore(
    objects: list[tuple[str, int, datetime]], prefix: str, delimiter: str, max_keys: int
) -> None:
    bucket = Bucket("bucket", (StoredObject(key, size, date) for key, size, date in objects))
    for body in render_pages(bucket, prefix=prefix, delimiter=delimiter, max_keys=max_keys):
        assert scanner_view(scan_list_page(body)) == botocore_view(body)


EMPTY = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>bucket</Name><Prefix></Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>"
    b"<EncodingType>url</EncodingType><IsTruncated>false</IsTruncated></ListBucketResult>"
)
ONE = EMPTY.replace(b"<KeyCount>0", b"<KeyCount>1").replace(
    b"</ListBucketResult>",
    b"<Contents><Key>a</Key><LastModified>2026-11-16T10:00:00.000Z</LastModified>"
    b"<Size>3</Size></Contents></ListBucketResult>",
)


def test_an_empty_listing_has_nothing() -> None:
    assert scan_list_page(EMPTY) == ListPage([], [], [], None)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(
            EMPTY.replace(b"<EncodingType>url</EncodingType>", b""),
            "not URL-encoded",
            id="no-encoding",
        ),
        pytest.param(
            EMPTY.replace(b">false<", b">true<"), "<NextContinuationToken>", id="no-token"
        ),
        pytest.param(
            EMPTY.replace(b"<IsTruncated>false</IsTruncated>", b""), "<IsTruncated>", id="no-flag"
        ),
        pytest.param(
            EMPTY.replace(b"<KeyCount>0</KeyCount>", b""), "malformed listing", id="no-key-count"
        ),
        pytest.param(ONE.replace(b"<KeyCount>1", b"<KeyCount>2"), "<KeyCount>", id="bad-key-count"),
        pytest.param(ONE.replace(b"<Size>3", b"<Size>three"), "malformed listing", id="bad-size"),
        pytest.param(ONE.replace(b":00.000Z", b":00Z"), "<LastModified>", id="short-date"),
        pytest.param(ONE.replace(b"</Contents>", b""), "malformed listing", id="unterminated"),
    ],
)
def test_protocol_violations_are_errors(body: bytes, reason: str) -> None:
    with pytest.raises(ProtocolError, match=reason):
        scan_list_page(body)


def test_error_documents_give_their_code_and_message() -> None:
    body = responses.error("SlowDown", "Please reduce your request rate & retry", "b", "id")
    assert scan_error(body.encode()) == ("SlowDown", "Please reduce your request rate & retry")
    assert scan_error(b"<html>Bad Gateway</html>") == ("", "")
