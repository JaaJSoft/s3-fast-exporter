import asyncio
import random
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import aiohttp
from yarl import URL

from exporter.s3.presigner import Presigner
from exporter.s3.proxies import Proxies
from exporter.s3.scanner import ListPage, scan_error, scan_list_page
from exporter.s3.throttle import Throttle

RETRYABLE_STATUSES = frozenset({500, 502, 504})
# S3 answers 503 SlowDown past the request rate it accepts: a request to slow down, no failure.
SLOW_DOWN = 503


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
        ceiling = min(self.max_delay, self.base_delay * 2 ** min(attempt, 32))
        return random.uniform(0, ceiling)  # noqa: S311


class Lister:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        presigner: Presigner,
        retry: RetryPolicy | None = None,
        proxies: Proxies | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self._http = http
        self._presigner = presigner
        self._retry = retry or RetryPolicy()
        self._proxies = proxies
        self._throttle = throttle or Throttle(sys.maxsize)
        self.requests = 0
        self.slow_downs = 0

    async def pages(
        self, bucket: str, *, prefix: str = "", delimiter: str = "", start_after: str = ""
    ) -> AsyncGenerator[ListPage, None]:
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
        # encoded=True: re-quoting the presigned URL would invalidate its signature.
        target = URL(url, encoded=True)
        proxy = None if self._proxies is None else self._proxies(target)
        failures = slow_downs = 0
        while True:
            async with self._throttle.request() as pace:
                self.requests += 1
                try:
                    async with self._http.get(target, proxy=proxy) as response:
                        body = await response.read()
                except (aiohttp.ClientError, TimeoutError):
                    failures += 1
                    if failures >= self._retry.attempts:
                        raise
                else:
                    if response.status == 200:
                        self._throttle.answered()
                        return body
                    if response.status == SLOW_DOWN:
                        # Back to the queue after a pause, however long S3 takes to accept it.
                        self._throttle.slowed_down(pace)
                        slow_downs += 1
                        self.slow_downs += 1
                    else:
                        failures += 1
                        retryable = response.status in RETRYABLE_STATUSES
                        if not retryable or failures >= self._retry.attempts:
                            raise S3Error(response.status, *scan_error(body))
            await asyncio.sleep(self._retry.delay(failures + slow_downs))
