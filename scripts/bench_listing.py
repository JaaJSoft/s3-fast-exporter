"""Probe the bench bucket through the upstream exporter and through this one, and compare.

Needs the benchmark services; from the repository root:

    docker compose -f compose.yaml -f compose.bench.yaml up --detach --build --wait --force-recreate
    uv run python -m scripts.bench_listing
"""

import argparse
import platform
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from prometheus_client.parser import text_string_to_metric_families

FAKES3 = "http://127.0.0.1:9000"
EXPORTERS = {
    "upstream": ("upstream", "http://127.0.0.1:9340"),
    "s3-fast-exporter": ("exporter", "http://127.0.0.1:9341"),
}
# Prefixes of the bench bucket, by number of objects, and how many times each is probed.
LISTINGS = {
    "10k": ("data/00000/", 20),
    "100k": ("data/0000", 10),
    "1M": ("data/000", 5),
    "10M": ("data/", 3),
}
VALUES = (
    "s3_objects",
    "s3_objects_size_sum_bytes",
    "s3_biggest_object_size_bytes",
    "s3_last_modified_object_date",
    "s3_last_modified_object_size_bytes",
)
CLOCK_TICKS = 100


@dataclass(frozen=True, slots=True)
class Probe:
    seconds: float
    requests: int
    values: dict[str, float]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe the bench bucket through both exporters, and compare."
    )
    parser.add_argument("--listings", default=",".join(LISTINGS), help="Sizes to probe.")
    parser.add_argument(
        "--upstream-up-to", default="1M", help="Largest size probed through upstream."
    )
    parser.add_argument("--rounds", type=int, help="Probes per size, after the first one.")
    parser.add_argument("--exporters", default=",".join(EXPORTERS), help="Exporters to probe.")
    args = parser.parse_args()
    sizes = args.listings.split(",")
    upstream_sizes = list(LISTINGS)[: list(LISTINGS).index(args.upstream_up_to) + 1]

    print(environment())
    print(
        "| Objects | Exporter | First probe | Median | Slowest | Requests | CPU per probe "
        "| Peak RSS |"
    )
    print("|---|---|---|---|---|---|---|---|")
    expected: dict[str, dict[str, float]] = {}
    for name in args.exporters.split(","):
        service, url = EXPORTERS[name]
        for size in sizes:
            if name == "upstream" and size not in upstream_sizes:
                continue
            prefix, rounds = LISTINGS[size]
            first = probe(url, prefix)
            cpu_before = process_stats(service)[0]
            probes = [probe(url, prefix) for _ in range(args.rounds or rounds)]
            cpu_after, peak = process_stats(service)
            for each in (first, *probes):
                if expected.setdefault(size, each.values) != each.values:
                    raise SystemExit(f"{name} {size}: {each.values} != {expected[size]}")
            times = sorted(each.seconds for each in probes)
            requests = statistics.mean(each.requests for each in probes)
            print(
                f"| {size} | {name} | {seconds(first.seconds)} ({first.requests} requests) "
                f"| {seconds(statistics.median(times))} | {seconds(times[-1])} "
                f"| {requests:.0f} | {seconds((cpu_after - cpu_before) / len(probes))} "
                f"| {peak / 2**20:.0f} MiB |",
                flush=True,
            )


def probe(url: str, prefix: str) -> Probe:
    before = list_requests()
    query = urllib.parse.urlencode({"bucket": "bench", "prefix": prefix})
    started = time.perf_counter()
    with urllib.request.urlopen(f"{url}/probe?{query}", timeout=3600) as response:
        body = response.read().decode()
    elapsed = time.perf_counter() - started
    values = {
        sample.name: sample.value
        for family in text_string_to_metric_families(body)
        for sample in family.samples
        if sample.name in VALUES
    }
    if len(values) != len(VALUES):
        raise SystemExit(f"the probe of {prefix} failed:\n{body}")
    return Probe(elapsed, list_requests() - before, values)


def list_requests() -> int:
    with urllib.request.urlopen(f"{FAKES3}/_fakes3/list-requests") as response:
        return int(response.read())


def process_stats(service: str) -> tuple[float, int]:
    """CPU seconds and peak resident bytes of the main process of a compose service."""
    container = run("docker", "compose", "ps", "--quiet", service).strip()
    proc = run(
        "docker", "run", "--rm", f"--pid=container:{container}", "busybox",
        "cat", "/proc/1/stat", "/proc/1/status",
    )  # fmt: skip
    stat, _, status = proc.partition("\n")
    fields = stat.rsplit(")", 1)[1].split()
    peak = next(line.split()[1] for line in status.splitlines() if line.startswith("VmHWM:"))
    return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS, int(peak) * 1024


def environment() -> str:
    cpus = run("docker", "info", "--format", "{{.NCPU}} CPUs, {{.OperatingSystem}}").strip()
    model = run("docker", "compose", "exec", "fakes3", "grep", "-m1", "model name", "/proc/cpuinfo")
    python = run("docker", "compose", "exec", "exporter", "/opt/venv/bin/python", "-VV")
    return (
        f"Docker: {cpus}; CPU: {model.split(':', 1)[1].strip()}; host: {platform.system()}\n"
        f"s3-fast-exporter on {python.strip()}\n"
    )


def run(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def seconds(value: float) -> str:
    return f"{value:.2f} s" if value >= 1 else f"{value * 1000:.0f} ms"


if __name__ == "__main__":
    main()
