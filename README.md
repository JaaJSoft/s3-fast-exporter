# s3-fast-exporter

Prometheus exporter for S3 buckets and objects, built for large buckets. It exposes the same
metrics as [ribbybibby/s3_exporter](https://github.com/ribbybibby/s3_exporter), which is no longer
maintained, and aims to replace it without touching dashboards or alerts.

Status: work in progress, nothing usable yet.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m scripts.record_golden   # needs Docker: re-records tests/golden/upstream
```

## License

Apache License 2.0.
