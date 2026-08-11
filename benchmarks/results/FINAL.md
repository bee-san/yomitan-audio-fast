# Final benchmark note

This is the concise, publication-facing summary of the completed real-data benchmark. It separates the higher-sample performance run from the corrected final correctness smoke.

## Headline result

Warm HTTP/1.1 keep-alive, full-response p50 latency in milliseconds:

| Workload | Original | Fast Anki | Rust | Fast Anki speedup | Rust speedup |
|---|---:|---:|---:|---:|---:|
| Cached lookup | 5.513 | 0.574 | 0.248 | 9.60× | 22.22× |
| Unique real hit | 5.870 | 1.194 | 0.279 | 4.92× | 21.05× |
| Lookup + first audio | 23.548 | 1.294 | 0.712 | 18.19× | 33.08× |
| Direct `/v1/play` | unavailable | 0.659 | 0.266 | — | — |

Median trial throughput:

| Workload | Original | Fast Anki | Rust |
|---|---:|---:|---:|
| Cached lookup | 96 ops/s | 1,620 ops/s | 2,913 ops/s |
| Unique real hit | 87 ops/s | 709 ops/s | 2,237 ops/s |
| Lookup + first audio | 47 ops/s | 782 ops/s | 1,299 ops/s |
| Direct `/v1/play` | unavailable | 1,437 ops/s | 2,900 ops/s |

Raw exported rows: [standard metrics CSV](standard-20260811-metrics.csv).

## Performance run design

- Run ID: `20260811-173346`, profile `standard`.
- Five repeated sequential trials.
- Per trial: 160 lookups, 40 two-stage operations, and 80 direct plays.
- Concurrency: 160 requests per trial at 1, 8, and 32 workers, repeated three times.
- Real deterministic corpus from a 590,410-row database, including all populated sources, source subsets/order, real Forvo user filters, omitted readings, aliases, and misses.
- Percentiles use nearest-rank over successful pooled samples; throughput is the median of trial operation rates.
- Full latency includes reading the complete response body. A two-stage operation includes the JSON lookup and first audio request.

System:

- AMD Ryzen AI Z2 Extreme, 8 cores / 16 threads.
- Windows 11 Home build 26200.
- 13.62 GiB RAM; Samsung NVMe SSD.
- Anki CPython 3.13.5 / SQLite 3.47.1.
- 590,410 mappings, 280,396 terms, 373,911 referenced audio assets, 1,713,724,873 referenced audio bytes.

## Why the standard report originally said `FAIL`

The standard run's performance samples and compatibility comparisons completed. Its only required-feature failure was one harness assertion for Rust's unsatisfiable byte range:

- Rust returned the required HTTP `416`.
- Rust returned the correct `Content-Range: bytes */3492`.
- It included a 41-byte explanatory representation.
- The old harness incorrectly required an empty body, although a `416` response is allowed to carry a representation.

The harness now treats the `416` body as representation-allowed while still requiring the exact status and `Content-Range`. No server code or timed lookup/audio path changed for that correction.

## Corrected final smoke

Run ID `20260811-174022` used the corrected assertion and finished **PASS**:

| Endpoint | Cases | Audio candidates SHA-256 checked | Result |
|---|---:|---:|---:|
| Original | 57/57 | 108 | PASS |
| Fast Anki | 57/57 | 108 | PASS |
| Rust | 57/57 | 108 | PASS |

Raw exported rows: [final smoke metrics CSV](final-smoke-20260811-metrics.csv).

The smoke used fewer timing samples, so the higher-sample standard run remains the performance source; the corrected smoke is the final protocol/correctness verdict.

## Concurrency at 32 workers

Warm keep-alive, 480 requests per row:

| Workload | Original p50 / p95 | Fast Anki p50 / p95 | Rust p50 / p95 |
|---|---:|---:|---:|
| Lookup | 12.171 / 529.571 ms | 5.315 / 13.204 ms | 3.398 / 10.982 ms |
| Audio | 9.656 / 522.252 ms | 4.996 / 12.932 ms | 4.347 / 13.178 ms |

Throughput at 32 workers was 152, 1,906, and 2,116 lookup requests/s for original, Fast Anki, and Rust respectively.

## Correctness beyond the smoke

- Independent exhaustive add-on audit: 743,634 real selection cases, zero mismatches.
- Standard run: 281/281 compatibility cases for the original and Fast Anki paths; 400 audio candidates byte-checked per endpoint.
- Packed add-on soak: 2,064 requests across 16 clients, zero errors and zero reconnects.
- Add-on lifecycle/race/corruption tests covered database publication epochs, overlapping pack leases, immutable historical URLs, ranges, HEAD, ETags/304, CORS, and tamper rejection.
- Rust release tests: 4/4.

## Interpretation limits

- These are loopback server measurements, not Chromium scheduling, audio decode, or sound-device latency.
- Filesystem and SQLite caches are warm; Windows standby cache was not flushed.
- The original server used HTTP/1.0 and closed every response even when the client requested keep-alive. That behavior is part of the measured original end-to-end path.
- The original add-on shares Anki's Python process, so whole-process memory is not directly comparable with isolated Fast Anki/Rust runners.
- The final live Rust headline used `mmap-sorted-hash`; a separate architecture matrix found an MPH throughput edge at 32 workers. Sorted remains the documented balanced/default mode because its component lookup was faster and it avoids an additional hash.

For component experiments and rejected alternatives, see [the evidence archive](evidence-summary.md) and [`../../ATTEMPTS.md`](../../ATTEMPTS.md).
