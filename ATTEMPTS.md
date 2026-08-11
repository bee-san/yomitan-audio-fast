# Architecture experiments and decisions

This project benchmarked competing designs rather than locking onto the first proposal.

## Anki add-on lookup

| Design | Result | Decision |
|---|---:|---|
| Legacy SQLite connection/query per request | 2,292.1 us median serialized lookup | baseline |
| Retained immutable SQLite pool + bounded LRUs | 75.0 us uncached, 0.8 us cache hit, ~2.2 ms setup | selected |
| Full Python preload | 34.5 us, but 5.76 s startup and +250 MiB | optional/rejected as default |
| Python mmap hash/postings | 72.4 us | rejected: complexity without material end-to-end gain |

Raw SQL variants were also isolated. `reading IS NULL OR reading=?` kept a 23.5 us median and far better tail than term-only SQL plus Python filtering.

## Anki HTTP transport

`ThreadingHTTPServer` with HTTP/1.1 keep-alive (649.3 us cached median) beat a custom stdlib asyncio loop (696.3 us) for the target workload and integrated cleanly with Anki. The legacy HTTP/1.0/new-connection path was about 5.7-5.8 ms.

## Audio storage

On a 2,000-file real sample, loose-file open/read was 314.1 us median versus a 1.2 us mmap slice. End-to-end add-on audio HTTP was 1,225.3 us loose versus 549.3 us packed (2.23x faster). The selected row-ID index resolves directly to an immutable pack slice and falls back safely to original files when no pack exists.

## Database regeneration

Per-row insertion with a live index was compared with batched insertion and deferred indexing. Batches of 8,192 plus a deferred index processed 156,720 rows/s, 1.46x the original fixture rate.

## Standalone Rust lookup

Real-data component medians including filtering, absolute URL creation, and JSON serialization:

- preloaded HashMap: 3.5 us, but extra setup/RAM;
- sorted mmap with xxh3 fanout: 3.8 us and near-zero mode setup;
- CHD minimal-perfect-hash table: 4.2 us;
- retained SQLite: 153.8 us.

The final live default is sorted mmap plus mmap pack: it is collision-safe through exact key verification, opens cheaply, and avoids the preload memory cost. CHD showed higher throughput in one repeated HTTP matrix, but the difference was noisy and its component path required a second hash.

Rust mmap pack audio delivered 3.12x the component throughput of individual file opens; at HTTP level it cut median audio latency by 36.6% in the architecture matrix. Eight pack workers processed a warm 10,000-file hash sample 6.18x faster than one worker.

## Correctness and hardening work tried

The implementation went through deterministic tests for stale cache insertion during atomic database publication, memory-mode swaps, concurrent pack leases, reload while an audio request is active, old immutable URLs after reload/restart, close/reload serialization, corrupted same-size index reuse, missing/tampered integrity sidecars, Range/HEAD/304/CORS semantics, and mutable direct-play caching. All reproduced issues were fixed before the current 34-test snapshot.

Full numbers and caveats are kept under `benchmarks/`.

