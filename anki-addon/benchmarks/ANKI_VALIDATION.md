# Final Anki add-on validation

The desktop-only replacement passed all final gates against the copied real
collection. The original installed add-on remained untouched.

## Exhaustive compatibility audit

An independent legacy SQL/reference implementation and the optimized store
produced identical ordered names/rows for all 743,634 audited cases in
70.185 seconds, with zero mismatches:

| Matrix | Cases | Returned rows | Ordered SHA-256 |
| --- | ---: | ---: | --- |
| Every term-only expression | 280,396 | 590,410 | `9661d6d3f2144bdfb2d3c22814e072b3455effbfced3a57362a9deccd5f7494f` |
| Every distinct non-null exact expression/reading key | 242,976 | 540,267 | `7660a74487bedb87d85e7804105ca8441a13db8f5d3c957802ba37ffc2f48125` |
| Ordered source subsets/permutations | 51,675 | 467,712 | `b8a517a7c2c5b43a964eecdc36b78e61f88fe8f38230a5eb844199648c02d29e` |
| Comprehensive Forvo filters (all five speakers) | 168,587 | 470,132 | `fbb6e6eaeb1c4803402adb96107afe381d78b6157347434a962e3623b94319f7` |

SQLite `quick_check` was `ok`. The cache-disabled audit ended with one idle
read-only connection and no cache growth.

## Lifecycle, race, and corruption suite

All 34 tests passed in 8.524 seconds with `ResourceWarning` promoted to an
error. Besides API/HTTP behavior, deterministic concurrency tests cover DB
epochs, atomic memory publication, reload during serialization, blocked
audio writers, deferred mmap close, historical immutable URLs across
reload/restart, direct-play selection swaps, and serialized close/reload.
Importer tests reject missing or wrong integrity sidecars, same-size lookup
or pack corruption, and same-size corruption of a previously published
index. Migration fixtures additionally verify add-on/user_files/source-folder
discovery, validated DB copy without changing originals, atomic absolute-path
config persistence, and one automatic attempt per unchanged collection.

## Verified real pack

The hardened import verified the required SHA-256 sidecar, merge-streamed
590,410 DB rows against 373,911 Rust assets, and republished add-on version
`bbdb8223f72ede22` in 12.375 seconds (13.615 seconds wall). The
1,713,724,873-byte pack is an NTFS hardlink to the verified Rust pack; the
9,446,640-byte add-on index SHA-256 is
`1e2663083fcd1029a22c5078c70f589ea69d6eb99b9c98123accbb692af43327`.

## Bounded final HTTP/1.1 soak

Final PID 4176 served 2,064 requests from 16 persistent concurrent clients
in 1.447 seconds: 1,600 lookups, 320 ranges, 80 `HEAD`, 48 conditional
`304`, and 16 CORS preflights. There were zero errors and zero unexpected
reconnects. The SQLite pool peaked at 16 and ended with all 16 connections
idle; pack leases ended at zero.

Diagnostic latency (this is a correctness soak, not the independent
before/after comparison): lookup p50/p95/p99 was 11.160/23.402/33.188 ms;
audio/protocol was 4.194/7.525/9.638 ms. Working set changed from 38,703,104
to 67,219,456 bytes, private bytes from 26,734,592 to 36,835,328, thread
count returned to 8, and handles changed from 201 to 233 after warming 513
cache entries and 16 retained SQLite connections. Packed URLs, direct-play
SHA equality, `HEAD`, Range, clean `304`, CORS, connection reuse, pool bounds,
and pool quiescence all passed.

Machine-readable details are in `ANKI_VALIDATION.json`.
