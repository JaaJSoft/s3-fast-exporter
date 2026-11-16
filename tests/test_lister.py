import asyncio
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
import pytest
from aiohttp import web
from botocore.config import Config
from botocore.session import Session
from hypothesis import given, settings
from hypothesis import strategies as st
from servers import moto_server, serve_app

from exporter.s3.lister import Lister, RetryPolicy, S3Error
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.scanner import ListPage
from fakes3 import responses
from fakes3.app import create_app
from fakes3.golden import PROBES, Probe, golden_buckets
from fakes3.store import Bucket, list_page

EMPTY_LISTING = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>bucket</Name><Prefix></Prefix><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>"
    b"<EncodingType>url</EncodingType><IsTruncated>false</IsTruncated></ListBucketResult>"
)
SLOW_DOWN = responses.error("SlowDown", "Please reduce your request rate.", "bucket", "id")
NO_DELAY = RetryPolicy(attempts=4, base_delay=0)

type Entries = tuple[list[int], list[bytes], list[bytes]]


class RecordingPresigner(Presigner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.urls: list[str] = []

    async def list_objects_v2(self, bucket: str, **kwargs: Any) -> str:
        url = await super().list_objects_v2(bucket, **kwargs)
        self.urls.append(url)
        return url


def presigner_for(session: Session, url: str) -> RecordingPresigner:
    return RecordingPresigner(session, region="us-east-1", endpoint=Endpoint(url, path_style=True))


def collect(
    presigner: Presigner, bucket: str, *, retry: RetryPolicy = NO_DELAY, **listing: str
) -> list[ListPage]:
    async def run() -> list[ListPage]:
        async with aiohttp.ClientSession() as http:
            return [page async for page in Lister(http, presigner, retry).pages(bucket, **listing)]

    return asyncio.run(run())


def entries(pages: list[ListPage]) -> Entries:
    return (
        [size for page in pages for size in page.sizes],
        [date for page in pages for date in page.last_modified],
        [prefix for page in pages for prefix in page.common_prefixes],
    )


def expected_entries(bucket: Bucket, prefix: str, delimiter: str) -> Entries:
    sizes, dates, prefixes = [], [], []
    after = ""
    while True:
        page = list_page(bucket, prefix=prefix, delimiter=delimiter, after=after, max_keys=1000)
        sizes += [obj.size for obj in page.contents]
        dates += [responses.timestamp(obj.last_modified).encode() for obj in page.contents]
        prefixes += [quote(common, safe="/").encode() for common in page.common_prefixes]
        if page.resume_after is None:
            return sizes, dates, prefixes
        after = page.resume_after


@pytest.fixture(scope="module")
def fakes3(aws_session: Session) -> Iterator[RecordingPresigner]:
    with serve_app(create_app(golden_buckets())) as url:
        yield presigner_for(aws_session, url)


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_listings_match_the_store(fakes3: RecordingPresigner, probe: Probe) -> None:
    pages = collect(fakes3, probe.bucket, prefix=probe.prefix, delimiter=probe.delimiter)
    bucket = golden_buckets()[probe.bucket]
    assert entries(pages) == expected_entries(bucket, probe.prefix, probe.delimiter)


def test_a_missing_bucket_raises_the_s3_error(fakes3: RecordingPresigner) -> None:
    with pytest.raises(S3Error) as error:
        collect(fakes3, "missing")
    assert (error.value.status, error.value.code) == (404, "NoSuchBucket")


def test_aws_urls_are_regional_and_signed_with_sigv4(aws_session: Session) -> None:
    presigner = Presigner(aws_session, region="eu-west-1")
    url = asyncio.run(presigner.list_objects_v2("my-bucket", prefix="backups/"))
    assert url.startswith("https://my-bucket.s3.eu-west-1.amazonaws.com/?list-type=2")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "encoding-type=url" in url


@pytest.fixture(scope="module")
def capture(aws_session: Session) -> Iterator[tuple[RecordingPresigner, list[str]]]:
    targets: list[str] = []

    async def record(request: web.Request) -> web.Response:
        targets.append(request.raw_path)
        return web.Response(body=EMPTY_LISTING, content_type="application/xml")

    app = web.Application()
    app.router.add_get("/{path:.*}", record)
    with serve_app(app) as url:
        yield presigner_for(aws_session, url), targets


text = st.text(st.characters(codec="utf-8"), max_size=20)


@settings(max_examples=150, deadline=None)
@given(text, st.sampled_from(["", "/", "+", " ", "é"]), text)
def test_presigned_urls_reach_s3_byte_for_byte(
    capture: tuple[RecordingPresigner, list[str]], prefix: str, delimiter: str, start_after: str
) -> None:
    presigner, targets = capture
    collect(presigner, "bucket", prefix=prefix, delimiter=delimiter, start_after=start_after)
    signed = urlsplit(presigner.urls[-1])
    assert targets[-1] == f"{signed.path}?{signed.query}"


def flaky(failures: int) -> web.Application:
    remaining = [failures]

    async def respond(request: web.Request) -> web.Response:
        if remaining[0] > 0:
            remaining[0] -= 1
            return web.Response(status=503, text=SLOW_DOWN, content_type="application/xml")
        return web.Response(body=EMPTY_LISTING, content_type="application/xml")

    app = web.Application()
    app.router.add_get("/{path:.*}", respond)
    return app


def test_server_errors_are_retried(aws_session: Session) -> None:
    with serve_app(flaky(failures=3)) as url:
        assert collect(presigner_for(aws_session, url), "bucket") == [ListPage([], [], [], None)]


def test_retries_give_up_after_the_last_attempt(aws_session: Session) -> None:
    with serve_app(flaky(failures=4)) as url, pytest.raises(S3Error) as error:
        collect(presigner_for(aws_session, url), "bucket")
    assert (error.value.status, error.value.code) == (503, "SlowDown")


def test_connection_failures_are_retried_then_raised(aws_session: Session) -> None:
    with serve_app(web.Application()) as url:
        closed = url
    with pytest.raises(aiohttp.ClientConnectionError):
        collect(presigner_for(aws_session, closed), "bucket")


MOTO_KEYS = (
    "a", "a b", "a+b", "a%20b", "a/1", "a/b/1", "a/b/2", "b/1", "é/1", "~tilde", "Z", "q?x=1&y",
)  # fmt: skip


def test_listings_match_botocore_on_moto(aws_session: Session) -> None:
    pytest.importorskip("moto.server")
    with moto_server() as url:
        client = aws_session.create_client(
            "s3",
            region_name="us-east-1",
            endpoint_url=url,
            config=Config(s3={"addressing_style": "path"}),
        )
        client.create_bucket(Bucket="bucket")
        for key in MOTO_KEYS:
            client.put_object(Bucket="bucket", Key=key, Body=b"x" * len(key))
        presigner = presigner_for(aws_session, url)
        for prefix in ("", "a", "a/", "é"):
            for delimiter in ("", "/"):
                ours = entries(collect(presigner, "bucket", prefix=prefix, delimiter=delimiter))
                theirs = client.list_objects_v2(Bucket="bucket", Prefix=prefix, Delimiter=delimiter)
                assert ours[0] == [obj["Size"] for obj in theirs.get("Contents", [])]
                assert [date.decode() for date in ours[1]] == [
                    responses.timestamp(obj["LastModified"]) for obj in theirs.get("Contents", [])
                ]
                assert [prefix.decode() for prefix in ours[2]] == [
                    quote(common["Prefix"], safe="/") for common in theirs.get("CommonPrefixes", [])
                ]
