import socket
import threading
import time
import urllib.request

import pytest
from botocore.session import Session
from servers import serve_app

from exporter.app import SCRAPE_TIMEOUT_HEADER, create_app, scrape_timeout
from exporter.config import parse_settings


@pytest.mark.parametrize("processes", [1, 2])
def test_shutting_down_does_not_wait_for_the_scans_in_flight(
    aws_session: Session, monkeypatch: pytest.MonkeyPatch, processes: int
) -> None:
    # Worker processes resolve their own credentials.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDFAKES3")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fakes3-secret")
    # The kernel completes connections to a listening socket that nobody accepts: requests to
    # this S3 are sent, and never answered.
    with socket.create_server(("127.0.0.1", 0)) as silent_s3:
        host, port = silent_s3.getsockname()[:2]
        settings = parse_settings(
            [
                f"--s3.endpoint-url=http://{host}:{port}",
                "--s3.force-path-style",
                f"--s3.list-processes={processes}",
            ],
            {},
        )
        answers: list[object] = []

        def probe(url: str) -> None:
            # With a delimiter, the scan lists at once, in a worker when there are workers.
            query = "bucket=bucket&delimiter=/"
            try:
                with urllib.request.urlopen(f"{url}/probe?{query}", timeout=120) as answer:
                    answers.append(answer.status)
            except OSError as error:
                answers.append(error)

        with serve_app(create_app(settings, aws_session)) as url:
            prober = threading.Thread(target=probe, args=(url,))
            prober.start()
            time.sleep(1)
            assert answers == []
            stopping = time.perf_counter()
        assert time.perf_counter() - stopping < 10
        prober.join(10)
        assert len(answers) == 1


def test_a_probe_answers_before_the_scrape_timeout_of_prometheus(aws_session: Session) -> None:
    with socket.create_server(("127.0.0.1", 0)) as silent_s3:
        host, port = silent_s3.getsockname()[:2]
        settings = parse_settings(
            ["--s3.endpoint-url", f"http://{host}:{port}", "--s3.force-path-style"], {}
        )
        with serve_app(create_app(settings, aws_session)) as url:
            request = urllib.request.Request(
                f"{url}/probe?bucket=bucket", headers={SCRAPE_TIMEOUT_HEADER: "1"}
            )
            started = time.perf_counter()
            with urllib.request.urlopen(request, timeout=10) as answer:
                body = answer.read().decode()
            elapsed = time.perf_counter() - started
    assert 0.5 <= elapsed < 1
    assert 's3_list_success{bucket="bucket",delimiter="",prefix=""} 0.0' in body


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("10", 9.5), ("2.5", 2.0), ("0.6", 0.3), (None, None), ("", None), ("soon", None),
     ("0", None), ("-3", None), ("inf", None), ("nan", None)],
)  # fmt: skip
def test_probes_take_the_scrape_timeout_minus_a_margin(
    header: str | None, seconds: float | None
) -> None:
    assert scrape_timeout(header) == seconds
