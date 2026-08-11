# Fast Anki add-on

Desktop-only, same-ID replacement for the Yomitan Local Audio Server add-on. It keeps the legacy JSON contract and existing `user_files`, while replacing the request hot path with retained SQLite connections, bounded caches, HTTP/1.1 keep-alive, and an optional memory-mapped audio pack.

## Install over an existing collection

1. Close Anki.
2. Copy this directory's contents over `%APPDATA%\Anki2\addons21\1045800357`.
3. Keep the existing `user_files` directory. Do not replace or delete it.
4. Start Anki.

The server binds to `127.0.0.1:5050` by default. Configure a Yomitan Custom JSON source with:

```text
http://127.0.0.1:5050/?term={term}&reading={reading}
```

## Anki Tools menu

**Tools → Local Audio Server** contains:

- **Regenerate desktop database** — rebuild metadata from the configured source adapters.
- **Import/process existing audio folder…** — select an old add-on root, `user_files`, collection root, or recognized source folder; validate and adopt it without moving original audio.
- **Build/rebuild fast desktop audio pack** — create the fixed row index and immutable pack locally.
- **Show statistics** — show database, active pack, and source status.

Only one import/regeneration/pack job runs at a time. Database and pack publication are atomic; active requests retain a safe lease on the version they started with.

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

## HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /?term=...&reading=...` | Drop-in Yomitan `audioSourceList` |
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

## Build a code-only ZIP

From this directory:

```powershell
.\build-code-only-package.ps1 -OutputPath ..\local-audio-fast-anki-addon-code-only.zip
```

The package deliberately excludes `user_files`, databases, audio, packs, caches, and bytecode.

## Scope

- Tested on Windows 11 with Anki 25.09.5 / CPython 3.13.5.
- Desktop only; `android.db` is not supported.
- Loopback only by default.
- Original audio is never moved or deleted by the importer or pack builder.

Return to the [project README](../README.md) for benchmark results and the architecture comparison.
