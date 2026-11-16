# s3-fast-exporter

Prometheus exporter for S3 buckets and objects. It exposes the same metrics as
[ribbybibby/s3_exporter](https://github.com/ribbybibby/s3_exporter), which is no longer
maintained, and replaces it without touching dashboards or alerts: same flags, same environment
variables, same port, same metrics.

Status: version 0.1 behaves like the upstream exporter and, like it, lists each prefix
sequentially. Parallel listing for large buckets is in progress.

## Quick start

No image is published yet. Build it from this repository:

```sh
docker build -t s3-fast-exporter .
docker run --rm -p 9340:9340 \
  -e AWS_REGION=eu-west-1 -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
  s3-fast-exporter
curl 'http://localhost:9340/probe?bucket=my-bucket&prefix=backups/'
```

To use the AWS configuration of the host, mount it read-only:

```sh
docker run --rm -p 9340:9340 -v "$HOME/.aws:/aws:ro" \
  -e AWS_CONFIG_FILE=/aws/config -e AWS_SHARED_CREDENTIALS_FILE=/aws/credentials \
  -e AWS_PROFILE=my-profile \
  s3-fast-exporter
```

## Endpoints

| Path | Answer |
|---|---|
| `/probe?bucket=B&prefix=P&delimiter=D` | the metrics of the objects of bucket `B` under prefix `P`; `prefix` and `delimiter` are optional |
| `/discovery` | the buckets the exporter can see, in the Prometheus HTTP service discovery format |
| `/metrics` | the exporter's own metrics: process, Python, build information |
| `/-/healthy` | `OK` while the exporter runs |

## Configuration

| Flag | Variable | Default | Meaning |
|---|---|---|---|
| `--web.listen-address` | `S3_EXPORTER_WEB_LISTEN_ADDRESS` | `:9340` | address to listen on |
| `--web.metrics-path` | `S3_EXPORTER_WEB_METRICS_PATH` | `/metrics` | path of the exporter's own metrics |
| `--web.probe-path` | `S3_EXPORTER_WEB_PROBE_PATH` | `/probe` | path of the probe endpoint |
| `--web.discovery-path` | `S3_EXPORTER_WEB_DISCOVERY_PATH` | `/discovery` | path of the service discovery endpoint |
| `--s3.endpoint-url` | `S3_EXPORTER_S3_ENDPOINT_URL` | AWS | URL of another S3 service |
| `--s3.force-path-style` | `S3_EXPORTER_S3_FORCE_PATH_STYLE` | off | path-style URLs instead of virtual-hosted buckets |
| `--s3.disable-ssl` | `S3_EXPORTER_S3_DISABLE_SSL` | off | HTTP instead of HTTPS |
| `--log.level` | `S3_EXPORTER_LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error` or `fatal` |
| `--log.format` | `S3_EXPORTER_LOG_FORMAT` | `logfmt` | `logfmt` or `json` |

Flags win over variables, and boolean flags also have a `--no-` form.

AWS access goes through botocore, so the usual AWS configuration applies:
- credentials come from variables, shared files and profiles, web identity (EKS IRSA), EKS Pod
  Identity, ECS task roles, EC2 instance profiles or SSO;
- `AWS_REGION` sets the default region;
- `AWS_CA_BUNDLE` adds a private certificate authority;
- `HTTPS_PROXY` and `NO_PROXY` set proxies.

On AWS, the region of each bucket is found automatically. The exporter needs `s3:ListBucket` on
the probed buckets, and `s3:ListAllMyBuckets` for `/discovery`.

Only the S3 API as AWS documents it is supported: a response that departs from it is reported as
a failed listing, never read approximately.

## Metrics

| Metric | Labels | Value |
|---|---|---|
| `s3_objects` | bucket, prefix | number of objects |
| `s3_objects_size_sum_bytes` | bucket, prefix | total size of the objects |
| `s3_biggest_object_size_bytes` | bucket, prefix | size of the biggest object |
| `s3_last_modified_object_date` | bucket, prefix | modification date of the most recently modified object, in seconds since the epoch |
| `s3_last_modified_object_size_bytes` | bucket, prefix | size of the most recently modified object |
| `s3_common_prefixes` | bucket, prefix, delimiter | number of common prefixes, when a delimiter is given |
| `s3_list_success` | bucket, prefix, delimiter | 1 when the listing succeeded, 0 otherwise |
| `s3_list_duration_seconds` | bucket, prefix, delimiter | duration of the listing |

With a delimiter, only `s3_common_prefixes`, `s3_list_success` and `s3_list_duration_seconds`
are reported, as upstream does.

## Prometheus configuration

Probe a fixed list of buckets and prefixes:

```yaml
scrape_configs:
  - job_name: s3
    metrics_path: /probe
    static_configs:
      - targets:
          - bucket=backups;prefix=database/;
          - bucket=uploads;prefix=;
    relabel_configs:
      - source_labels: [__address__]
        regex: "^bucket=(.*);prefix=(.*);$"
        replacement: "${1}"
        target_label: __param_bucket
      - source_labels: [__address__]
        regex: "^bucket=(.*);prefix=(.*);$"
        replacement: "${2}"
        target_label: __param_prefix
      - target_label: __address__
        replacement: 127.0.0.1:9340
```

Or probe every bucket the exporter can see, keeping some of them:

```yaml
scrape_configs:
  - job_name: s3
    metrics_path: /probe
    http_sd_configs:
      - url: http://127.0.0.1:9340/discovery
    relabel_configs:
      - source_labels: [__param_bucket]
        action: keep
        regex: ^backups-.*
```

Alert when the newest object of a prefix is more than a day old:

```
(time() - s3_last_modified_object_date) / 3600 > 24
```

## Migrating from ribbybibby/s3_exporter

Replace the image and keep everything else. Flags, `S3_EXPORTER_*` variables, port and paths are
the same, and so are the metric names, labels, types and HELP texts.

Two values differ on purpose:

- A failed listing (missing bucket, denied access, network error) answers `s3_list_success 0`.
  Upstream crashed and restarted instead, because of a bug in that code path.
- An empty listing reports `s3_last_modified_object_date 0`. Upstream reported `-6795364578`, an
  overflow of Go's zero time. An alert on the age of the newest object, like the one above, fires
  in both cases.

Other changes:

- Buckets outside the default region now work; upstream failed on them.
- `AWS_SDK_LOAD_CONFIG` is no longer needed: shared AWS files are always read. Upstream's
  `-e HOME=/ -v $HOME/.aws:/.aws` keeps working.
- `/metrics` shows this exporter's process and Python metrics instead of Go's.
  `s3_exporter_build_info` keeps its name, with a `version` label.
- Logs are logfmt or JSON lines. Upstream's `--log.format` values `logger:stderr`, `logger:stdout`
  and `?json=true` are accepted; `logger:syslog` is not.

## Development

Requires [uv](https://docs.astral.sh/uv/), and Docker for the last two commands.

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m scripts.record_golden   # re-records the upstream exporter's answers
docker compose up --build exporter upstream
```

The last command runs this exporter on port 9341 and the upstream one on port 9340, both against
`fakes3`, a deterministic S3 server with the test datasets, so that their answers can be
compared side by side.

## License

Apache License 2.0. See [NOTICE](NOTICE) for what comes from ribbybibby/s3_exporter.
