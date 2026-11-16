"""logfmt and JSON log lines. Both escape every value, so no input can forge a log line."""

import json
import logging
import sys
from datetime import UTC, datetime

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}
LEVEL_NAMES = {value: name for name, value in LEVELS.items()}
RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def configure_logging(level: str, log_format: str, stream: str) -> None:
    handler = logging.StreamHandler(sys.stdout if stream == "stdout" else sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format == "json" else LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    # Libraries stay at warning: botocore's debug output is huge and describes every request.
    root.setLevel(max(LEVELS[level], logging.WARNING))
    logging.getLogger("exporter").setLevel(LEVELS[level])


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return " ".join(f"{key}={_logfmt(value)}" for key, value in fields(record).items())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _to_json(fields(record))


def fields(record: logging.LogRecord) -> dict[str, object]:
    moment = datetime.fromtimestamp(record.created, UTC)
    line: dict[str, object] = {
        "ts": moment.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": LEVEL_NAMES.get(record.levelno, record.levelname.lower()),
        "logger": record.name,
        "msg": record.getMessage(),
    }
    line.update((key, value) for key, value in vars(record).items() if key not in RECORD_ATTRIBUTES)
    if record.exc_info:
        line["exception"] = logging.Formatter().formatException(record.exc_info)
    return line


def _logfmt(value: object) -> str:
    text = str(value)
    if text and text.isprintable() and not any(char in text for char in ' ="\\'):
        return text
    return _to_json(text)


def _to_json(value: object) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    # json.dumps keeps U+2028, U+2029, U+0085 and other non-ASCII separators as they are, and
    # some log pipelines break lines on them: every unprintable character gets a \u escape.
    return "".join(char if char.isprintable() else json.dumps(char)[1:-1] for char in text)
