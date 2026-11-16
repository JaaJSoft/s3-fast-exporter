"""The recorded upstream outputs, checked against a model of what the exporter must compute."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from exposition_diff import Labels, parse_exposition

from fakes3.golden import PROBES, Probe, golden_buckets
from fakes3.store import common_prefix

GOLDEN = Path(__file__).parent / "golden" / "upstream"
BUCKETS = golden_buckets()
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Upstream turns Go's zero time into seconds through an int64 overflow when nothing is listed.
UPSTREAM_EMPTY_DATE = -6795364578.0

type Samples = dict[tuple[str, Labels], float]


def model(probe: Probe) -> Samples:
    objects = [obj for obj in BUCKETS[probe.bucket].objects if obj.key.startswith(probe.prefix)]
    three: Labels = (
        ("bucket", probe.bucket),
        ("delimiter", probe.delimiter),
        ("prefix", probe.prefix),
    )
    two: Labels = (("bucket", probe.bucket), ("prefix", probe.prefix))
    samples: Samples = {("s3_list_success", three): 1.0}
    if probe.delimiter:
        prefixes = {common_prefix(obj.key, probe.prefix, probe.delimiter) for obj in objects}
        samples["s3_common_prefixes", three] = float(len(prefixes - {None}))
        return samples
    newest = None
    for obj in objects:
        if newest is None or obj.last_modified > newest.last_modified:
            newest = obj
    samples["s3_objects", two] = float(len(objects))
    samples["s3_objects_size_sum_bytes", two] = float(sum(obj.size for obj in objects))
    samples["s3_biggest_object_size_bytes", two] = float(max((o.size for o in objects), default=0))
    samples["s3_last_modified_object_size_bytes", two] = float(newest.size if newest else 0)
    samples["s3_last_modified_object_date", two] = (
        float((newest.last_modified - EPOCH) // timedelta(seconds=1)) if newest else 0.0
    )
    return samples


def recorded(probe: Probe) -> Samples:
    families = parse_exposition((GOLDEN / f"{probe.name}.prom").read_text(encoding="utf-8"))
    return {
        (name, labels): value
        for name, family in families.items()
        for labels, value in family.samples.items()
        if name != "s3_list_duration_seconds"
    }


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_upstream_output_matches_the_model(probe: Probe) -> None:
    expected = model(probe)
    date = ("s3_last_modified_object_date", (("bucket", probe.bucket), ("prefix", probe.prefix)))
    if expected.get(date) == 0.0:
        expected[date] = UPSTREAM_EMPTY_DATE
    assert recorded(probe) == expected


@pytest.mark.parametrize(
    "probe", [p for p in PROBES if not p.crashes_upstream], ids=lambda probe: probe.name
)
def test_upstream_reports_a_duration_per_probe(probe: Probe) -> None:
    families = parse_exposition((GOLDEN / f"{probe.name}.prom").read_text(encoding="utf-8"))
    labels = (("bucket", probe.bucket), ("delimiter", probe.delimiter), ("prefix", probe.prefix))
    assert families["s3_list_duration_seconds"].samples.keys() == {labels}


def test_upstream_crashes_when_listing_fails() -> None:
    manifest = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))
    crashing = {probe.name for probe in PROBES if probe.crashes_upstream}
    assert manifest["crashes"].keys() == crashing
    for panic in manifest["crashes"].values():
        assert panic.startswith("panic: inconsistent label cardinality")


def test_upstream_discovery_lists_every_bucket() -> None:
    targets = json.loads((GOLDEN / "discovery.json").read_text(encoding="utf-8"))
    assert targets == [
        {"targets": ["127.0.0.1:9340"], "labels": {"__param_bucket": name}}
        for name in sorted(BUCKETS)
    ]


def test_upstream_rejects_a_probe_without_bucket() -> None:
    answer = (GOLDEN / "probe-without-bucket.txt").read_text(encoding="utf-8")
    assert answer == "400\nbucket parameter is missing\n"
