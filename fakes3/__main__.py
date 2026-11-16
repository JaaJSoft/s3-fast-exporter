import argparse
import multiprocessing
import sys
from functools import partial

from fakes3.app import InFlight, RequestCounter, SlowDown
from fakes3.server import VIRTUAL_BUCKET, serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fakes3", description="Deterministic S3 server for tests and benchmarks."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--latency-ms", type=float, default=0.0, help="Time S3 takes to answer any request."
    )
    parser.add_argument(
        "--virtual-objects",
        type=int,
        default=0,
        help=f"Objects of a bucket named {VIRTUAL_BUCKET}, made up as they are listed.",
    )
    parser.add_argument(
        "--slow-down-above",
        type=int,
        default=0,
        help="Requests in flight past which S3 answers 503 SlowDown; 0 never does.",
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Processes sharing the port, on Linux."
    )
    args = parser.parse_args()
    if args.workers > 1 and not sys.platform.startswith("linux"):
        parser.error("--workers: only Linux lets processes share a port")
    shared = args.workers > 1
    slow_down = None
    if args.slow_down_above:
        slow_down = SlowDown(
            InFlight(args.slow_down_above, shared=shared), RequestCounter(shared=shared)
        )
    serving = partial(
        serve,
        host=args.host,
        port=args.port,
        latency=args.latency_ms / 1000,
        virtual_objects=args.virtual_objects,
        reuse_port=shared,
        list_requests=RequestCounter(shared=shared),
        slow_down=slow_down,
    )
    for _ in range(args.workers - 1):
        multiprocessing.Process(target=serving, daemon=True).start()
    serving()


if __name__ == "__main__":
    main()
