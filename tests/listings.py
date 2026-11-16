"""fakes3 listings, rendered and read by the scanner without HTTP."""

import asyncio
from collections.abc import AsyncGenerator

from exporter.s3.scanner import ListPage, scan_list_page
from fakes3 import responses
from fakes3.app import decode_token, encode_token
from fakes3.store import Bucket, list_page


def render_page(
    bucket: Bucket,
    *,
    prefix: str = "",
    delimiter: str = "",
    max_keys: int = 1000,
    start_after: str = "",
    continuation_token: str = "",
) -> bytes:
    after = decode_token(continuation_token) if continuation_token else start_after
    page = list_page(bucket, prefix=prefix, delimiter=delimiter, after=after, max_keys=max_keys)
    return responses.list_objects_v2(
        bucket=bucket.name,
        prefix=prefix,
        delimiter=delimiter,
        max_keys=max_keys,
        url_encoded=True,
        start_after=start_after,
        continuation_token=continuation_token,
        next_token=None if page.resume_after is None else encode_token(page.resume_after),
        fetch_owner=False,
        page=page,
    ).encode()


def render_pages(bucket: Bucket, *, prefix: str, delimiter: str, max_keys: int) -> list[bytes]:
    bodies = [render_page(bucket, prefix=prefix, delimiter=delimiter, max_keys=max_keys)]
    while (token := scan_list_page(bodies[-1]).next_token) is not None:
        bodies.append(
            render_page(
                bucket,
                prefix=prefix,
                delimiter=delimiter,
                max_keys=max_keys,
                continuation_token=token,
            )
        )
    return bodies


class StoreSource:
    """Pages as the lister gets them from fakes3, counting the requests."""

    def __init__(self, bucket: Bucket, *, max_keys: int = 1000) -> None:
        self.bucket = bucket
        self.max_keys = max_keys
        self.requests = 0

    async def pages(
        self, bucket: str, *, prefix: str = "", delimiter: str = "", start_after: str = ""
    ) -> AsyncGenerator[ListPage, None]:
        token = ""
        while True:
            self.requests += 1
            body = render_page(
                self.bucket,
                prefix=prefix,
                delimiter=delimiter,
                max_keys=self.max_keys,
                start_after="" if token else start_after,
                continuation_token=token,
            )
            page = scan_list_page(body)
            yield page
            if page.next_token is None:
                return
            token = page.next_token
            # Ranges listed together interleave, as they do over HTTP.
            await asyncio.sleep(0)
