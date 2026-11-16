"""Command line flags and S3_EXPORTER_* variables, named as the upstream exporter named them."""

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from exporter import __version__

ENV_PREFIX = "S3_EXPORTER_"
LOG_LEVELS = ("debug", "info", "warn", "error", "fatal")
TRUE_VALUES = frozenset({"1", "t", "T", "TRUE", "true", "True"})
FALSE_VALUES = frozenset({"0", "f", "F", "FALSE", "false", "False"})
LIST_CONCURRENCY = 64


@dataclass(frozen=True, slots=True)
class Settings:
    listen_host: str | None
    listen_port: int
    metrics_path: str
    probe_path: str
    discovery_path: str
    endpoint_url: str | None
    disable_ssl: bool
    force_path_style: bool
    list_concurrency: int
    list_processes: int
    log_level: str
    log_format: str
    log_stream: str


def parse_settings(argv: Sequence[str], environ: Mapping[str, str]) -> Settings:
    parser = argparse.ArgumentParser(
        prog="s3-fast-exporter", description="Prometheus exporter for S3 buckets and objects."
    )

    def option(flag: str, default: str, help: str, parse: Callable[[str], object] = str) -> None:
        parser.add_argument(
            f"--{flag}",
            dest=_dest(flag),
            default=environ.get(_env(flag), default),
            type=parse,
            help=help,
        )

    def switch(flag: str, help: str) -> None:
        value = environ.get(_env(flag), "false")
        if value not in TRUE_VALUES | FALSE_VALUES:
            parser.error(f"{_env(flag)}: not a boolean: {value!r}")
        parser.add_argument(
            f"--{flag}",
            dest=_dest(flag),
            action=argparse.BooleanOptionalAction,
            default=value in TRUE_VALUES,
            help=help,
        )

    option("web.listen-address", ":9340", "Address to listen on for web interface and telemetry.")
    option("web.metrics-path", "/metrics", "Path under which to expose metrics.")
    option("web.probe-path", "/probe", "Path under which to expose the probe endpoint.")
    option("web.discovery-path", "/discovery", "Path under which to expose service discovery.")
    option("s3.endpoint-url", "", "Custom S3 endpoint URL.")
    switch("s3.disable-ssl", "Talk to S3 over HTTP instead of HTTPS.")
    switch("s3.force-path-style", "Address buckets with path-style instead of virtual-hosted URLs.")
    option(
        "s3.list-concurrency",
        str(LIST_CONCURRENCY),
        "Most ListObjectsV2 requests a probe sends at once, per listing process.",
        _positive,
    )
    option(
        "s3.list-processes",
        "1",
        "Processes that list, each on a core of its own: past 1, worker processes list.",
        _positive,
    )
    option("log.level", "info", f"Lowest severity logged: {', '.join(LOG_LEVELS)}.")
    option(
        "log.format", "logfmt", "logfmt or json; upstream values such as logger:stderr also work."
    )
    parser.add_argument("--version", action="version", version=f"s3-fast-exporter {__version__}")
    args = parser.parse_args(argv)

    if args.log_level not in LOG_LEVELS:
        parser.error(
            f"--log.level: expected one of {', '.join(LOG_LEVELS)}, got {args.log_level!r}"
        )
    try:
        host, port = _listen_address(args.web_listen_address)
        log_format, log_stream = _log_format(args.log_format)
    except ValueError as error:
        parser.error(str(error))
    return Settings(
        listen_host=host,
        listen_port=port,
        metrics_path=args.web_metrics_path,
        probe_path=args.web_probe_path,
        discovery_path=args.web_discovery_path,
        endpoint_url=args.s3_endpoint_url or None,
        disable_ssl=args.s3_disable_ssl,
        force_path_style=args.s3_force_path_style,
        list_concurrency=args.s3_list_concurrency,
        list_processes=args.s3_list_processes,
        log_level=args.log_level,
        log_format=log_format,
        log_stream=log_stream,
    )


def _dest(flag: str) -> str:
    return flag.replace(".", "_").replace("-", "_")


def _env(flag: str) -> str:
    return ENV_PREFIX + _dest(flag).upper()


def _positive(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return int(value)


def _listen_address(address: str) -> tuple[str | None, int]:
    host, separator, port = address.rpartition(":")
    if not separator or not port.isdigit():
        raise ValueError(f"--web.listen-address: expected host:port, got {address!r}")
    host = host.removeprefix("[").removesuffix("]")
    return host or None, int(port)


def _log_format(value: str) -> tuple[str, str]:
    if value in ("logfmt", "json"):
        return value, "stderr"
    # prometheus/common/log syntax, as used by the upstream exporter: logger:stdout?json=true
    target, _, options = value.removeprefix("logger:").partition("?")
    if value.startswith("logger:") and target in ("stderr", "stdout"):
        return ("json" if "json=true" in options.split("&") else "logfmt"), target
    raise ValueError(f"--log.format: expected logfmt or json, got {value!r}")
