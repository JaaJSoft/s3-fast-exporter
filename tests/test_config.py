import pytest

from exporter.config import Settings, parse_settings

DEFAULTS = Settings(
    listen_host=None,
    listen_port=9340,
    metrics_path="/metrics",
    probe_path="/probe",
    discovery_path="/discovery",
    endpoint_url=None,
    disable_ssl=False,
    force_path_style=False,
    log_level="info",
    log_format="logfmt",
    log_stream="stderr",
)


def test_defaults_are_upstream_defaults() -> None:
    assert parse_settings([], {}) == DEFAULTS


def test_upstream_flags_are_understood() -> None:
    settings = parse_settings(
        [
            "--web.listen-address=127.0.0.1:9999",
            "--web.probe-path=/s3",
            "--s3.endpoint-url=http://s3.local:9000",
            "--s3.disable-ssl",
            "--s3.force-path-style",
            "--log.level=debug",
        ],
        {},
    )
    assert (settings.listen_host, settings.listen_port, settings.probe_path) == (
        "127.0.0.1",
        9999,
        "/s3",
    )
    assert settings.endpoint_url == "http://s3.local:9000"
    assert settings.disable_ssl
    assert settings.force_path_style
    assert settings.log_level == "debug"


def test_environment_variables_are_named_like_upstream_and_flags_win() -> None:
    environ = {
        "S3_EXPORTER_WEB_LISTEN_ADDRESS": ":9341",
        "S3_EXPORTER_S3_ENDPOINT_URL": "http://env:9000",
        "S3_EXPORTER_S3_DISABLE_SSL": "true",
        "S3_EXPORTER_S3_FORCE_PATH_STYLE": "1",
        "S3_EXPORTER_LOG_FORMAT": "json",
    }
    settings = parse_settings(["--no-s3.disable-ssl"], environ)
    assert settings.listen_port == 9341
    assert settings.endpoint_url == "http://env:9000"
    assert not settings.disable_ssl
    assert settings.force_path_style
    assert settings.log_format == "json"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("logfmt", ("logfmt", "stderr")),
        ("json", ("json", "stderr")),
        ("logger:stderr", ("logfmt", "stderr")),
        ("logger:stdout?json=true", ("json", "stdout")),
    ],
)
def test_upstream_log_formats_are_accepted(value: str, expected: tuple[str, str]) -> None:
    settings = parse_settings([f"--log.format={value}"], {})
    assert (settings.log_format, settings.log_stream) == expected


def test_ipv6_listen_address() -> None:
    settings = parse_settings(["--web.listen-address=[::1]:9340"], {})
    assert (settings.listen_host, settings.listen_port) == ("::1", 9340)


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        (["--web.listen-address=9340"], {}),
        (["--log.level=verbose"], {}),
        (["--log.format=logger:syslog"], {}),
        ([], {"S3_EXPORTER_S3_DISABLE_SSL": "yes"}),
    ],
)
def test_invalid_settings_exit_with_usage(argv: list[str], environ: dict[str, str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        parse_settings(argv, environ)
    assert exit_.value.code == 2
