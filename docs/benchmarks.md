# Benchmarks

## Probes, against the upstream exporter

`fakes3` plays an S3 that answers every request after 60 ms, a latency typical of a page of
1000 keys on AWS. Its `bench` bucket holds 10 million made-up objects, named
`data/{i // 10000:05d}/{i:010d}.bin`, so that each decade from 10 thousand to 10 million objects
is a prefix. It runs 8 worker processes, so that it never slows the exporters down. Each exporter
gets one core.

```sh
docker compose -f compose.yaml -f compose.bench.yaml up --detach --build --wait --force-recreate
uv run python -m scripts.bench_listing
```

The script probes each prefix once, then several more times: the first probe of a prefix splits
it along its top-level "folders", later probes along the pages of the previous listing. It checks
that both exporters report the same values. The upstream exporter is not probed on 10 million
objects: a probe would take about 12 minutes.

Results of 2026-11-16, on an AMD Ryzen 9 5950X under Docker Desktop, s3-fast-exporter on
CPython 3.15 with uvloop. Durations are medians of the later probes:

| Objects | upstream | s3-fast-exporter | its first probe |
|---|---|---|---|
| 10 thousand | 754 ms | 332 ms | 764 ms |
| 100 thousand | 7.52 s | 307 ms | 811 ms |
| 1 million | 74.7 s | 1.64 s | 1.98 s |
| 10 million | | 17.3 s | 17.4 s |

What a probe costs:

| Objects | Requests, upstream | Requests, s3-fast-exporter (first probe) | CPU, upstream | CPU, s3-fast-exporter |
|---|---|---|---|---|
| 10 thousand | 10 | 10 (11) | 274 ms | 20 ms |
| 100 thousand | 100 | 100 (110) | 2.71 s | 157 ms |
| 1 million | 1000 | 1000 (1064) | 26.3 s | 1.59 s |
| 10 million | | 10000 (10064) | | 17.3 s |

- Once it knows a prefix, s3-fast-exporter sends as many requests as a single listing: its ranges
  end where pages ended. A first probe sends one request to find the folders, and each range
  but the last reads one page into the next folder.
- The upstream exporter spends 26 ms of CPU on each page, reading it with the AWS SDK for Go:
  even listing one page at a time, it needs a third of its core.
- The peak resident memory of s3-fast-exporter grew from 75 MiB to 108 MiB over the run, that of
  the upstream exporter from 32 MiB to 34 MiB.

## Page scanner

`uv run python -m scripts.bench_scanner` reads a page of 1000 objects with the scanner and with
botocore's parser, which reads the whole XML document into dictionaries:

| Runtime | Scanner | botocore |
|---|---|---|
| CPython 3.14 | 0.73 ms | 50.3 ms |
| PyPy 3.12 | 0.39 ms | 18.8 ms |

## Where the time goes

On one core, s3-fast-exporter spends 1.6 ms of CPU on each page of 1000 objects:

| Step | CPU per page |
|---|---|
| Reading the page | 0.85 ms |
| Signing the request, by botocore | 0.37 ms |
| Sending it and receiving the answer, by aiohttp on uvloop | 0.35 ms |
| Statistics | 0.05 ms |

With S3 answering in 60 ms, about 40 requests at once keep one core busy: a larger listing takes
as long as its CPU time, 1.6 seconds per million objects. Measured inside the exporter's image,
timing each step of a probe of 1 million objects.
