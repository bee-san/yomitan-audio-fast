# Optimized Anki add-on: component benchmark record

Measured 2026-08-11 on the copied production collection (590,410 mapping
rows, 280,396 expressions, 373,911 distinct source/path assets) using Anki's
bundled CPython 3.13.5 and SQLite 3.47.1. Times are medians unless marked
otherwise. The original installed add-on was not modified.

## Lookup architecture search

| Architecture | Uncached lookup + serialization | Cache hit | Startup | Added working set | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Legacy-style new SQLite connection/query per request | 2,292.1 us | n/a | n/a | n/a | Baseline |
| Retained immutable SQLite pool + bounded LRU | 75.0 us | 0.8 us | 2.23 ms | 0.14 MiB | Selected |
| Full Python in-memory preload | 34.5 us | 0.8 us | 5.76 s | 250.0 MiB | Optional, not default |
| Compiled mmap open-address hash + postings | 72.4 us | 0.8 us | 2.11 ms | 0.01 MiB | Rejected for runtime complexity/no material gain |

The retained SQLite design won for the add-on because it is within 2.6 us of
the compiled Python hash median, starts in milliseconds, preserves SQLite's
exact filtering/order semantics, and avoids the preload's 250 MiB cost. The
response LRU makes repeat lookups equivalent across the three optimized
designs. A separate native release bundle evaluates sorted mmap and CHD/MPH;
those results are recorded in the lab-level benchmark report.

The selected SQL shape (`reading IS NULL OR reading=?`) measured 23.5 us at
the raw-query layer with a 77.7 us p95. Term-only SQL followed by Python
filtering had a similar 23.4 us median but a 628.2 us p95; `UNION ALL` measured
27.45 us median / 91.6 us p95.

## HTTP architecture search

| Server path | Cached keep-alive | Uncached/unique keep-alive | New connection (cached) |
| --- | ---: | ---: | ---: |
| Legacy HTTP/1.0 + new DB connection | 5,698.2 us | 5,815.7 us | same request model |
| `ThreadingHTTPServer`, HTTP/1.1 | 649.3 us | 795.7 us | 3,386.2 us |
| Dedicated stdlib asyncio loop | 696.3 us | 858.8 us | 2,698.2 us |

The threaded HTTP/1.1 implementation was selected: it had the best reused
connection latency and integrates cleanly with Anki. It disables request
logging, enables `TCP_NODELAY`, retains database connections in a bounded
cross-thread pool, and supports keep-alive, `HEAD`, ETag, and byte ranges.

## Audio path search (2,000 real files, 9,110,681 sampled bytes)

| Architecture | Median | p95 | p99 |
| --- | ---: | ---: | ---: |
| Individual file open + read | 314.1 us | 611.6 us | 771.0 us |
| mmap pack slice | 1.2 us | 3.7 us | 5.1 us |
| HTTP keep-alive, loose file | 1,225.3 us | 1,552.6 us | 1,803.8 us |
| HTTP keep-alive, mmap pack | 549.3 us | 776.8 us | 870.7 us |

The mmap pack reduced component access about 262x and sampled end-to-end
audio HTTP latency about 2.23x. The runtime maps the pack lazily, so the 1.71
GiB file does not become startup resident memory.

## Real pack import/publication

- Rust source bundle version: `70ba998db1c770c4`
- Add-on row-index version: `bbdb8223f72ede22`
- Initial merge-stream import: 9.788 s
- Hardened SHA-256-verified reimport: 12.375 s (13.615 s wall)
- Mapping rows indexed: 590,410
- Distinct source/path assets: 373,911
- Repeated mapping references to an existing source/path: 216,499
- Byte-identical assets at different source/path keys: 0
- Pack bytes: 1,713,724,873
- Fixed row-ID index: 9,446,640 bytes (16 bytes per possible row ID plus header)
- Full Rust lookup + pack BLAKE3 verification: passed separately in 1.677 s
- Required SHA-256 sidecar verification: lookup
  `ad32f820f695d71f5ac8e77bee58325bb401c775921d8abb485d4749a2498270`;
  pack `ee099859f3a3e40cde9602a065b9a8091d0ab4d35864e136c481b2ee9e1dafee`
- Published add-on index SHA-256:
  `1e2663083fcd1029a22c5078c70f589ea69d6eb99b9c98123accbb692af43327`
- Publication: verified NTFS hardlink to the Rust pack; no second payload copy

The abandoned loose-file Python pack pass reached 30,000 paths in roughly
1.5-2.5 minutes (about 200-330 paths/s), implying roughly 19-31 minutes for
the real 373,911-file corpus. It was stopped without publishing. The final
importer instead reads only the Rust metadata table and SQLite rows, checks
every key/order/path/range, and publishes the 9.45 MiB row index atomically.

## Desktop database regeneration

A representative 100,000-row in-memory ingestion fixture was run seven times
per architecture:

| Insert/index plan | Median | Rows/s | Relative to original |
| --- | ---: | ---: | ---: |
| Per-row inserts + live index | 0.9321 s | 107,280 | 1.00x |
| Per-row inserts + deferred index | 0.7531 s | 132,776 | 1.24x |
| 8,192-row batches + live index | 0.8352 s | 119,730 | 1.12x |
| 8,192-row batches + deferred index | 0.6381 s | 156,720 | 1.46x |

The selected rebuild path uses 8,192-row `executemany` batches, creates the
index after ingestion, verifies the temporary database, and then atomically
replaces the active database.

## Final live validation

The exact add-on standalone process on port 5051 started at 38.14 MiB working
set / 25.75 MiB private bytes before requests with the pack mapped. A real
U+732B (cat) / U+306D U+3053 (reading) request returned seven ordered
candidates. `/v1/play` matched
the first compatibility candidate byte-for-byte (SHA-256
`c3e27fc1131e8ac5561916a610810d941220c256eafb4d7500a54b5ed63948c4`),
`HEAD` returned the correct length, and `Range: bytes=0-1023` returned 206 and
exactly 1,024 bytes.

An exhaustive independent offline differential audit then compared 743,634
real selection cases in 70.185 s with zero mismatches: every 280,396 term-only
expression, every 242,976 distinct non-null exact expression/reading key,
51,675 ordered source subset/permutation cases, and 168,587 comprehensive
Forvo cases spanning all five speakers. Ordered output digests are recorded in
`ANKI_VALIDATION.json`.

The final race-hardened server (PID 4176, pack `bbdb8223f72ede22`) passed a
bounded 16-client HTTP/1.1 soak: 2,064 requests in 1.447 s, zero errors, zero
unexpected reconnects, pool peak/end 16/16 with all 16 idle at completion.
Diagnostic lookup latency was 11.160 ms p50 / 23.402 ms p95 / 33.188 ms p99;
audio/protocol latency was 4.194 / 7.525 / 9.638 ms. `HEAD`, Range, 304,
CORS, and `/v1/play` SHA equality all passed. Working set moved from
38,703,104 to 67,219,456 bytes and private bytes from 26,734,592 to
36,835,328 after warming 513 row/response cache entries and 16 idle SQLite
connections; thread count returned to 8 and pack leases returned to zero.

The lab-level `benchmarks` directory contains the independent original/new
HTTP harness, architecture JSON, duplication audit, startup specifications,
and machine-readable final results.
