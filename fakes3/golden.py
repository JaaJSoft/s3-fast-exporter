from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fakes3.store import Bucket, StoredObject

FIVE_TIB = 5 * 2**40
NOVEMBER = datetime(2026, 11, 1, tzinfo=UTC)
SPECIAL_KEYS = (
    "special/with space.txt",
    "special/plus+sign",
    "special/percent%25",
    "special/amp&ersand",
    "special/less<than>greater",
    'special/quote"double',
    "special/apostrophe'",
    "special/é-accent",
    "special/emoji-🙂",
    "special/question?mark",
    "special/hash#tag",
    "special/back\\slash",
)


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    bucket: str
    prefix: str = ""
    delimiter: str = ""
    crashes_upstream: bool = False


PROBES = (
    Probe("empty-bucket", "empty"),
    Probe("single-object", "single"),
    Probe("all-objects", "golden"),
    Probe("prefix", "golden", prefix="logs/"),
    Probe("prefix-is-not-a-path", "golden", prefix="logs"),
    Probe("prefix-without-match", "golden", prefix="nothing-here/"),
    Probe("prefix-with-space", "golden", prefix="special/with sp"),
    Probe("prefix-with-quote", "golden", prefix='special/quote"'),
    Probe("prefix-with-unicode", "golden", prefix="special/é"),
    Probe("same-date", "golden", prefix="ties/"),
    Probe("subsecond-dates", "golden", prefix="subsecond/"),
    Probe("zero-size", "golden", prefix="zero/"),
    Probe("huge-sizes", "golden", prefix="huge/"),
    Probe("special-keys", "golden", prefix="special/"),
    Probe("pagination", "golden", prefix="many/"),
    Probe("delimiter-root", "golden", delimiter="/"),
    Probe("delimiter", "golden", prefix="data/", delimiter="/"),
    Probe("delimiter-pagination", "golden", prefix="delim-many/", delimiter="/"),
    Probe("delimiter-without-match", "golden", prefix="nothing-here/", delimiter="/"),
    Probe("delimiter-multichar", "golden", prefix="logs/", delimiter="/11/"),
    Probe("missing-bucket", "missing", crashes_upstream=True),
)


def golden_buckets() -> dict[str, Bucket]:
    buckets = (
        Bucket("empty", ()),
        Bucket(
            "single", (StoredObject("backup.tar.gz", 1234, datetime(2019, 6, 13, 21, tzinfo=UTC)),)
        ),
        Bucket("golden", _golden_objects()),
    )
    return {bucket.name: bucket for bucket in buckets}


def _golden_objects() -> list[StoredObject]:
    objects = [StoredObject("root.txt", 7, NOVEMBER)]
    objects += (
        StoredObject(
            f"logs/2026/11/{day:02d}/app.log",
            1_000 + day * 37 % 500,
            NOVEMBER + timedelta(days=day - 1, hours=23),
        )
        for day in range(1, 31)
    )
    objects += (
        StoredObject(
            f"logs-archive/2026-{month:02d}.tar.gz",
            50_000_000 + month,
            datetime(2026, month, 28, tzinfo=UTC),
            "GLACIER",
        )
        for month in range(1, 11)
    )
    objects += (
        StoredObject(
            f"data/{part}/part-{n}.parquet", 4_096 * (n + 1), NOVEMBER + timedelta(hours=n)
        )
        for part in "abc"
        for n in range(3)
    )
    objects.append(StoredObject("data/readme.txt", 42, NOVEMBER))
    objects += (StoredObject(f"delim-many/p{n:04d}/object", 1, NOVEMBER) for n in range(1_200))
    objects += (
        StoredObject(
            f"many/{n:05d}",
            1_000_000 if n == 1_500 else 100 + n % 7,
            NOVEMBER + timedelta(days=365 if n == 2_400 else 0, seconds=n),
        )
        for n in range(2_500)
    )
    noon = datetime(2026, 6, 1, 12, tzinfo=UTC)
    objects += (StoredObject("ties/a", 10, noon), StoredObject("ties/b", 20, noon))
    objects += (
        StoredObject("subsecond/a", 1, noon + timedelta(milliseconds=100)),
        StoredObject("subsecond/b", 2, noon + timedelta(milliseconds=900)),
    )
    objects += (
        StoredObject(f"zero/{name}", 0, NOVEMBER + timedelta(hours=n))
        for n, name in enumerate("abc")
    )
    objects += (
        StoredObject(f"huge/{n:04d}", FIVE_TIB - n, NOVEMBER + timedelta(minutes=n))
        for n in range(2_000)
    )
    # An odd total above 2**53 forces the float64 conversion to round.
    objects.append(StoredObject("huge/odd", 1, NOVEMBER))
    objects += (
        StoredObject(key, 100 + n, NOVEMBER + timedelta(minutes=n))
        for n, key in enumerate(SPECIAL_KEYS)
    )
    return objects
