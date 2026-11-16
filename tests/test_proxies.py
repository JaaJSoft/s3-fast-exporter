import pytest
from yarl import URL

from exporter.s3.proxies import Proxies

ENVIRONMENT = {
    "http": "http://proxy:3128",
    "https": "http://user:p%40ss@proxy:3128",
    "no": "localhost, .internal,minio:9000",
}
PROXY = URL("http://proxy:3128")


@pytest.mark.parametrize(
    ("url", "proxy"),
    [
        (
            "https://bucket.s3.eu-west-1.amazonaws.com/?list-type=2",
            URL("http://user:p%40ss@proxy:3128"),
        ),
        ("http://s3.example.com/bucket", PROXY),
        ("http://localhost:9000/bucket", None),
        ("https://s3.internal/bucket", None),
        ("https://internal/bucket", None),
        ("http://notinternal/bucket", PROXY),
        ("http://minio:9000/bucket", None),
        ("http://minio:9001/bucket", PROXY),
    ],
)
def test_hosts_get_the_proxy_the_environment_names(url: str, proxy: URL | None) -> None:
    proxies = Proxies(ENVIRONMENT)
    assert proxies(URL(url)) == proxy
    assert proxies(URL(url)) == proxy


def test_an_asterisk_in_no_proxy_bypasses_every_host() -> None:
    assert Proxies({"https": "http://proxy:3128", "no": "*"})(URL("https://s3/")) is None


def test_nothing_is_proxied_without_proxy_variables() -> None:
    assert Proxies({})(URL("https://s3/")) is None
    assert Proxies({"ftp": "http://proxy:21"})(URL("https://s3/")) is None
