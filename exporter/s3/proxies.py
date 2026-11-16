from collections.abc import Mapping

from yarl import URL


class Proxies:
    """The proxy of each host, as HTTP_PROXY, HTTPS_PROXY and NO_PROXY name them.

    aiohttp's trust_env reads them, and ~/.netrc, in a worker thread for every request: these
    are read once. A presigned URL carries its own authentication, which ~/.netrc must not add to.
    """

    def __init__(self, environment: Mapping[str, str]) -> None:
        """environment: as urllib.request.getproxies_environment() gives it."""
        self._addresses = {
            scheme: URL(address)
            for scheme, address in environment.items()
            if scheme in ("http", "https")
        }
        self._no_proxy = [
            name for name in environment.get("no", "").lower().replace(" ", "").split(",") if name
        ]
        self._proxies: dict[tuple[str, str, int | None], URL | None] = {}

    def __call__(self, url: URL) -> URL | None:
        """The proxy for this URL, with the credentials aiohttp sends it, if any."""
        if not self._addresses:
            return None
        host = (url.scheme, url.host or "", url.explicit_port)
        if host not in self._proxies:
            bypassed = self._bypassed(host[1], host[2])
            self._proxies[host] = None if bypassed else self._addresses.get(url.scheme)
        return self._proxies[host]

    def _bypassed(self, host: str, port: int | None) -> bool:
        names = (host,) if port is None else (host, f"{host}:{port}")
        for entry in self._no_proxy:
            if entry == "*":
                return True
            domain = entry.lstrip(".")
            if any(name == domain or name.endswith(f".{domain}") for name in names):
                return True
        return False
