import socket
import threading
import time
import urllib.request

from botocore.session import Session
from servers import serve_app

from exporter.app import create_app
from exporter.config import parse_settings


def test_shutting_down_does_not_wait_for_the_scans_in_flight(aws_session: Session) -> None:
    # The kernel completes connections to a listening socket that nobody accepts: requests to
    # this S3 are sent, and never answered.
    with socket.create_server(("127.0.0.1", 0)) as silent_s3:
        host, port = silent_s3.getsockname()[:2]
        settings = parse_settings(
            ["--s3.endpoint-url", f"http://{host}:{port}", "--s3.force-path-style"], {}
        )
        answers: list[object] = []

        def probe(url: str) -> None:
            try:
                with urllib.request.urlopen(f"{url}/probe?bucket=bucket", timeout=120) as answer:
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
