# Project status

## Complete

- Desktop-only, same-ID Anki replacement with existing-collection import UI.
- Retained SQLite connections, bounded caches, HTTP/1.1 keep-alive, direct play, fixed row-ID mmap index, immutable mmap audio pack, loose-file fallback, ranges, HEAD, ETags/304, CORS, and versioned URLs.
- Atomic database/pack publication, generation-aware caches, overlapping pack leases, historical immutable versions, shutdown/reload serialization, and integrity verification.
- Standalone Rust compiler/server with sorted mmap, MPH, and preload lookup modes plus packed and individual-file A/B paths.
- Exhaustive add-on audit: 743,634 real cases, zero mismatches.
- Packed add-on soak: 2,064 requests across 16 clients, zero errors and zero reconnects.
- Corrected three-server live smoke: 57/57 cases per endpoint, 108 audio candidates SHA-256 checked per endpoint.
- Higher-sample standard performance run and raw metrics published under [`benchmarks/results/`](benchmarks/results/).
- Code-only packaging denylist excludes audio, databases, packs, bundles, executables, caches, and bytecode.
- Same-ID `.ankiaddon` packaging, drag-and-drop replacement/import, determinate progress, cooperative cancellation, and crash-safe pack checkpoints.
- GitHub Actions coverage for the Python suite, code-only package validation, downloadable artifacts, and tagged `.ankiaddon` releases.

## Next useful work

1. Add CI for `cargo fmt`/Clippy/release tests, Markdown links, and SVG validation.
2. Test the folder picker and same-ID overlay in a completely clean Anki profile with a small copied fixture.
3. Add release signing/attestation and, if desired, a portable Rust EXE. Private bundles/audio must still be built locally.
4. Measure and document macOS/Linux behavior before describing the project as cross-platform.
5. Consider `If-Range` and weak ETag semantics. Yomitan does not currently depend on them.
6. Rerun the full standard three-server benchmark with the corrected `416` assertion if a single all-green higher-sample report is desired. The existing standard performance data and corrected final smoke are documented separately and transparently in [`benchmarks/results/FINAL.md`](benchmarks/results/FINAL.md).
