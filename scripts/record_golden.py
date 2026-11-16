"""Record the upstream exporter's answers to the golden probes into tests/golden/upstream.

Needs Docker. Run from the repository root: uv run python -m scripts.record_golden
"""

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fakes3.golden import PROBES, Probe

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "upstream"
UPSTREAM = "http://127.0.0.1:9340"
DURATION = "s3_list_duration_seconds"


def main() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for stale in GOLDEN.iterdir():
        stale.unlink()
    compose("up", "--detach", "--build", "--wait", "fakes3", "upstream")
    try:
        wait_for_upstream()
        crashes = {}
        for probe in PROBES:
            if not probe.crashes_upstream:
                write(f"{probe.name}.prom", without_durations(fetch_ok(probe_path(probe))))
        status, body = fetch("/probe")
        write("probe-without-bucket.txt", f"{status}\n{body}")
        write("discovery.json", fetch_ok("/discovery"))
        build_info = next(
            line
            for line in fetch_ok("/metrics").splitlines()
            if line.startswith("s3_exporter_build_info")
        )
        for probe in PROBES:
            if probe.crashes_upstream:
                crashes[probe.name] = record_crash(probe)
        manifest = {
            "image": json.loads(compose("config", "--format", "json"))["services"]["upstream"][
                "image"
            ],
            "build_info": build_info,
            "durations": f"every {DURATION} value is replaced by 0",
            "crashes": crashes,
        }
        write("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    finally:
        compose("down")


def probe_path(probe: Probe) -> str:
    query = {"bucket": probe.bucket, "prefix": probe.prefix, "delimiter": probe.delimiter}
    return "/probe?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})


def without_durations(exposition: str) -> str:
    lines = [
        line[: line.rindex(" ")] + " 0" if line.startswith(DURATION + "{") else line
        for line in exposition.splitlines()
    ]
    return "\n".join(lines) + "\n"


def record_crash(probe: Probe) -> str:
    try:
        status, _ = fetch(probe_path(probe))
    except OSError:
        pass
    else:
        raise RuntimeError(f"{probe.name}: expected the upstream exporter to crash, got {status}")
    subprocess.run(
        ["docker", "compose", "wait", "upstream"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )
    logs = compose("logs", "--no-log-prefix", "upstream")
    panic = next(line for line in logs.splitlines() if line.startswith("panic: "))
    compose("up", "--detach", "--wait", "upstream")
    wait_for_upstream()
    return panic


def fetch(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(UPSTREAM + path, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def fetch_ok(path: str) -> str:
    status, body = fetch(path)
    if status != 200:
        raise RuntimeError(f"GET {path}: HTTP {status}: {body}")
    return body


def wait_for_upstream(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fetch("/")
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
        else:
            return


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def write(name: str, text: str) -> None:
    (GOLDEN / name).write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
