import asyncio
import base64
import binascii
import multiprocessing
from collections.abc import Mapping
from dataclasses import dataclass

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware

from fakes3 import responses
from fakes3.store import Listable, list_page
from fakes3.virtual import VirtualBucket

MAX_KEYS = 1000
REQUEST_ID = "FAKES3REQUEST000"
COUNTERS = "/_fakes3/"
LIST_REQUESTS_PATH = COUNTERS + "list-requests"
SLOW_DOWNS_PATH = COUNTERS + "slow-downs"


class RequestCounter:
    """Counts the ListObjectsV2 requests served; a shared one counts across worker processes."""

    def __init__(self, *, shared: bool = False) -> None:
        self._count = 0
        self._shared = multiprocessing.Value("Q", 0) if shared else None

    def add(self) -> None:
        if self._shared is None:
            self._count += 1
            return
        with self._shared.get_lock():
            self._shared.value += 1

    @property
    def total(self) -> int:
        return self._count if self._shared is None else int(self._shared.value)


class InFlight:
    """Requests being answered, up to a limit; a shared one counts across worker processes."""

    def __init__(self, limit: int, *, shared: bool = False) -> None:
        self._limit = limit
        self._count = 0
        self._shared = multiprocessing.Value("Q", 0) if shared else None

    def enter(self) -> bool:
        if self._shared is None:
            if self._count >= self._limit:
                return False
            self._count += 1
            return True
        with self._shared.get_lock():
            if self._shared.value >= self._limit:
                return False
            self._shared.value += 1
            return True

    def leave(self) -> None:
        if self._shared is None:
            self._count -= 1
            return
        with self._shared.get_lock():
            self._shared.value -= 1


@dataclass(frozen=True, slots=True)
class SlowDown:
    """S3's request rate limit, as a number of requests in flight past which it answers 503."""

    in_flight: InFlight
    answered: RequestCounter


BUCKETS: web.AppKey[dict[str, Listable]] = web.AppKey("buckets")
LIST_REQUESTS: web.AppKey[RequestCounter] = web.AppKey("list_requests")
SLOW_DOWNS: web.AppKey[RequestCounter] = web.AppKey("slow_downs")


class InvalidArgumentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ListRequest:
    prefix: str
    delimiter: str
    max_keys: int
    url_encoded: bool
    start_after: str
    continuation_token: str
    after: str
    fetch_owner: bool


def create_app(
    buckets: Mapping[str, Listable],
    *,
    latency: float = 0.0,
    list_requests: RequestCounter | None = None,
    slow_down: SlowDown | None = None,
) -> web.Application:
    """latency: seconds S3 takes to answer any request."""
    middlewares = [] if slow_down is None else [_slow_down(slow_down, latency)]
    app = web.Application(middlewares=[*middlewares, _delay(latency)] if latency else middlewares)
    app[BUCKETS] = dict(buckets)
    app[LIST_REQUESTS] = list_requests or RequestCounter()
    app[SLOW_DOWNS] = RequestCounter() if slow_down is None else slow_down.answered
    app.router.add_get(LIST_REQUESTS_PATH, list_request_count, allow_head=False)
    app.router.add_get(SLOW_DOWNS_PATH, slow_down_count, allow_head=False)
    app.router.add_get("/", list_buckets, allow_head=False)
    app.router.add_route("HEAD", "/{bucket}", head_bucket)
    app.router.add_get("/{bucket}", list_objects, allow_head=False)
    app.router.add_get("/{bucket}/", list_objects, allow_head=False)
    return app


async def list_request_count(request: web.Request) -> web.Response:
    return web.Response(text=f"{request.app[LIST_REQUESTS].total}\n")


async def slow_down_count(request: web.Request) -> web.Response:
    return web.Response(text=f"{request.app[SLOW_DOWNS].total}\n")


async def list_buckets(request: web.Request) -> web.Response:
    return _xml(responses.list_all_my_buckets(request.app[BUCKETS].values()))


async def head_bucket(request: web.Request) -> web.Response:
    bucket = request.app[BUCKETS].get(request.match_info["bucket"])
    if bucket is None:
        return web.Response(status=404, headers=_headers())
    return web.Response(headers={**_headers(), "x-amz-bucket-region": bucket.region})


async def list_objects(request: web.Request) -> web.Response:
    request.app[LIST_REQUESTS].add()
    name = request.match_info["bucket"]
    bucket = request.app[BUCKETS].get(name)
    if bucket is None:
        return _error(404, "NoSuchBucket", "The specified bucket does not exist", name)
    if request.query.get("list-type") != "2":
        return _error(501, "NotImplemented", "fakes3 only implements ListObjectsV2", name)
    try:
        params = parse_list_request(request.query)
    except InvalidArgumentError as exc:
        return _error(400, "InvalidArgument", str(exc), name)
    contents = None
    if isinstance(bucket, VirtualBucket) and not params.delimiter and not params.fetch_owner:
        page, contents = bucket.fast_page(
            prefix=params.prefix, after=params.after, max_keys=params.max_keys
        )
    else:
        page = list_page(
            bucket,
            prefix=params.prefix,
            delimiter=params.delimiter,
            after=params.after,
            max_keys=params.max_keys,
        )
    next_token = None if page.resume_after is None else encode_token(page.resume_after)
    return _xml(
        responses.list_objects_v2(
            bucket=name,
            prefix=params.prefix,
            delimiter=params.delimiter,
            max_keys=params.max_keys,
            url_encoded=params.url_encoded,
            start_after=params.start_after,
            continuation_token=params.continuation_token,
            next_token=next_token,
            fetch_owner=params.fetch_owner,
            page=page,
            contents=contents,
        )
    )


def parse_list_request(query: Mapping[str, str]) -> ListRequest:
    try:
        max_keys = int(query.get("max-keys", str(MAX_KEYS)))
    except ValueError:
        raise InvalidArgumentError(
            "Provided max-keys not an integer or within integer range"
        ) from None
    if max_keys < 0:
        raise InvalidArgumentError("Argument maxKeys must be an integer between 0 and 2147483647")
    encoding = query.get("encoding-type", "")
    if encoding not in ("", "url"):
        raise InvalidArgumentError("Invalid Encoding Method specified in Request")
    start_after = query.get("start-after", "")
    token = query.get("continuation-token", "")
    return ListRequest(
        prefix=query.get("prefix", ""),
        delimiter=query.get("delimiter", ""),
        max_keys=min(max_keys, MAX_KEYS),
        url_encoded=encoding == "url",
        start_after=start_after,
        continuation_token=token,
        after=decode_token(token) if token else start_after,
        fetch_owner=query.get("fetch-owner", "") == "true",
    )


def encode_token(resume_after: str) -> str:
    return base64.urlsafe_b64encode(resume_after.encode()).decode()


def decode_token(token: str) -> str:
    try:
        return base64.b64decode(token.encode(), altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise InvalidArgumentError("The continuation token provided is incorrect") from None


def _delay(latency: float) -> Middleware:
    @web.middleware
    async def delay(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith(COUNTERS):
            await asyncio.sleep(latency)
        return await handler(request)

    return delay


def _slow_down(slow_down: SlowDown, latency: float) -> Middleware:
    @web.middleware
    async def answer_503(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path.startswith(COUNTERS):
            return await handler(request)
        if not slow_down.in_flight.enter():
            slow_down.answered.add()
            await asyncio.sleep(latency)
            bucket = request.match_info.get("bucket", "")
            return _error(503, "SlowDown", "Please reduce your request rate.", bucket)
        try:
            return await handler(request)
        finally:
            slow_down.in_flight.leave()

    return answer_503


def _xml(body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=body, status=status, content_type="application/xml", headers=_headers()
    )


def _error(status: int, code: str, message: str, bucket: str) -> web.Response:
    return _xml(responses.error(code, message, bucket, REQUEST_ID), status)


def _headers() -> dict[str, str]:
    return {"x-amz-request-id": REQUEST_ID, "x-amz-id-2": responses.OWNER_ID}
