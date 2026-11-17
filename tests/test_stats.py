from exporter.s3.scanner import ListPage
from exporter.stats import SIZE_BUCKETS, ListingStats

NOON = "2026-06-01T12:00:00.000Z"


def page(*objects: tuple[int, str], prefixes: int = 0, storage: str = "STANDARD") -> ListPage:
    return ListPage(
        sizes=[size for size, _ in objects],
        last_modified=[date.encode() for _, date in objects],
        common_prefixes=[b"p/"] * prefixes,
        next_token=None,
        storage_classes=[storage.encode()] * len(objects),
    )


def stats_of(*pages: ListPage) -> ListingStats:
    stats = ListingStats()
    for each in pages:
        stats.add(each)
    return stats


def test_nothing_listed_dates_at_zero() -> None:
    stats = stats_of(page(), page())
    assert (stats.objects, stats.total_size, stats.biggest_size, stats.newest_size) == (0, 0, 0, 0)
    assert (stats.newest_epoch(), stats.oldest_epoch()) == (0, 0)
    assert stats.size_buckets == [0] * len(SIZE_BUCKETS)
    assert stats.storage_classes == {}


def test_the_oldest_object_is_found_across_pages() -> None:
    stats = stats_of(page((1, "2026-06-01T12:00:00.900Z")), page((2, "2026-06-01T12:00:00.100Z")))
    assert stats.oldest_date == b"2026-06-01T12:00:00.100Z"
    assert stats.oldest_epoch() == 1780315200


def test_objects_count_in_every_size_bucket_they_fit_in() -> None:
    sizes = (0, 1024, 1025, 128 * 1024, 2 << 20, 6 << 30)
    stats = stats_of(
        page(*((size, NOON) for size in sizes[:3])), page(*((s, NOON) for s in sizes[3:]))
    )
    # Upper bounds: 0, 1 KiB, 128 KiB, 1 MiB, 16 MiB, 256 MiB, 1 GiB and 5 GiB.
    assert stats.size_buckets == [1, 2, 4, 4, 5, 5, 5, 5]


def test_objects_and_sizes_add_up_by_storage_class() -> None:
    mixed = ListPage(
        sizes=[1, 2, 4, 8],
        last_modified=[NOON.encode()] * 4,
        common_prefixes=[],
        next_token=None,
        storage_classes=[b"STANDARD", b"GLACIER", b"STANDARD", b"DEEP_ARCHIVE"],
    )
    stats = stats_of(page((16, NOON), (32, NOON)), mixed, page((64, NOON), storage="GLACIER"))
    assert stats.storage_classes == {
        b"STANDARD": [4, 53],
        b"GLACIER": [2, 66],
        b"DEEP_ARCHIVE": [1, 8],
    }


def test_the_first_of_equally_new_objects_wins_within_and_across_pages() -> None:
    noon = "2026-06-01T12:00:00.000Z"
    stats = stats_of(page((1, noon), (2, noon)), page((3, noon)))
    assert stats.newest_size == 1


def test_a_strictly_newer_object_on_a_later_page_wins() -> None:
    stats = stats_of(page((1, "2026-06-01T12:00:00.100Z")), page((2, "2026-06-01T12:00:00.900Z")))
    assert stats.newest_size == 2
    assert stats.newest_epoch() == 1780315200


def test_sums_are_exact_beyond_float_precision() -> None:
    date = "2026-11-01T00:00:00.000Z"
    stats = stats_of(page((2**53, date), (1, date)), page((2**60, date)))
    assert stats.total_size == 2**60 + 2**53 + 1
    assert stats.biggest_size == 2**60
    assert stats.objects == 3


def test_common_prefixes_add_up_over_pages() -> None:
    assert stats_of(page(prefixes=1000), page(prefixes=200)).common_prefixes == 1200


def test_merging_in_listing_order_gives_the_stats_of_one_listing() -> None:
    noon, later = "2026-06-01T12:00:00.000Z", "2026-06-01T13:00:00.000Z"
    pages = [
        page((5, noon), prefixes=2),
        page((7, noon), (1, later), storage="GLACIER"),
        page(prefixes=4),
        page((9, later), prefixes=1),
    ]
    merged = ListingStats()
    for each in pages:
        merged.merge(stats_of(each))
    assert merged == stats_of(*pages)
    assert (merged.newest_size, merged.biggest_size, merged.common_prefixes) == (1, 9, 7)
