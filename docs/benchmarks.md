# Benchmarks

## Probes, against the upstream exporter

`fakes3` plays an S3 that answers every request after 60 ms, a latency typical of a page of
1000 keys on AWS. Its `bench` bucket holds 10 million made-up objects, named
`data/{i // 10000:05d}/{i:010d}.bin`, so that each decade from 10 thousand to 10 million objects
is a prefix. It runs 8 worker processes, so that it never slows the exporters down. Each exporter
gets one core, except `exporter-processes`, which lists with `--s3.list-processes=4` on four
cores.

```sh
docker compose -f compose.yaml -f compose.bench.yaml up --detach --build --wait --force-recreate
uv run python -m scripts.bench_listing
```

The script probes each prefix once, then several more times: the first probe of a prefix splits
it along its top-level "folders", later probes along the pages of the previous listing. It checks
that the exporters report the same values. The upstream exporter is not probed on 10 million
objects: a probe would take about 12 minutes.

Results of 2026-11-16, on an AMD Ryzen 9 5950X under Docker Desktop, s3-fast-exporter on
CPython 3.15 with uvloop. Durations are medians of the later probes. From one session to
another, they vary by up to 20% on this machine: compare them within a table.

| Objects | upstream | s3-fast-exporter | s3-fast-exporter, 4 processes |
|---|---|---|---|
| 10 thousand | 745 ms | 328 ms | 329 ms |
| 100 thousand | 7.44 s | 302 ms | 303 ms |
| 1 million | 74.1 s | 1.47 s | 555 ms |
| 10 million | | 15.0 s | 4.99 s |

The first probe of each prefix:

| Objects | s3-fast-exporter | s3-fast-exporter, 4 processes |
|---|---|---|
| 10 thousand | 751 ms | 850 ms |
| 100 thousand | 832 ms | 813 ms |
| 1 million | 1.81 s | 1.48 s |
| 10 million | 15.1 s | 4.90 s |

What a probe costs:

| Objects | Requests: upstream / s3-fast-exporter | First probe: 1 process / 4 | CPU: upstream / 1 process / 4 | Peak RSS: upstream / 1 process / 4 |
|---|---|---|---|---|
| 10 thousand | 10 / 10 | 11 / 11 | 260 ms / 18 ms / 18 ms | 32 / 76 / 346 MiB |
| 100 thousand | 100 / 100 | 110 / 110 | 2.59 s / 142 ms / 179 ms | 33 / 85 / 429 MiB |
| 1 million | 1000 / 1000 | 1064 / 1100 | 25.0 s / 1.44 s / 1.69 s | 34 / 105 / 500 MiB |
| 10 million | - / 10000 | 10064 / 10256 | - / 14.8 s / 17.9 s | - / 107 / 512 MiB |

- Once it knows a prefix, s3-fast-exporter sends as many requests as a single listing: its ranges
  end where pages ended. A first probe sends one request to find the folders, and each range
  but the last reads one page into the next folder. Four processes list four times as many
  ranges at once, hence more of those pages.
- The upstream exporter spends 25 ms of CPU on each page, reading it with the AWS SDK for Go:
  even listing one page at a time, it needs a third of its core.
- Four processes list 10 million objects 3 times as fast as one, for a fifth more CPU: each
  range costs a little, and so does handing it to a worker process and merging what comes back.
- Each process takes about 100 MiB: four workers and the exporter's process reach 512 MiB.

## When S3 slows down

With `FAKES3_SLOW_DOWN_ABOVE=32`, fakes3 answers `503 SlowDown` past 32 requests in flight, so
that it lists at most about 530 pages a second. Each process of s3-fast-exporter halves the
requests it sends to the bucket on a 503, sends the request again after a pause, and raises the
pace by one for each round of answers. Same session as above:

| Objects | s3-fast-exporter | 503 answers per probe | 4 processes | 503 answers per probe |
|---|---|---|---|---|
| 1 million | 2.83 s | 2 | 2.57 s | 67 |
| 10 million | 26.9 s | 26 | 22.6 s | 379 |

- At 530 pages a second, 10 million objects take at least 19 s: one process lists at 70% of that
  pace, four at 83%. The pace a process finds is kept from one probe to the next, so that each
  probe starts at it rather than at full speed.
- Four processes pace themselves apart, so they cross the limit more often: one request in 26
  meets a 503, one in 385 for a single process.

## Page scanner

`uv run python -m scripts.bench_scanner` reads a page of 1000 objects with the scanner and with
botocore's parser, which reads the whole XML document into dictionaries:

| Runtime | Scanner | botocore |
|---|---|---|
| CPython 3.14 | 0.73 ms | 50.3 ms |

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
