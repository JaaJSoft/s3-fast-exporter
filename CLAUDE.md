# s3-fast-exporter

Prometheus exporter for S3 buckets and objects, replacing the unmaintained
[ribbybibby/s3_exporter](https://github.com/ribbybibby/s3_exporter). Written in Python to show that
a well-designed Python service can be fast.

## Rules that do not bend

- Metrics on `/probe` are a contract: same names, labels, types, HELP and values as upstream.
  A value difference must be deliberate, covered by the golden tests and documented for users.
- Standard S3 API only. A response that breaks the protocol is a scan error, never
  silently partial data. No vendor-specific workaround.
- No AWS SDK at runtime. S3 access goes through `exporter.s3`; botocore is a test oracle.
- Runtime dependencies: aiohttp, prometheus-client, optional uvloop. Adding one needs an explicit agreement first.
- The code runs on CPython >= 3.12 and on PyPy 3.12.

## Commands

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run --no-default-groups --group test --python pypy3.12 pytest
```

mypy does not install on PyPy: the `test` group is the only one meant for PyPy.

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

- The scanner and the signer are checked against botocore (differential tests).
- A partitioned listing must give the same result as a sequential one for any partition
  boundaries (hypothesis).
- Integration tests use moto in server mode.

## Writing

- Never use an em dash or an en dash, in code, docs or commits. Use "-", ":" or parentheses.
