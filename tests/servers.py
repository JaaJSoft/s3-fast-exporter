import asyncio
import socket
import ssl
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

from aiohttp import web


@contextmanager
def serve_app(app: web.Application, tls: ssl.SSLContext | None = None) -> Iterator[str]:
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app, access_log=None)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls).start())
    host, port = runner.addresses[0][:2]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield f"{'https' if tls else 'http'}://{host}:{port}"
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


@contextmanager
def moto_server() -> Iterator[str]:
    from moto.server import ThreadedMotoServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    url = f"http://127.0.0.1:{port}"
    # moto keeps its buckets in the process, from one server to the next: each starts empty.
    urllib.request.urlopen(urllib.request.Request(f"{url}/moto-api/reset", method="POST")).close()
    try:
        yield url
    finally:
        server.stop()
