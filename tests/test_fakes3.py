import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from servers import moto_server, serve_app

from fakes3.app import create_app
from fakes3.store import Bucket, StoredObject, list_page

DAY = datetime(2026, 11, 1, tzinfo=UTC)
KEYS = (
    "a", "a b", "a+b", "a%20b", "a/1", "a/2", "a/b/1", "a/b/2", "a/c", "b/1", "b/2/x", "b/3",
    "c", "logs/2026/11/01", "logs/2026/11/02", "logs/2026/12/01", "Z", "_x", "é/1", "é/2", "~tilde",
)  # fmt: skip
ECHOED_FIELDS = ("KeyCount", "IsTruncated", "MaxKeys", "Prefix", "Delimiter", "EncodingType")
LISTINGS = [
    {"prefix": prefix, "delimiter": delimiter, "max_keys": max_keys, "start_after": start_after}
    for prefix in ("", "a", "a/", "b/", "é", "nothing")
    for delimiter in ("", "/", "/1")
    for max_keys in (1, 2, 1000)
    for start_after in ("", "a/b/1")
]


def bucket_of(*keys: str) -> Bucket:
    return Bucket("bucket", (StoredObject(key, len(key), DAY) for key in keys))


def walk(
    bucket: Bucket, *, prefix: str = "", delimiter: str = "", max_keys: int = 1000
) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    common_prefixes: list[str] = []
    after = ""
    while True:
        page = list_page(bucket, prefix=prefix, delimiter=delimiter, after=after, max_keys=max_keys)
        keys += [obj.key for obj in page.contents]
        common_prefixes += page.common_prefixes
        if page.resume_after is None:
            return keys, common_prefixes
        after = page.resume_after


def test_keys_are_listed_in_utf8_byte_order() -> None:
    assert walk(bucket_of("é", "a", "Z", "b", "~")) == (["Z", "a", "b", "~", "é"], [])


@pytest.mark.parametrize("delimiter", ["", "/"])
def test_small_pages_add_up_to_the_full_listing(delimiter: str) -> None:
    bucket = bucket_of(*KEYS)
    full = walk(bucket, delimiter=delimiter)
    for max_keys in range(1, len(KEYS) + 1):
        assert walk(bucket, delimiter=delimiter, max_keys=max_keys) == full


def test_a_common_prefix_is_never_repeated_on_the_next_page() -> None:
    bucket = bucket_of("a/1", "a/2", "a/3", "b")
    first = list_page(bucket, prefix="", delimiter="/", after="", max_keys=1)
    assert first.common_prefixes == ("a/",)
    assert first.resume_after is not None
    second = list_page(bucket, prefix="", delimiter="/", after=first.resume_after, max_keys=1)
    assert [obj.key for obj in second.contents] == ["b"]


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate keys"):
        bucket_of("a", "a")


@pytest.fixture(scope="module")
def fakes3_url() -> Iterator[str]:
    with serve_app(create_app({"bucket": bucket_of(*KEYS)})) as url:
        yield url


def test_a_missing_bucket_is_a_standard_s3_error(fakes3_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{fakes3_url}/missing?list-type=2")
    assert error.value.code == 404
    assert b"<Code>NoSuchBucket</Code>" in error.value.read()


def test_a_forged_continuation_token_is_rejected(fakes3_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{fakes3_url}/bucket?list-type=2&continuation-token=%25%25")
    assert error.value.code == 400
    assert b"<Code>InvalidArgument</Code>" in error.value.read()


@pytest.fixture(scope="module")
def clients(fakes3_url: str) -> Iterator[dict[str, Any]]:
    pytest.importorskip("moto.server")
    with moto_server() as moto_url:
        moto = s3_client(moto_url)
        moto.create_bucket(Bucket="bucket")
        for key in KEYS:
            moto.put_object(Bucket="bucket", Key=key, Body=b"x" * len(key))
        yield {"moto": moto, "fakes3": s3_client(fakes3_url)}


@pytest.mark.parametrize("url_encoding", [True, False], ids=["url-encoded", "raw"])
@pytest.mark.parametrize("listing", LISTINGS, ids=lambda listing: repr(listing))
def test_listings_match_moto(
    clients: dict[str, Any], listing: dict[str, Any], url_encoding: bool
) -> None:
    moto, fakes3 = (with_url_encoding(clients[name], url_encoding) for name in ("moto", "fakes3"))
    assert pages(fakes3, **listing) == pages(moto, **listing)


def s3_client(endpoint: str) -> Any:
    from botocore.config import Config
    from botocore.session import get_session

    return get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url=endpoint,
        aws_access_key_id="fakes3",
        aws_secret_access_key="fakes3",  # noqa: S106
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


def with_url_encoding(client: Any, enabled: bool) -> Any:
    from botocore.handlers import decode_list_object_v2, set_list_objects_encoding_type_url

    events = client.meta.events
    events.unregister("before-parameter-build.s3.ListObjectsV2", set_list_objects_encoding_type_url)
    events.unregister("after-call.s3.ListObjectsV2", decode_list_object_v2)
    if enabled:
        events.register(
            "before-parameter-build.s3.ListObjectsV2", set_list_objects_encoding_type_url
        )
        events.register("after-call.s3.ListObjectsV2", decode_list_object_v2)
    return client


def pages(
    client: Any, *, prefix: str, delimiter: str, max_keys: int, start_after: str
) -> list[dict[str, Any]]:
    request: dict[str, Any] = {"Bucket": "bucket", "Prefix": prefix, "MaxKeys": max_keys}
    if delimiter:
        request["Delimiter"] = delimiter
    if start_after:
        request["StartAfter"] = start_after
    listed = []
    while True:
        response = client.list_objects_v2(**request)
        # AWS documents echoing StartAfter whenever it is sent; moto drops it once a
        # continuation token is present, so only the first page compares it.
        echoed_start_after = None if "ContinuationToken" in request else response.get("StartAfter")
        listed.append(
            {
                **{field: response.get(field) for field in ECHOED_FIELDS},
                "StartAfter": echoed_start_after,
                "Contents": [
                    (obj["Key"], obj["Size"], obj["StorageClass"])
                    for obj in response.get("Contents", [])
                ],
                "CommonPrefixes": [cp["Prefix"] for cp in response.get("CommonPrefixes", [])],
            }
        )
        if not response["IsTruncated"]:
            return listed
        request["ContinuationToken"] = response["NextContinuationToken"]
