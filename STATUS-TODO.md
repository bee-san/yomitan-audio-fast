# Handoff status and TODO

## Completed

- Original installed add-on was left untouched; all development used a copied collection.
- Desktop-only drop-in add-on implemented; Android/`android.db` paths removed from scope.
- Existing collection folder picker added to Anki Tools.
- Same-ID upgrades retain `user_files` and automatically attempt one background fast-pack build when a valid DB and referenced audio exist but no valid pack is active.
- Migration validates `entries.db`, copies metadata atomically when needed, merges absolute source paths safely, never moves/deletes original audio, and shares one job gate with regeneration/manual pack work.
- Retained-SQLite/LRU lookup, HTTP/1.1 server, direct play/candidate endpoints, mmap pack, ranges, HEAD, ETag/304, CORS, lifecycle, publication epochs, pack leases, historical versions, and integrity verification implemented.
- Current add-on source passed 34/34 tests under Anki CPython 3.13.5 with `ResourceWarning` treated as an error.
- Standalone Rust compiler/server implemented with sorted mmap, CHD, and preload modes; final release tests passed 4/4.
- Architecture and correctness evidence is checked in under `benchmarks/`.

## Still worth doing

1. Run the fail-closed three-server smoke harness against the exact migration-enabled 5051 process and append it to `benchmarks/results/evidence-summary.md` (the accumulated component/exhaustive/soak evidence is already present).
2. Rebuild the distributable add-on ZIP/checksum after any future source edit and audit it for forbidden `user_files`, DB, audio, pack, cache, and bytecode entries.
3. Add CI jobs for Anki-Python unit tests, Rust format/clippy/test, and the repository payload denylist.
4. Test the folder picker manually inside a clean Anki profile using a copied small fixture and a same-ID upgrade using preserved `user_files`.
5. Consider `If-Range` and weak ETag semantics; Yomitan does not depend on them, so they were not release blockers.
6. Consider a signed downloadable Rust release and a small installer only after deciding how users should obtain/build private audio bundles locally.

## Local lab endpoints at handoff

- Original installed add-on: `http://127.0.0.1:5050/`
- Optimized copied add-on: `http://127.0.0.1:5051/`
- Standalone Rust: `http://127.0.0.1:5052/`

Those processes and private payloads are not part of this repository and will not persist on another machine. Recreate them from `DATA-LAYOUT.md`.
