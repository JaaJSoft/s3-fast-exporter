from collections.abc import Callable, Iterator

from prometheus_client import (
    CollectorRegistry,
    Gauge,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.core import GaugeHistogramMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from prometheus_client.utils import floatToGoString

from exporter.probe import ProbeResult
from exporter.stats import SIZE_BUCKETS, ListingStats

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
WITH_DELIMITER = ("bucket", "delimiter", "prefix")
WITHOUT_DELIMITER = ("bucket", "prefix")
BY_STORAGE_CLASS = ("bucket", "prefix", "storage_class")
# How Prometheus clients write the bounds of histogram buckets, as in le="1.048576e+06".
go_float: Callable[[float], str] = floatToGoString
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
# Families the upstream exporter does not have.
NEW_HELP = {
    "s3_list_requests": "The number of ListObjectsV2 requests the listing sent, retries included",
    "s3_list_slow_downs": "The number of ListObjectsV2 requests S3 answered with 503 SlowDown",
    "s3_object_size_bytes": "The number of objects at or under each size",
    "s3_oldest_object_date": "The last modified date of the object that was modified least "
    "recently",
    "s3_storage_class_objects": "The number of objects of each storage class",
    "s3_storage_class_size_sum_bytes": "The total size of the objects of each storage class",
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
        result = self._result
        listing, stats = result.listing, result.stats
        listed = (listing.bucket, listing.delimiter, listing.prefix)
        success = 0.0 if stats is None else 1.0
        families = [_gauge("s3_list_success", WITH_DELIMITER, listed, success)]
        if stats is not None:
            families += [
                _gauge("s3_list_duration_seconds", WITH_DELIMITER, listed, result.duration),
                _gauge("s3_list_requests", WITH_DELIMITER, listed, result.requests),
                _gauge("s3_list_slow_downs", WITH_DELIMITER, listed, result.slow_downs),
            ]
            if listing.delimiter:
                families.append(
                    _gauge("s3_common_prefixes", WITH_DELIMITER, listed, stats.common_prefixes)
                )
            else:
                families += _object_families(listing.bucket, listing.prefix, stats)
        yield from sorted(families, key=lambda family: family.name)


def _object_families(bucket: str, prefix: str, stats: ListingStats) -> list[Metric]:
    objects = (bucket, prefix)
    sizes = GaugeHistogramMetricFamily(
        "s3_object_size_bytes", NEW_HELP["s3_object_size_bytes"], labels=WITHOUT_DELIMITER
    )
    buckets = [
        *(
            (go_float(bound), count)
            for bound, count in zip(SIZE_BUCKETS, stats.size_buckets, strict=True)
        ),
        ("+Inf", stats.objects),
    ]
    sizes.add_metric(list(objects), buckets, stats.total_size)
    storage_objects = GaugeMetricFamily(
        "s3_storage_class_objects", NEW_HELP["s3_storage_class_objects"], labels=BY_STORAGE_CLASS
    )
    storage_sizes = GaugeMetricFamily(
        "s3_storage_class_size_sum_bytes",
        NEW_HELP["s3_storage_class_size_sum_bytes"],
        labels=BY_STORAGE_CLASS,
    )
    for storage_class, (count, size) in sorted(stats.storage_classes.items()):
        labels = [bucket, prefix, storage_class.decode(errors="replace")]
        storage_objects.add_metric(labels, float(count))
        storage_sizes.add_metric(labels, float(size))
    return [
        _gauge("s3_biggest_object_size_bytes", WITHOUT_DELIMITER, objects, stats.biggest_size),
        _gauge("s3_last_modified_object_date", WITHOUT_DELIMITER, objects, stats.newest_epoch()),
        _gauge("s3_last_modified_object_size_bytes", WITHOUT_DELIMITER, objects, stats.newest_size),
        _gauge("s3_objects", WITHOUT_DELIMITER, objects, stats.objects),
        _gauge("s3_objects_size_sum_bytes", WITHOUT_DELIMITER, objects, stats.total_size),
        _gauge("s3_oldest_object_date", WITHOUT_DELIMITER, objects, stats.oldest_epoch()),
        sizes,
        storage_objects,
        storage_sizes,
    ]


def _gauge(name: str, names: tuple[str, ...], labels: tuple[str, ...], value: float) -> Metric:
    gauge = GaugeMetricFamily(name, HELP.get(name) or NEW_HELP[name], labels=names)
    gauge.add_metric(labels, float(value))
    return gauge
