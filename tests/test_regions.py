import asyncio
from typing import Any, cast

import pytest

from exporter.s3.lister import S3Error
from exporter.s3.presigner import Presigner
from exporter.s3.regions import Regions


class Recorder:
    def __init__(self, regions: dict[str, str | None] | None = None) -> None:
        self.regions = regions or {}
        self.created: list[str] = []
        self.looked_up: list[str] = []

    def new_presigner(self, region: str) -> Presigner:
        self.created.append(region)
        return cast(Presigner, f"presigner:{region}")

    def lookup(self, bucket: str) -> str | None:
        self.looked_up.append(bucket)
        if bucket not in self.regions:
            raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist")
        return self.regions[bucket]


def presigners(regions: Regions, *buckets: str) -> list[Any]:
    async def run() -> list[Any]:
        return list(await asyncio.gather(*(regions.presigner_for(b) for b in buckets)))

    return asyncio.run(run())


def test_without_lookup_every_bucket_uses_the_default_region() -> None:
    recorder = Recorder()
    regions = Regions(recorder.new_presigner, "eu-west-3")
    assert presigners(regions, "a", "b", "a") == ["presigner:eu-west-3"] * 3
    assert recorder.created == ["eu-west-3"]


def test_each_bucket_is_looked_up_once_and_each_region_gets_one_presigner() -> None:
    recorder = Recorder({"paris": "eu-west-3", "dublin": "eu-west-1", "lyon": "eu-west-3"})
    regions = Regions(recorder.new_presigner, "us-east-1", recorder.lookup)
    found = presigners(regions, "paris", "dublin", "lyon", "paris", "paris")
    assert found == ["presigner:eu-west-3", "presigner:eu-west-1"] + ["presigner:eu-west-3"] * 3
    assert sorted(recorder.created) == ["eu-west-1", "eu-west-3"]
    assert sorted(recorder.looked_up) == ["dublin", "lyon", "paris"]
    presigners(regions, "paris", "lyon")
    assert len(recorder.looked_up) == 3


def test_a_bucket_without_region_header_uses_the_default_region() -> None:
    recorder = Recorder({"legacy": None})
    regions = Regions(recorder.new_presigner, "us-east-1", recorder.lookup)
    assert presigners(regions, "legacy") == ["presigner:us-east-1"]


def test_failed_lookups_are_not_cached_and_forgotten_buckets_are_looked_up_again() -> None:
    recorder = Recorder({"paris": "eu-west-3"})
    regions = Regions(recorder.new_presigner, "us-east-1", recorder.lookup)
    with pytest.raises(S3Error):
        presigners(regions, "missing")
    with pytest.raises(S3Error):
        presigners(regions, "missing")
    presigners(regions, "paris")
    regions.forget("paris")
    presigners(regions, "paris")
    assert recorder.looked_up == ["missing", "missing", "paris", "paris"]


def test_regions_are_kept_for_the_buckets_probed_last() -> None:
    recorder = Recorder({"a": "eu-west-1", "b": "eu-west-2", "c": "eu-west-3"})
    regions = Regions(recorder.new_presigner, lookup=recorder.lookup, remembered=2)

    async def run() -> None:
        for bucket in "abacba":
            await regions.region_of(bucket)

    asyncio.run(run())
    assert recorder.looked_up == ["a", "b", "c", "b", "a"]
