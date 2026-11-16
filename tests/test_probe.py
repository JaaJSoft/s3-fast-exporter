from exporter.probe import ListingStats
from exporter.s3.scanner import ListPage


def page(*objects: tuple[int, str], prefixes: int = 0) -> ListPage:
    return ListPage(
        sizes=[size for size, _ in objects],
        last_modified=[date.encode() for _, date in objects],
        common_prefixes=[b"p/"] * prefixes,
        next_token=None,
    )


def stats_of(*pages: ListPage) -> ListingStats:
    stats = ListingStats()
    for each in pages:
        stats.add(each)
    return stats


def test_nothing_listed_dates_at_zero() -> None:
    stats = stats_of(page(), page())
    assert (stats.objects, stats.total_size, stats.biggest_size, stats.newest_size) == (0, 0, 0, 0)
    assert stats.newest_epoch() == 0


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
