"""The exporter, served over HTTP, answers the golden probes as upstream did."""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
from botocore.session import Session
from exposition_diff import differences, parse_exposition
from servers import serve_app

from exporter import __version__
from exporter.app import create_app
from exporter.config import parse_settings
from exporter.exposition import NEW_HELP
from fakes3.app import create_app as create_fakes3
from fakes3.golden import PROBES, Probe, golden_buckets

GOLDEN = Path(__file__).parent / "golden" / "upstream"
# The two deliberate differences with upstream:
# an empty listing dates its newest object at 0, not at the overflow of Go's zero time,
UPSTREAM_EMPTY_DATE = "-6.795364578e+09"
# and a failed listing reports its failure instead of crashing the exporter.
FAILED_LISTING = """\
# HELP s3_list_success If the ListObjects operation was a success
# TYPE s3_list_success gauge
s3_list_success{{bucket="{bucket}",delimiter="",prefix=""}} 0
"""
# Families of this exporter only: the size histogram exposes its count and sum as gauges.
NEW_FAMILIES = frozenset({*NEW_HELP, "s3_object_size_bytes_gcount", "s3_object_size_bytes_gsum"})


@pytest.fixture(scope="module")
def exporter(aws_session: Session) -> Iterator[str]:
    """In-process by default; PARITY_EXPORTER_URL points the same tests at a running image."""
    if url := os.environ.get("PARITY_EXPORTER_URL"):
        yield url
        return
    with serve_app(create_fakes3(golden_buckets())) as fakes3:
        settings = parse_settings(["--s3.endpoint-url", fakes3, "--s3.force-path-style"], {})
        with serve_app(create_app(settings, aws_session)) as url:
            yield url


def get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def probe_url(exporter: str, probe: Probe) -> str:
    query = {"bucket": probe.bucket, "prefix": probe.prefix, "delimiter": probe.delimiter}
    return f"{exporter}/probe?{urlencode({key: value for key, value in query.items() if value})}"


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_probes_answer_as_upstream(exporter: str, probe: Probe) -> None:
    upstream = (GOLDEN / f"{probe.name}.prom").read_text(encoding="utf-8")
    status, body = get(probe_url(exporter, probe))
    assert status == 200
    assert differences(upstream.replace(UPSTREAM_EMPTY_DATE, "0"), body, new=NEW_FAMILIES) == []


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_new_families_agree_with_the_upstream_ones(exporter: str, probe: Probe) -> None:
    status, body = get(probe_url(exporter, probe))
    assert status == 200
    families = parse_exposition(body)

    def value(name: str) -> float:
        [only] = families[name].samples.values()
        return only

    assert value("s3_list_requests") >= 1
    assert value("s3_list_slow_downs") == 0
    if probe.delimiter:
        assert families.keys() & {"s3_objects", "s3_object_size_bytes"} == set()
        return
    objects, size = value("s3_objects"), value("s3_objects_size_sum_bytes")
    buckets = families["s3_object_size_bytes"].samples
    counts = [count for labels, count in buckets.items() if dict(labels)["le"] != "+Inf"]
    assert counts == sorted(counts)
    assert max(buckets.values()) == objects == value("s3_object_size_bytes_gcount")
    assert value("s3_object_size_bytes_gsum") == size
    assert sum(families["s3_storage_class_objects"].samples.values()) == objects
    assert sum(families["s3_storage_class_size_sum_bytes"].samples.values()) == size
    newest, oldest = value("s3_last_modified_object_date"), value("s3_oldest_object_date")
    assert 0 <= oldest <= newest
    assert (oldest == 0) == (objects == 0)


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if p.crashes_upstream], ids=lambda probe: probe.name
)
def test_a_failed_listing_reports_its_failure(exporter: str, probe: Probe) -> None:
    status, body = get(probe_url(exporter, probe))
    assert status == 200
    assert differences(FAILED_LISTING.format(bucket=probe.bucket), body) == []


def test_a_probe_without_bucket_is_rejected_as_upstream(exporter: str) -> None:
    status, body = get(f"{exporter}/probe")
    assert f"{status}\n{body}" == (GOLDEN / "probe-without-bucket.txt").read_text(encoding="utf-8")


def test_discovery_answers_as_upstream(exporter: str) -> None:
    status, body = get(f"{exporter}/discovery", {"Host": "127.0.0.1:9340"})
    assert status == 200
    assert json.loads(body) == json.loads((GOLDEN / "discovery.json").read_text(encoding="utf-8"))


def test_metrics_expose_the_build_info(exporter: str) -> None:
    status, body = get(f"{exporter}/metrics")
    assert status == 200
    assert f's3_exporter_build_info{{version="{__version__}"}} 1.0' in body


def test_health_endpoint(exporter: str) -> None:
    assert get(f"{exporter}/-/healthy") == (200, "OK\n")
