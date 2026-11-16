import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass

from aiohttp import web

from fakes3 import responses
from fakes3.store import Bucket, list_page

MAX_KEYS = 1000
REQUEST_ID = "FAKES3REQUEST000"
BUCKETS: web.AppKey[dict[str, Bucket]] = web.AppKey("buckets")


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


def create_app(buckets: Mapping[str, Bucket]) -> web.Application:
    app = web.Application()
    app[BUCKETS] = dict(buckets)
    app.router.add_get("/", list_buckets, allow_head=False)
    app.router.add_route("HEAD", "/{bucket}", head_bucket)
    app.router.add_get("/{bucket}", list_objects, allow_head=False)
    app.router.add_get("/{bucket}/", list_objects, allow_head=False)
    return app


async def list_buckets(request: web.Request) -> web.Response:
    return _xml(responses.list_all_my_buckets(request.app[BUCKETS].values()))


async def head_bucket(request: web.Request) -> web.Response:
    bucket = request.app[BUCKETS].get(request.match_info["bucket"])
    if bucket is None:
        return web.Response(status=404, headers=_headers())
    return web.Response(headers={**_headers(), "x-amz-bucket-region": bucket.region})


async def list_objects(request: web.Request) -> web.Response:
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


def _xml(body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=body, status=status, content_type="application/xml", headers=_headers()
    )


def _error(status: int, code: str, message: str, bucket: str) -> web.Response:
    return _xml(responses.error(code, message, bucket, REQUEST_ID), status)


def _headers() -> dict[str, str]:
    return {"x-amz-request-id": REQUEST_ID, "x-amz-id-2": responses.OWNER_ID}
