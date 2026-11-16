import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from html import escape
from urllib.request import getproxies_environment

from aiohttp import web
from botocore.session import Session, get_session
from prometheus_client import CollectorRegistry, generate_latest

from exporter import __version__
from exporter.config import Settings
from exporter.exposition import CONTENT_TYPE, exporter_registry, probe_exposition
from exporter.failures import LISTING_ERRORS
from exporter.probe import Prober
from exporter.s3.account import Account
from exporter.s3.client import http_client
from exporter.s3.lister import RetryPolicy
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.proxies import Proxies
from exporter.s3.regions import Regions
from exporter.scan import Listing
from exporter.workers import Workers

log = logging.getLogger(__name__)

SCRAPE_TIMEOUT_HEADER = "X-Prometheus-Scrape-Timeout-Seconds"
# Prometheus drops answers that arrive after its scrape timeout: probes answer this much sooner.
SCRAPE_TIMEOUT_MARGIN = 0.5
NOSNIFF = {"X-Content-Type-Options": "nosniff"}


@dataclass(frozen=True, slots=True)
class S3:
    account: Account
    prober: Prober


SETTINGS: web.AppKey[Settings] = web.AppKey("settings")
S3_ACCESS: web.AppKey[S3] = web.AppKey("s3")
REGISTRY: web.AppKey[CollectorRegistry] = web.AppKey("registry")


def create_app(settings: Settings, session: Session | None = None) -> web.Application:
    app = web.Application()
    app[SETTINGS] = settings
    app[REGISTRY] = exporter_registry(__version__)
    app.cleanup_ctx.append(s3_context(session or get_session()))
    app.on_shutdown.append(stop_scans)
    app.router.add_get(settings.probe_path, probe)
    app.router.add_get(settings.discovery_path, discovery)
    app.router.add_get(settings.metrics_path, metrics)
    app.router.add_get("/-/healthy", healthy)
    if "/" not in (settings.probe_path, settings.discovery_path, settings.metrics_path):
        app.router.add_get("/", landing)
    return app


def s3_context(session: Session) -> Callable[[web.Application], AsyncIterator[None]]:
    async def context(app: web.Application) -> AsyncIterator[None]:
        settings = app[SETTINGS]
        region = session.get_config_variable("region") or "us-east-1"
        endpoint = Endpoint(
            settings.endpoint_url,
            path_style=settings.force_path_style,
            use_ssl=not settings.disable_ssl,
        )
        account = await asyncio.to_thread(Account, session, region=region, endpoint=endpoint)
        regions = Regions(
            lambda bucket_region: Presigner(session, region=bucket_region, endpoint=endpoint),
            default_region=region,
            # A custom endpoint is a single S3 service: its buckets share the configured region.
            lookup=account.bucket_region if endpoint.url is None else None,
        )
        workers = None
        if settings.list_processes > 1:
            workers = Workers(settings.list_processes, settings, endpoint)
            workers.start()
        try:
            async with http_client(session, settings.list_concurrency) as http:
                prober = Prober(
                    http,
                    regions,
                    RetryPolicy(),
                    concurrency=settings.list_concurrency * settings.list_processes,
                    proxies=Proxies(getproxies_environment()),
                    workers=workers,
                )
                app[S3_ACCESS] = S3(account, prober)
                yield
                await prober.close()
        finally:
            if workers is not None:
                await workers.close()

    return context


async def stop_scans(app: web.Application) -> None:
    # Runs before the server waits for the requests in flight: probes waiting for a long scan
    # fail at once instead of holding the shutdown.
    if (s3 := app.get(S3_ACCESS)) is not None:
        await s3.prober.close()


async def probe(request: web.Request) -> web.Response:
    bucket = request.query.get("bucket", "")
    if not bucket:
        return web.Response(status=400, text="bucket parameter is missing\n", headers=NOSNIFF)
    listing = Listing(bucket, request.query.get("prefix", ""), request.query.get("delimiter", ""))
    timeout = scrape_timeout(request.headers.get(SCRAPE_TIMEOUT_HEADER))
    result = await request.app[S3_ACCESS].prober.probe(listing, timeout)
    return web.Response(body=probe_exposition(result), headers={"Content-Type": CONTENT_TYPE})


def scrape_timeout(header: str | None) -> float | None:
    """Seconds a probe may take, from the scrape timeout Prometheus sends; None without one."""
    try:
        seconds = float(header or "nan")
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return max(seconds - SCRAPE_TIMEOUT_MARGIN, seconds / 2)


async def discovery(request: web.Request) -> web.Response:
    try:
        names = await asyncio.to_thread(request.app[S3_ACCESS].account.bucket_names)
    except LISTING_ERRORS as error:
        log.warning("bucket discovery failed", extra={"error": str(error)})
        return web.Response(status=500, text="error listing buckets\n", headers=NOSNIFF)
    targets = [
        {"targets": [request.host], "labels": {"__param_bucket": name}} for name in names if name
    ]
    return web.Response(
        text=json.dumps(targets, separators=(",", ":"), ensure_ascii=False),
        content_type="application/json",
    )


async def metrics(request: web.Request) -> web.Response:
    body = generate_latest(request.app[REGISTRY])
    return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE})


async def healthy(request: web.Request) -> web.Response:
    return web.Response(text="OK\n")


async def landing(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    links = (
        (
            f"{settings.probe_path}?bucket=BUCKET&prefix=PREFIX",
            "Probe the objects of BUCKET under PREFIX",
        ),
        (settings.metrics_path, "Metrics"),
        (settings.discovery_path, "Service discovery"),
    )
    items = "\n".join(f'<p><a href="{escape(href)}">{text}</a></p>' for href, text in links)
    return web.Response(
        content_type="text/html",
        text=f"<html>\n<head><title>S3 Exporter</title></head>\n<body>\n"
        f"<h1>S3 Exporter</h1>\n{items}\n</body>\n</html>\n",
    )
