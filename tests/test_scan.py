import asyncio
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from listings import StoreSource

from exporter.s3.scanner import ListPage, ProtocolError
from exporter.scan import (
    MIN_PAGES_PER_RANGE,
    Listing,
    Scan,
    balanced_bounds,
    discover_bounds,
    scan,
)
from fakes3.store import Bucket, StoredObject, common_prefix

NOON = datetime(2026, 6, 1, 12, tzinfo=UTC)
LISTING = Listing("bucket")

# Few characters, so that keys, groups and bounds collide; "0" follows "/", and "+", " " and the
# emoji change order once URL-encoded.
names = st.text(st.sampled_from("ab0/ +é🙂"), min_size=1, max_size=5)
objects = st.lists(
    st.tuples(names, st.integers(0, 4), st.integers(0, 3)), max_size=30, unique_by=lambda o: o[0]
)
prefixes = st.sampled_from(["", "a", "a/", "é"])
delimiters = st.sampled_from(["", "/", "0/", "a"])


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.Runner]:
    # One event loop for all examples: on Windows, each new one opens a TCP connection to itself,
    # and thousands of them in a row exhaust the ephemeral ports.
    with asyncio.Runner() as runner:
        yield runner


def bucket_of(objects: list[tuple[str, int, int]]) -> Bucket:
    return Bucket(
        "bucket",
        (StoredObject(key, size, NOON + timedelta(minutes=age)) for key, size, age in objects),
    )


def named(*keys: str) -> Bucket:
    return bucket_of([(key, len(key), 0) for key in keys])


def groups_of(bucket: Bucket, listing: Listing) -> set[str]:
    keys = (obj.key for obj in bucket.objects if obj.key.startswith(listing.prefix))
    groups = (common_prefix(key, listing.prefix, listing.delimiter) for key in keys)
    return {group for group in groups if group is not None}


@settings(max_examples=500, deadline=None)
@given(objects, prefixes, delimiters, st.lists(names, max_size=5), st.integers(1, 3))
# A key named like the end of the group before it, "0" after "/": the range ending there must
# still list it.
@example([("/", 0, 0), ("0", 0, 0)], "", "/", ["0"], 1)
def test_any_bounds_give_the_result_of_a_single_listing(
    loop: asyncio.Runner,
    objects: list[tuple[str, int, int]],
    prefix: str,
    delimiter: str,
    bounds: list[str],
    max_keys: int,
) -> None:
    source = StoreSource(bucket_of(objects), max_keys=max_keys)
    listing = Listing("bucket", prefix, delimiter)
    single = loop.run(scan(source, listing))
    split = loop.run(scan(source, listing, bounds))
    assert split.stats == single.stats
    assert split.entries == single.entries


@settings(max_examples=300, deadline=None)
@given(objects, prefixes, delimiters, st.integers(1, 2), st.integers(2, 6))
def test_learned_bounds_never_fall_inside_a_group(
    loop: asyncio.Runner,
    objects: list[tuple[str, int, int]],
    prefix: str,
    delimiter: str,
    max_keys: int,
    ranges: int,
) -> None:
    # S3 does not document whether StartAfter inside a group, or equal to its prefix, lists the
    # group again. Bounds outside of every group make the question moot.
    bucket = bucket_of(objects)
    source = StoreSource(bucket, max_keys=max_keys)
    listing = Listing("bucket", prefix, delimiter)
    groups = groups_of(bucket, listing)
    bounds: list[str] = []
    for _ in range(3):
        result = loop.run(scan(source, listing, bounds))
        bounds = balanced_bounds(result, ranges)
        assert [bound for bound in bounds if any(map(bound.startswith, groups))] == []


def test_learned_ranges_are_even_and_cost_no_extra_request(loop: asyncio.Runner) -> None:
    source = StoreSource(named(*(f"k{number:04d}" for number in range(100))), max_keys=2)
    single = loop.run(scan(source, LISTING))
    assert (source.requests, single.pages) == (50, 50)

    bounds = balanced_bounds(single, 4)
    assert bounds == ["k0023", "k0049", "k0073"]
    source.requests = 0
    split = loop.run(scan(source, LISTING, bounds))
    assert source.requests == 50
    assert split.stats == single.stats
    assert balanced_bounds(split, 4) == bounds


def test_listings_of_few_pages_are_not_split(loop: asyncio.Runner) -> None:
    source = StoreSource(named(*"abcdefgh"), max_keys=1)
    result = loop.run(scan(source, LISTING))
    assert result.pages == 2 * MIN_PAGES_PER_RANGE
    assert balanced_bounds(result, 8) == ["d"]
    result.pages -= 1
    assert balanced_bounds(result, 8) == []
    assert balanced_bounds(Scan(), 8) == []


@pytest.mark.parametrize(
    ("keys", "max_keys", "expected"),
    [
        pytest.param(
            [f"{letter}/1" for letter in "abcdefgh"], 1000, ["c/", "e/", "g/"], id="top-groups"
        ),
        pytest.param(
            ["README", "data/x/1", "data/y/1", "data/z/1"],
            1000,
            ["data/y/", "data/z/"],
            id="single-group-descended",
        ),
        pytest.param(["1", "2", "3"], 1000, [], id="no-group"),
        pytest.param(["a/b/c/d/1", "a/b/c/e/1"], 1000, [], id="deeper-than-discovery"),
        pytest.param(
            [f"{letter}/1" for letter in "abcde"], 2, ["b/"], id="first-page-of-a-wide-level"
        ),
    ],
)
def test_first_scans_split_on_the_top_groups(
    loop: asyncio.Runner, keys: list[str], max_keys: int, expected: list[str]
) -> None:
    source = StoreSource(named(*keys), max_keys=max_keys)
    assert loop.run(discover_bounds(source, LISTING, 4)) == expected


class IgnoringStartAfter(StoreSource):
    def pages(
        self, bucket: str, *, prefix: str = "", delimiter: str = "", start_after: str = ""
    ) -> AsyncGenerator[ListPage, None]:
        return super().pages(bucket, prefix=prefix, delimiter=delimiter)


def test_a_server_ignoring_start_after_fails_the_scan(loop: asyncio.Runner) -> None:
    source = IgnoringStartAfter(named("a", "b", "c"))
    with pytest.raises(ProtocolError, match="StartAfter"):
        loop.run(scan(source, LISTING, ["b"]))
