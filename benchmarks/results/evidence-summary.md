# Yomitan audio acceleration: accumulated benchmark evidence

This report freezes the completed architecture experiments, correctness work, and soak evidence while the final migration-enabled add-on process is being restarted. The last section is intentionally reserved for the bounded three-server smoke run; it will be replaced with measured values from that exact final build.

## System and corpus

- AMD Ryzen AI Z2 Extreme, 8 cores / 16 threads
- Windows 11 Home build 26200; 13.62 GiB RAM; Samsung MZVMA1T0HCLD NVMe
- Anki CPython 3.13.5, SQLite 3.47.1; Rust 1.97.1
- `entries.db`: 590,410 candidate mappings, 280,396 terms, 133.90 MiB
- Copied collection: 382,509 files / 1.853 GiB logical (rounded copy-log total; no exact full-copy byte sum was retained)
- DB-referenced audio: 373,911 distinct `(source,path)` assets and 1,713,724,873 exact logical bytes

The copied collection is isolated from the installed add-on. No `android.db` path is used.

## Add-on lookup architecture experiments

Each Python lookup mode used 9,000 mixed serialized operations over the real database.

| Design | uncached serialized p50 | hot cache | mode setup | incremental WS | Decision |
|---|---:|---:|---:|---:|---|
| Legacy new SQLite connection/request | 2,292.1 µs | — | — | — | baseline |
| Retained immutable SQLite + bounded LRU | 75.0 µs | 0.8 µs | 2.2 ms | 0.14 MiB | selected |
| Full in-memory HashMap | 34.5 µs | 0.8 µs | 5,760 ms | 250 MiB | rejected: startup/RAM |
| mmap open-address hash index | 72.4 µs | 0.8 µs | 2.1 ms | 0.01 MiB | rejected: no end-to-end advantage, worse first-touch tail |

The selected add-on keeps SQLite’s exact source/user filtering and candidate order, returning stable row IDs. Each row ID then resolves in O(1) through a fixed 16-byte mmap record into a lazily mapped pack slice. `audio.idx` is exactly 9,446,640 bytes: a 64-byte header plus 590,411 records (9,446,576 bytes). The 1,713,724,873-byte pack is not bulk-loaded.

Raw SQL alternatives were also measured: selected `reading IS NULL OR reading=?` was 23.5 µs p50 / 77.7 µs p95; term-only SQL plus Python filtering was 23.4/628.2 µs; `UNION ALL` was 27.45/91.6 µs.

HTTP server architecture search:

| Server path | cached keep-alive | uncached/unique keep-alive | new connection cached | Decision |
|---|---:|---:|---:|---|
| Legacy HTTP/1.0 + new DB connection | 5,698.2 µs | 5,815.7 µs | same request model | baseline |
| `ThreadingHTTPServer`, HTTP/1.1 | 649.3 µs | 795.7 µs | 3,386.2 µs | selected |
| Dedicated stdlib asyncio loop | 696.3 µs | 858.8 µs | 2,698.2 µs | not selected |

The final add-on and Rust pack paths are NTFS hardlinks, so they share one physical pack payload allocation. Loose copied files remain present for drop-in compatibility; no claim is made that the current lab folder realizes the counterfactual loose-file removal saving.

## Add-on audio component experiment

Real 2,000-file sample:

| Path | component p50/p95 | keep-alive HTTP p50/p95 |
|---|---:|---:|
| Individual file open/read | 314.1 / 611.6 µs | 1,225.3 / 1,552.6 µs |
| mmap pack slice | 1.2 / 3.7 µs | 549.3 / 776.8 µs |

The 1.2 µs value measures pack-slice view creation only. It is not directly comparable to the Rust payload-touch-and-hash microbenchmark below.

## Add-on correctness, publication, and soak audit

- Final regression/lifecycle/corruption suite: 28/28 passed in 8.593 s under Anki CPython 3.13.5 with `ResourceWarning` promoted to an error.
- The reviewer deterministically reproduced the old stale-publication race (`cached old`), then verified the epoch/lease repair: publication waited for the active DB reader and the next lookup returned new data.
- Two packed leases overlapped. Reload completed while an old lease remained open; that immutable historical version still returned exact `OggS-old` bytes, and every holder/test thread completed.
- HTTP/config coverage included legacy and packed play/cache behavior, full/open/suffix/ranged-HEAD semantics, clean 304, conditional preflight headers, malformed overlay handling, and resource closure.
- Packed 16-client soak: 2,064 requests, 0 errors, 0 reconnects; pool peak/end was 16/16 idle. HEAD, Range, 304, CORS, direct play, and SHA-256 audio parity all passed.
- Initial Rust-bundle import: 9.788 s. Hardened SHA-256-verified reimport: 12.375 s (13.615 s wall). Full lookup/index + pack BLAKE3 verification: 1.677 s.
- Required Rust sidecars passed: lookup SHA-256 `ad32f820f695d71f5ac8e77bee58325bb401c775921d8abb485d4749a2498270`; pack SHA-256 `ee099859f3a3e40cde9602a065b9a8091d0ab4d35864e136c481b2ee9e1dafee`. Published add-on index SHA-256: `1e2663083fcd1029a22c5078c70f589ea69d6eb99b9c98123accbb692af43327`.

### Exhaustive add-on compatibility audit

An independent legacy SQL/reference implementation and the optimized store compared 743,634 real cases in 70.185 s with zero mismatches. SQLite `quick_check` was `ok`; the cache-disabled audit ended with one idle read-only connection and no cache growth.

| Matrix | Cases | returned rows | ordered SHA-256 |
|---|---:|---:|---|
| Every term-only expression | 280,396 | 590,410 | `9661d6d3f2144bdfb2d3c22814e072b3455effbfced3a57362a9deccd5f7494f` |
| Every distinct non-null expression/reading key | 242,976 | 540,267 | `7660a74487bedb87d85e7804105ca8441a13db8f5d3c957802ba37ffc2f48125` |
| Ordered source subsets/permutations | 51,675 | 467,712 | `b8a517a7c2c5b43a964eecdc36b78e61f88fe8f38230a5eb844199648c02d29e` |
| Comprehensive Forvo filters, all five speakers | 168,587 | 470,132 | `fbb6e6eaeb1c4803402adb96107afe381d78b6157347434a962e3623b94319f7` |

### Desktop database regeneration experiment

Representative 100,000-row in-memory ingestion fixture, seven trials per design:

| Insert/index plan | median | rows/s | relative to original |
|---|---:|---:|---:|
| Per-row inserts + live index | 0.9321 s | 107,280 | 1.00× |
| Per-row inserts + deferred index | 0.7531 s | 132,776 | 1.24× |
| 8,192-row batches + live index | 0.8352 s | 119,730 | 1.12× |
| 8,192-row batches + deferred index | 0.6381 s | 156,720 | 1.46× selected |

## Native lookup component experiments

Each native mode used 20,000 deterministic real lookups with response cache bypassed. Timings include reading/source/user filtering, absolute URL construction, and JSON serialization.

| Design | p50/p95/p99 | setup after shared open | component ops/s | Decision |
|---|---:|---:|---:|---|
| Preloaded HashMap | 3.5 / 8.8 / 14.0 µs | 137.498 ms | 220,790 | HTTP finalist, rejected on balance |
| Sorted mmap + xxh3 fanout | 3.8 / 11.0 / 21.7 µs | 0.017 ms | 190,404 | HTTP finalist |
| CHD minimal perfect hash mmap | 4.2 / 12.2 / 21.7 µs | ~0 ms | 172,245 | selected after HTTP matrix |
| Retained hot SQLite | 153.8 / 926.6 / 1,777.9 µs | 0.761 ms | 3,842 | rejected |

Shared bundle/index checksum and deep-validation open was 322.435 ms. Sorted, MPH, and preload serialized bodies were byte-identical. A separate 2,048-query differential test passed exact ordered `(name, source, speaker, reading)` tuple parity against SQLite; SQLite’s body checksum differed only because it emits legacy row-ID URLs.

Native warm real-data audio component (2,000 assets / 9,086,003 bytes, payload touched and hashed): mmap pack 358.3/563.1 µs p50/p95 versus individual open/read 721.5/2,069.3 µs. Output checksums matched. Eight-worker bundle prefetch processed 25,571 files/s versus 4,135 with one worker (6.18×).

## Native repeated HTTP architecture matrix

Three fresh-process trials per mode used the same 2,048-query corpus with response caching disabled: 4,096 mixed lookups, 1,000 hot lookups, 2,048 requests at 32 workers, and 200 audio GETs per trial. Rows are medians of trial-level values.

| Mode | startup | WS/private at ready | mixed p50/p95 | hot p50 | 32-worker req/s | audio p50/p95 | Decision |
|---|---:|---:|---:|---:|---:|---:|---|
| CHD MPH + pack | 538.255 ms | 70.89/5.12 MiB | 1,079.5/2,077.9 µs | 1,146.3 µs | 9,065 | 1,722.2/3,218.6 µs | selected |
| Sorted mmap + pack | 655.018 ms | 70.88/5.11 MiB | 1,029.6/2,340.0 µs | 1,135.8 µs | 7,420 | 1,907.7/4,051.2 µs | not selected |
| Preloaded HashMap + pack | 1,042.892 ms | 99.16/33.91 MiB | 919.6/1,767.9 µs | 971.8 µs | 7,612 | 1,818.7/3,467.5 µs | not selected |
| Sorted mmap + individual files | 528.042 ms | 70.92/5.15 MiB | 956.1/2,174.6 µs | 1,058.9 µs | 7,125 | 3,009.5/5,167.9 µs | asset mode rejected |

MPH+pack was the best balanced desktop choice: 32-worker throughput was 22.2% above sorted+pack and 19.1% above preload; packed-audio latency was 42.8% lower than individual files, while avoiding preload’s roughly 28.3 MiB extra startup working set and 1.94× startup median.

The matrix predates only the additive rich-candidate `reading` field rebuild; lookup, audio, index, and transport paths are unchanged. The final portable native executable is 3,341,312 bytes, SHA-256 `55540c1820555eca700d22f7003fc26b59ea556f91254651c6d689d41e7d4e4c`; 4/4 release tests passed.

## Mapping aliases and content duplication

The bundle compiler retained all 590,410 mapping rows. They reference 373,911 distinct source paths, so 216,499 additional mapping aliases remain in postings to preserve candidate count, order, and voice names. Compiler exact-byte checks found 373,911 unique payloads and zero byte-identical assets at distinct paths: byte dedup saved 0 B, and the pack size exactly equals DB-referenced audio bytes. This is compiler evidence; the long independent 373,911-file SHA-256 rescan was intentionally skipped after the user requested an urgent bounded finish.

The 1.853 GiB full-copy figure is rounded and includes more than DB-referenced audio. A contemporaneous free-space delta was confounded by toolchain/artifact creation and is not used as physical-size evidence.

## Legacy diagnostic baseline

An earlier original-only smoke (before the final harness hardening) passed 25/25 focused cases and byte-checked 63 candidates. Warm compatibility lookup p50 was 5.867 ms with connection-close and 5.933 ms when keep-alive was requested; the legacy HTTP/1.0 server closed responses, so requested keep-alive did not provide reuse. Two-stage lookup + first audio p50 was 10.579/11.044 ms.

The legacy compatibility response returns `localhost` audio URLs. Independent Anki-Python trials of the same clip were 2,086.73, 2,047.95, 2,047.49, 2,042.99, and 2,044.20 ms as returned versus 4.35, 4.37, 4.50, 4.27, and 4.19 ms with `127.0.0.1`. `getaddrinfo` was sub-millisecond after its first call, identifying IPv6 connect/fallback—not DNS—as the roughly two-second penalty on this client. Fair server comparisons normalize equivalent legacy loopback URLs to `127.0.0.1`; new servers return numeric-loopback URLs directly.

## Final migration-build three-server smoke

**Pending final 5051 restart.** This section will be replaced by the fail-closed original/Anki/Rust smoke result from the exact migration-enabled packed build. It will require dedicated endpoint shapes, rich candidate fields, exact count/name/source/URL order, numeric stable URLs, SHA-256 audio parity, CORS/preflight, ETag/304, HEAD, full/open/suffix/unsatisfiable Range, ranged HEAD, direct play, and bounded warm/concurrent timing.
