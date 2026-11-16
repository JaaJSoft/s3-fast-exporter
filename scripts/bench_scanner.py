"""Time the ListObjectsV2 page scanner against botocore's parser on a 1000-object page.

Run from the repository root: uv run python -m scripts.bench_scanner
"""

import platform
import timeit
from collections.abc import Callable

from botocore import parsers
from botocore.handlers import decode_list_object_v2
from botocore.session import get_session

from exporter.s3.scanner import scan_list_page
from fakes3 import responses
from fakes3.app import encode_token
from fakes3.golden import golden_buckets
from fakes3.store import list_page


def main() -> None:
    bucket = golden_buckets()["golden"]
    page = list_page(bucket, prefix="many/", delimiter="", after="", max_keys=1000)
    assert page.resume_after is not None  # noqa: S101
    body = responses.list_objects_v2(
        bucket="golden",
        prefix="many/",
        delimiter="",
        max_keys=1000,
        url_encoded=True,
        start_after="",
        continuation_token="",
        next_token=encode_token(page.resume_after),
        fetch_owner=False,
        page=page,
    ).encode()
    operation = get_session().get_service_model("s3").operation_model("ListObjectsV2")

    def botocore_parse() -> None:
        response = {"body": body, "headers": {}, "status_code": 200}
        parsed = parsers.RestXMLParser().parse(response, operation.output_shape)
        decode_list_object_v2(parsed, context={"encoding_type_auto_set": True})

    def scan() -> None:
        scan_list_page(body)

    runs: tuple[tuple[str, Callable[[], None], int], ...] = (
        ("scanner", scan, 200),
        ("botocore", botocore_parse, 10),
    )
    print(f"{platform.python_implementation()} {platform.python_version()}")
    for name, run, number in runs:
        best = min(timeit.repeat(run, number=number, repeat=7)) / number
        print(
            f"  {name:<9} {best * 1000:6.2f} ms per page of 1000 objects ({len(body) // 1024} KiB)"
        )


if __name__ == "__main__":
    main()
