# s3-fast-exporter

Prometheus exporter for S3 buckets and objects, replacing the unmaintained
[ribbybibby/s3_exporter](https://github.com/ribbybibby/s3_exporter). Written in Python to show that
a well-designed Python service can be fast.

## Rules that do not bend

- Metrics on `/probe` are a contract: same names, labels, types, HELP and values as upstream.
  A value difference must be deliberate, covered by the golden tests and documented for users.
- Standard S3 API only. A response that breaks the protocol is a scan error, never
  silently partial data. No vendor-specific workaround.
- botocore owns credentials, endpoints and signing (presigned URLs) and makes the rare calls.
  Listings never go through its HTTP stack or parser: aiohttp fetches, `exporter.s3.scanner` reads.
- Runtime dependencies: aiohttp, botocore, prometheus-client, optional uvloop. Adding one needs an
  explicit agreement first.
- The code runs on CPython >= 3.12 and on PyPy 3.12.

## Commands

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run --no-default-groups --group test --python pypy3.12 pytest
uv run python -m scripts.record_golden   # needs Docker: re-records tests/golden/upstream
uv run python -m scripts.bench_scanner   # scanner against botocore's parser, per page
uv run python -m fakes3 &                # the golden datasets on http://127.0.0.1:9000
uv run s3-fast-exporter --s3.endpoint-url=http://127.0.0.1:9000 --s3.force-path-style
docker compose up --detach --build --wait fakes3 exporter   # the image on :9341
PARITY_EXPORTER_URL=http://127.0.0.1:9341 uv run pytest tests/test_parity.py
```

The image must stay a drop-in replacement: same flags and variables as the `upstream` service of
`compose.yaml`, and `tests/test_parity.py` green against it (CI job `image`).

mypy and the `oracle` group (moto) need CPython with prebuilt wheels: only the `test` group is
meant for PyPy.

## Code

- English everywhere: code, comments, docs, commit messages.
- Comments are rare. They explain a "why" or a non-obvious invariant for someone reading the code
  ten years from now. Never narrate a change or a PR, never leave commented-out code. Design
  rationale goes into the commit message or the PR description.
- mypy strict. Small modules, no global mutable state, dependencies passed explicitly.
- asyncio only (TaskGroup, asyncio.timeout); no anyio or trio.

## Performance

- Hot path: the per-object loop in the scanner and the aggregators. No per-object dict, dataclass,
  datetime or exception there; read bytes, produce ints.
- A change to the scanner, the lister or the aggregators comes with before and after numbers.
- Measure before optimizing. Results go to `docs/benchmarks.md`.

## Tests

- The scanner is checked against botocore's parser on generated listings (hypothesis), and
  presigned URLs must reach the server byte for byte.
- A partitioned listing must give the same result as a sequential one for any partition
  boundaries (hypothesis).
- Integration tests use moto in server mode.
- `tests/golden/upstream` holds the upstream exporter's answers on the deterministic `fakes3`
  datasets (`fakes3/golden.py`). Changing those datasets means re-recording; CI checks it.

## Releasing

Every merge on master publishes `ghcr.io/jaajsoft/s3-fast-exporter:edge` for amd64 and arm64.
A release is cut from an up-to-date, clean master:

```sh
uv run python -m scripts.release X.Y.Z   # sets the version, commits "release: vX.Y.Z", tags it
git push --atomic origin master vX.Y.Z
```

CI refuses a tag that differs from the project version, and publishes `X.Y.Z`, `X.Y` and `latest`.

## Writing

- Never use an em dash or an en dash, in code, docs or commits. Use "-", ":" or parentheses.
