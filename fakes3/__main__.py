import argparse

from aiohttp import web

from fakes3.app import create_app
from fakes3.golden import golden_buckets


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fakes3", description="Deterministic S3 server for tests and benchmarks."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    web.run_app(
        create_app(golden_buckets()), host=args.host, port=args.port, print=None, access_log=None
    )


if __name__ == "__main__":
    main()
