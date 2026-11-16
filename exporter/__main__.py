import asyncio
import logging
import os
import sys

from aiohttp import web

from exporter import __version__
from exporter.app import create_app
from exporter.config import parse_settings
from exporter.logs import configure_logging

log = logging.getLogger("exporter")


def main() -> None:
    settings = parse_settings(sys.argv[1:], os.environ)
    configure_logging(settings.log_level, settings.log_format, settings.log_stream)
    log.info(
        "starting",
        extra={
            "version": __version__,
            "address": f"{settings.listen_host or ''}:{settings.listen_port}",
        },
    )
    web.run_app(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        loop=_event_loop(),
        print=None,
        access_log=None,
    )


def _event_loop() -> asyncio.AbstractEventLoop | None:
    try:
        import uvloop
    except ImportError:
        return None
    loop: asyncio.AbstractEventLoop = uvloop.new_event_loop()
    return loop


if __name__ == "__main__":
    main()
