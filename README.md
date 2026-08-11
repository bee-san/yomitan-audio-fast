# Yomitan Audio Fast

A desktop-only, drop-in acceleration project for the Yomitan Local Audio Server Anki add-on, plus a standalone Rust server. It preserves Yomitan's existing JSON contract and candidate order while removing per-request SQLite connections and loose-file overhead.

This repository intentionally contains **no audio, database, compiled bundle, or executable payloads**. Existing add-on users keep their `user_files` directory. The optimized add-on can reuse that data automatically, or the Anki Tools menu action **Local Audio Server -> Import/process existing audio folder...** can point at an existing add-on root, `user_files` directory, collection root, or recognized source folder. Originals are never moved or deleted.

## What is here

- `anki-addon/`: desktop-only drop-in add-on source, migration UI, tests, and pack compiler/importer.
- `rust-server/`: standalone Rust server/compiler source.
- `benchmarks/`: architecture experiments, correctness audits, machine-readable measurements, and harnesses.
- `DATA-LAYOUT.md`: map of the omitted audio/database artifacts and how to recreate them.
- `ATTEMPTS.md`: architectures tried and why the final designs were selected.
- `STATUS-TODO.md`: exact handoff state and remaining validation work.

## Fast start for an existing add-on user

1. Back up the installed add-on directory.
2. Overlay `anki-addon/` onto the existing add-on directory with the same Anki add-on ID. Do **not** delete `user_files`.
3. Start Anki. If a valid existing `entries.db` and referenced audio are found and no valid pack is active, the add-on schedules one background pack build for that data fingerprint.
4. Alternatively choose **Tools -> Local Audio Server -> Import/process existing audio folder...** and select the old add-on root or its `user_files` directory.
5. Configure Yomitan with `http://127.0.0.1:5050/?term={term}&reading={reading}` (or the port configured in the add-on).

The lab used ports 5051 (optimized add-on) and 5052 (Rust) so the original installed server on 5050 remained untouched.

## Validation snapshot

- 34/34 current add-on tests passed under Anki CPython 3.13.5 with `ResourceWarning` promoted to an error.
- 743,634 real-data compatibility cases produced zero mismatches.
- A 2,064-request, 16-client packed add-on soak completed with zero errors or unexpected reconnects.
- Rust release tests: 4/4.
- See `benchmarks/results/evidence-summary.md` and `anki-addon/benchmarks/RESULTS.md` for full evidence.

## Security and scope

Both servers bind loopback only by default. Media requests use opaque IDs or validated paths, enforce bounds/ranges, and expose no write or outbound-fetch endpoint. This project deliberately ignores `android.db` and targets desktop Anki.

