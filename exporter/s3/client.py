import ssl

import aiohttp
from botocore.session import Session

MIN_CONNECTIONS = 64
TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)


def http_client(
    session: Session, concurrency: int, *, verify_tls: bool = True
) -> aiohttp.ClientSession:
    """The HTTP client of the listings, trusting the CA bundle of the AWS configuration."""
    tls: ssl.SSLContext | bool = False
    if verify_tls:
        tls = ssl.create_default_context(cafile=session.get_config_variable("ca_bundle"))
    # Room for two probes at full speed; more probes share the connections.
    connections = max(MIN_CONNECTIONS, 2 * concurrency)
    connector = aiohttp.TCPConnector(limit=connections, ttl_dns_cache=300, ssl=tls)
    return aiohttp.ClientSession(connector=connector, timeout=TIMEOUT)
