import json
import logging
from collections.abc import Iterator

import pytest

from exporter.logs import JsonFormatter, LogfmtFormatter, configure_logging


@pytest.fixture
def restored_logging() -> Iterator[None]:
    root, exporter = logging.getLogger(), logging.getLogger("exporter")
    handlers, levels = root.handlers[:], (root.level, exporter.level)
    yield
    root.handlers[:] = handlers
    root.setLevel(levels[0])
    exporter.setLevel(levels[1])


@pytest.mark.usefixtures("restored_logging")
def test_the_log_level_applies_to_the_exporter_and_libraries_stay_at_warning() -> None:
    configure_logging("debug", "logfmt", "stderr")
    assert logging.getLogger("exporter.probe").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("botocore.endpoint").getEffectiveLevel() == logging.WARNING


def record(message: str, **extra: object) -> logging.LogRecord:
    entry = logging.makeLogRecord(
        {
            "name": "exporter.probe",
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": message,
        }
    )
    entry.__dict__.update(extra)
    return entry


def test_logfmt_quotes_values_that_need_it() -> None:
    line = LogfmtFormatter().format(record("listing failed", bucket="my-bucket", prefix='a b"c'))
    assert line.endswith(
        'level=warn logger=exporter.probe msg="listing failed" bucket=my-bucket prefix="a b\\"c"'
    )


@pytest.mark.parametrize("separator", ["\n", "\r", "\x85", "\u2028", "\u2029", "\x1b"])
@pytest.mark.parametrize("formatter", [LogfmtFormatter(), JsonFormatter()], ids=["logfmt", "json"])
def test_user_input_cannot_forge_a_log_line(formatter: logging.Formatter, separator: str) -> None:
    line = formatter.format(record("listing failed", prefix=f"é{separator}level=error msg=forged"))
    assert line.isprintable()
    assert "é" in line


def test_escaped_json_lines_still_round_trip() -> None:
    prefix = "é\u2028\x85\U0001f642\n"
    assert json.loads(JsonFormatter().format(record("m", prefix=prefix)))["prefix"] == prefix


def test_json_lines_carry_the_extra_fields() -> None:
    line = json.loads(JsonFormatter().format(record("listing failed", bucket="b")))
    assert {key: line[key] for key in ("level", "logger", "msg", "bucket")} == {
        "level": "warn",
        "logger": "exporter.probe",
        "msg": "listing failed",
        "bucket": "b",
    }
