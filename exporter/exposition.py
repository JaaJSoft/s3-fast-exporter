from collections.abc import Iterator

from prometheus_client import (
    CollectorRegistry,
    Gauge,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from exporter.probe import ProbeResult

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
WITH_DELIMITER = ("bucket", "delimiter", "prefix")
WITHOUT_DELIMITER = ("bucket", "prefix")
# Word for word from the upstream exporter: HELP texts are part of its metrics.
HELP = {
    "s3_biggest_object_size_bytes": "The size of the biggest object",
    "s3_common_prefixes": "A count of all the keys between the prefix and the next occurrence of "
    "the string specified by the delimiter",
    "s3_last_modified_object_date": "The last modified date of the object that was modified most "
    "recently",
    "s3_last_modified_object_size_bytes": "The size of the object that was modified most recently",
    "s3_list_duration_seconds": "The total duration of the list operation",
    "s3_list_success": "If the ListObjects operation was a success",
    "s3_objects": "The total number of objects for the bucket/prefix combination",
    "s3_objects_size_sum_bytes": "The total size of all objects summed",
}


def probe_exposition(result: ProbeResult) -> bytes:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(ProbeCollector(result))
    return generate_latest(registry)


def exporter_registry(version: str) -> CollectorRegistry:
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    build_info = Gauge(
        "s3_exporter_build_info",
        "A metric with a constant '1' value labeled by the version of s3-fast-exporter.",
        ["version"],
        registry=registry,
    )
    build_info.labels(version).set(1)
    return registry


class ProbeCollector(Collector):
    def __init__(self, result: ProbeResult) -> None:
        self._result = result

    def collect(self) -> Iterator[Metric]:
        listing, stats = self._result.listing, self._result.stats
        with_delimiter = (listing.bucket, listing.delimiter, listing.prefix)
        without_delimiter = (listing.bucket, listing.prefix)
        samples: list[tuple[str, tuple[str, ...], float]] = [
            ("s3_list_success", with_delimiter, 0.0 if stats is None else 1.0)
        ]
        if stats is not None:
            samples.append(("s3_list_duration_seconds", with_delimiter, self._result.duration))
            if listing.delimiter:
                samples.append(("s3_common_prefixes", with_delimiter, stats.common_prefixes))
            else:
                samples += [
                    ("s3_biggest_object_size_bytes", without_delimiter, stats.biggest_size),
                    ("s3_last_modified_object_date", without_delimiter, stats.newest_epoch()),
                    ("s3_last_modified_object_size_bytes", without_delimiter, stats.newest_size),
                    ("s3_objects", without_delimiter, stats.objects),
                    ("s3_objects_size_sum_bytes", without_delimiter, stats.total_size),
                ]
        for name, labels, value in sorted(samples):
            names = WITH_DELIMITER if len(labels) == len(WITH_DELIMITER) else WITHOUT_DELIMITER
            gauge = GaugeMetricFamily(name, HELP[name], labels=names)
            gauge.add_metric(labels, float(value))
            yield gauge
