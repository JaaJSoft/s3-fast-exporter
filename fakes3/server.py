from aiohttp import web

from fakes3.app import RequestCounter, create_app
from fakes3.golden import golden_buckets
from fakes3.store import Listable
from fakes3.virtual import VirtualBucket

VIRTUAL_BUCKET = "bench"


def serve(
    *,
    host: str,
    port: int,
    latency: float,
    virtual_objects: int,
    reuse_port: bool,
    list_requests: RequestCounter,
) -> None:
    buckets: dict[str, Listable] = {**golden_buckets()}
    if virtual_objects:
        buckets[VIRTUAL_BUCKET] = VirtualBucket(VIRTUAL_BUCKET, virtual_objects)
    web.run_app(
        create_app(buckets, latency=latency, list_requests=list_requests),
        host=host,
        port=port,
        reuse_port=reuse_port,
        print=None,
        access_log=None,
    )
