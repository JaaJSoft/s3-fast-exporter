import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp
from yarl import URL

from exporter.s3.presigner import Presigner
from exporter.s3.scanner import ListPage, scan_error, scan_list_page

RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


class S3Error(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.1
    max_delay: float = 2.0

    def delay(self, attempt: int) -> float:
        return random.uniform(0, min(self.max_delay, self.base_delay * 2**attempt))  # noqa: S311


class Lister:
    def __init__(
        self, http: aiohttp.ClientSession, presigner: Presigner, retry: RetryPolicy | None = None
    ) -> None:
        self._http = http
        self._presigner = presigner
        self._retry = retry or RetryPolicy()

    async def pages(
        self, bucket: str, *, prefix: str = "", delimiter: str = "", start_after: str = ""
    ) -> AsyncIterator[ListPage]:
        token: str | None = None
        while True:
            url = await self._presigner.list_objects_v2(
                bucket,
                prefix=prefix,
                delimiter=delimiter,
                start_after=start_after if token is None else "",
                continuation_token=token,
            )
            page = scan_list_page(await self._get(url))
            yield page
            if page.next_token is None:
                return
            token = page.next_token

    async def _get(self, url: str) -> bytes:
        attempt = 1
        while True:
            try:
                # encoded=True: re-quoting the presigned URL would invalidate its signature.
                async with self._http.get(URL(url, encoded=True)) as response:
                    body = await response.read()
            except (aiohttp.ClientError, TimeoutError):
                if attempt >= self._retry.attempts:
                    raise
            else:
                if response.status == 200:
                    return body
                error = S3Error(response.status, *scan_error(body))
                if response.status not in RETRYABLE_STATUSES or attempt >= self._retry.attempts:
                    raise error
            await asyncio.sleep(self._retry.delay(attempt))
            attempt += 1
