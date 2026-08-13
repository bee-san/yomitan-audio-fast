# Fast Anki add-on

Desktop-only, same-ID replacement for the Yomitan Local Audio Server add-on. It keeps the legacy JSON contract and existing `user_files`, while replacing the request hot path with retained SQLite connections, bounded caches, HTTP/1.1 keep-alive, and an optional memory-mapped audio pack.

## Install over an existing collection

1. Build or download `local-audio-fast.ankiaddon`.
2. In Anki, choose **Tools → Add-ons → Install from file…**.
3. Select the package and restart Anki.
4. When **Existing audio found** appears, click **Yes** to accelerate the preserved collection in place.

The package uses the original add-on ID (`1045800357`), so Anki replaces the old code and preserves its `user_files` for you. The add-on detects that database and its configured source folders automatically; drag-and-drop is only needed for a collection stored elsewhere.

The server binds to `127.0.0.1:5050` by default. Configure a Yomitan Custom JSON source with:

```text
http://127.0.0.1:5050/?term={term}&reading={reading}
```

For an opt-in single-candidate lookup, use:

```text
http://127.0.0.1:5050/v1/first?term={term}&reading={reading}
```

It returns only the first candidate in configured source order. Source and user
filters are intentionally ignored; use the compatible root endpoint when the
complete candidate list, request filters, or fallback candidates are needed.

## Anki Tools menu

**Tools → Local Audio Server** contains:

- **Regenerate desktop database** — rebuild metadata from the configured source adapters.
- **Import existing audio collection…** — drop or browse to an old add-on root, `user_files`, collection root, or recognized source folder; validate and adopt it without moving original audio.
- **Build/rebuild fast desktop audio pack** — create the fixed row index and immutable pack locally.
- **Move verified loose audio to Trash…** — after full verification, reclaim managed loose-file space without touching metadata, external roots, or unreferenced files.
- **Restore/verify loose audio originals…** — put a cleanup folder restored from Trash back into its exact source paths and leave packed-only mode.
- **Show statistics** — show database, active pack, and source status.

Only one import, regeneration, pack, cleanup, or restore job runs at a time. Pack work has determinate progress and a visible Cancel button. Completed work is checkpointed every few seconds; a retry or later Anki start resumes it. Database and pack publication are atomic, and active requests retain a safe lease on the version they started with.

## Pack layout

```text
user_files/fast_audio/
  active.json
  versions/<version>/
    manifest.json
    audio.idx
    audio.pack
```

`audio.idx` maps a stable SQLite row ID to a 16-byte offset/length/MIME record. `audio.pack` concatenates the original compressed payloads without recompressing them. Both are mapped lazily; the entire pack is not loaded into memory.

If no valid pack exists, the server safely falls back to the original loose files.

## Optional verified cleanup

After a fully covered pack is active, cleanup is offered as a separate confirmation with **No** as the default. The add-on freshly verifies SHA-256 for `audio.idx` and `audio.pack`, checks every current database row, and confirms that all source roots belong to this add-on's own `user_files`. Each loose file is byte-compared with its packed record and recorded in an independent per-file SHA-256 recovery inventory, then atomically moved into `fast_audio/loose-audio-originals-v1`. The database and pack are reverified before and after this single generated quarantine directory reaches the operating system Trash.

Cleanup removes only exact database-referenced regular audio files. It never removes source directories, external/shared roots, symbolic links or junctions, source metadata, config, `entries.db`, unreferenced files, or the pack, and it never falls back to permanent deletion. A durable `packed-only-v1.json` journal makes quarantine and pre-Trash failures resumable. Cached legacy audio URLs also fall back to the active pack.

After cleanup, database regeneration, import, and pack rebuilding are blocked because the verified pack is the serving audio copy. To recover, restore `loose-audio-originals-v1` from Trash into `user_files/fast_audio`, then run **Restore/verify loose audio originals…**. The recovery inventory can verify and restore originals even if the pack or database was damaged. Test playback before emptying Trash; disk space is not reclaimed until Trash is emptied.

## HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /?term=...&reading=...` | Drop-in Yomitan `audioSourceList` |
| `GET /v1/first?term=...&reading=...` | Single-candidate Yomitan list using configured source priority |
| `GET /v1/play?term=...&reading=...` | Select and stream the first permitted recording |
| `GET /v1/candidates?term=...&reading=...` | Rich candidates with stable IDs and source metadata |
| `GET /v/<version>/audio/<id>` | Immutable packed audio |
| `GET /healthz` | Readiness |
| `GET /v1/info` | Runtime/database/pack diagnostics |

Versioned media supports `HEAD`, single ranges, ETags/304, CORS, immutable caching, exact content lengths, and `nosniff`. `/v1/play` is `no-store` because its selected result can change after a database or source-order update.

## Standalone development runner

The runner loads the exact add-on package without starting Anki:

```powershell
& "$env:LOCALAPPDATA\AnkiProgramFiles\.venv\Scripts\python.exe" `
  .\standalone.py `
  --root . `
  --port 5051 `
  --lookup-mode sqlite
```

Build a Python pack without serving:

```powershell
& "$env:LOCALAPPDATA\AnkiProgramFiles\.venv\Scripts\python.exe" `
  .\standalone.py `
  --root . `
  --build-pack `
  --build-only
```

The optional Rust-bundle importer verifies a SHA-256 integrity sidecar, database identity, every record bound, source/path ordering, and the final NTFS hardlink before activation.

## Build the installable add-on

From this directory:

```powershell
.\build-code-only-package.ps1 -OutputPath ..\local-audio-fast.ankiaddon
```

The package deliberately excludes `user_files`, databases, audio, packs, caches, and bytecode.

## Scope

- Tested on Windows 11 with Anki 25.09.5 / CPython 3.13.5.
- Desktop only; `android.db` is not supported.
- Loopback only by default.
- The importer and pack builder never move original audio. The separate verified cleanup action can move eligible managed originals to the operating system Trash only after explicit confirmation.

Return to the [project README](../README.md) for benchmark results and the architecture comparison.
