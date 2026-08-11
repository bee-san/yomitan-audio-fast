# Yomitan Audio RS

A standalone, loopback-only Rust audio server for the legacy Local Audio
Server for Yomichan dataset. It does not require Anki at runtime.

The native runtime does not scan source directories or rebuild a database. A
one-time compile command converts a copied entries.db and its copied audio
folders into:

    bundle/
      manifest.json
      versions/<content-version>/
        lookup.bin
        audio.pack

Compilation reads an existing add-on collection but does not modify its
database or source audio. Runtime needs only the generated bundle.

## Fast architecture

- lookup.bin is memory-mapped and contains every legacy mapping row, reading,
  source, speaker, resolved display name, and stable logical audio ID.
- The default lookup is a sorted XXH3 index with a 65,537-entry fanout. On the
  real dataset it beat CHD while keeping near-zero setup time and no heap map.
- A competing CHD minimal-perfect-hash-style index is in the same artifact.
  An unknown key may land on a slot, so exact UTF-8 bytes are always verified.
- --lookup-mode preload builds a normal in-memory HashMap for the lowest
  possible component lookup latency at the cost of startup and RAM.
- audio.pack concatenates already-compressed audio. Logical recordings and
  voices remain distinct. Two assets share a byte range only after both a
  BLAKE3 match and an exact byte-for-byte comparison.
- HTTP audio bodies own a Bytes view over the pack mapping. The server does
  not allocate or copy an entire clip into a Rust Vec on the pack path.
- /v1/play performs lookup, selection, and playback in one request.

## Build, compile your private bundle, and run

This repository ships source. It does not ship an EXE, database, audio pack,
or private bundle.

From this directory on Windows with stable Rust/MSVC:

```powershell
cargo test --release
cargo build --release

$addonRoot = Join-Path $env:APPDATA 'Anki2\addons21\1045800357'
.\target\release\yomitan-audio-rs.exe compile `
  --addon-root $addonRoot `
  --output ..\bundle `
  --pack-workers 8

.\target\release\yomitan-audio-rs.exe verify --bundle ..\bundle
.\target\release\yomitan-audio-rs.exe serve `
  --bundle ..\bundle `
  --host 127.0.0.1 `
  --port 5052 `
  --lookup-mode sorted `
  --asset-mode pack
```

The process prints a machine-readable READY line only after the manifest,
index, pack bounds, checksum, and collision-safe lookup mapping have validated.

Use this Yomitan-compatible custom audio URL:

    http://127.0.0.1:5052/?term={term}&reading={reading}

expression= is accepted as an alias for term=. The legacy filters are also
supported:

    &sources=nhk16,jpod,forvo
    &user=akitomo,username2

The response shape is exactly:

    {
      "type": "audioSourceList",
      "audioSources": [
        {
          "name": "NHK16 ...",
          "url": "http://127.0.0.1:5052/v/0123456789abcdef/audio/123"
        }
      ]
    }

The origin is generated from the numeric loopback address and actual bound
port, not from the request's untrusted Host header.

## API

| Endpoint | Purpose |
|---|---|
| GET /?term=...&reading=... | Drop-in Yomitan audioSourceList |
| GET /v1/play?term=...&reading=... | Directly stream the highest-priority recording |
| GET /v1/candidates?term=...&reading=... | Rich candidate list with stable IDs, source, speaker, reading, name, MIME, and URL |
| GET /v/<version>/audio/<id> | Immutable opaque audio |
| GET /healthz | Lightweight readiness check |
| GET /v1/info | Bundle, source, mode, and count diagnostics |

HEAD, OPTIONS, single byte ranges (including suffix ranges), ETags, immutable
cache headers on versioned audio, CORS, exact content lengths, and nosniff are
supported.

## One-time compile

Run this against an add-on collection that contains `user_files/entries.db`
and the configured source folders:

```powershell
$addonRoot = Join-Path $env:APPDATA 'Anki2\addons21\1045800357'
.\target\release\yomitan-audio-rs.exe compile `
  --addon-root $addonRoot `
  --output ..\bundle `
  --pack-workers 8
```

Publication is versioned. The large files are completed before the small
top-level manifest switches to the new content version.

The manifest records:

- DB mapping rows and distinct terms;
- logical (source,path) audio assets;
- repeated path references;
- unique exact-byte payloads and content-duplicate assets;
- logical input bytes, packed bytes, and deduplicated bytes;
- preserved distinct speakers;
- pack, CHD, lookup, and total compiler times;
- BLAKE3 digests.

Validate normal startup invariants and the complete lookup hash:

    .\target\release\yomitan-audio-rs.exe verify --bundle ..\bundle

Also stream and hash the multi-GiB pack:

    .\target\release\yomitan-audio-rs.exe verify --bundle ..\bundle --full-pack-hash

## Measured alternatives

All three native lookup candidates are selectable:

    --lookup-mode mph
    --lookup-mode sorted
    --lookup-mode preload

The real-data component benchmark selected sorted as the balanced/default mode:
3.8 microseconds median including filtering, dynamic URL construction and JSON,
versus 4.2 for CHD. Preload reached 3.5 microseconds but added HashMap startup
and resident memory. Retained hot SQLite measured 153.8 microseconds.

The individual-file design remains available solely for real-data A/B
measurement:

    --asset-mode files --legacy-root $addonRoot

The component benchmark includes lookup, reading/source/user filtering,
dynamic absolute URL construction, and JSON serialization:

    .\target\release\yomitan-audio-rs.exe benchmark --bundle ..\bundle --addon-root $addonRoot --output ..\benchmarks\rust-component.json

The HTTP benchmark measures process startup, working set/private bytes,
sequential lookup latency, concurrency, audio latency, HEAD, range, CORS, and
all runtime modes. First export a deterministic mixed real-data corpus, then
run the matrix:

    .\target\release\yomitan-audio-rs.exe export-corpus --bundle ..\bundle --output ..\benchmarks\rust-http-corpus.json --count 2048
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\benchmark-http.ps1 -Exe .\target\release\yomitan-audio-rs.exe -Bundle ..\bundle -LegacyRoot $addonRoot -Corpus ..\benchmarks\rust-http-corpus.json -Output ..\benchmarks\rust-http-architecture.json

The harness sets the response cache capacity to zero when comparing lookup
structures, so a cached JSON response cannot hide index differences.

On this machine, the three-run HTTP medians for sorted+pack were 1,029.6
microseconds for a mixed request and 1,907.7 microseconds for audio. Preload
reduced mixed lookup to 919.6 microseconds but increased startup working set
from 70.9 MiB to 99.2 MiB and median readiness from 655.0 to 1,042.9
milliseconds. Individual-file audio measured 3,009.5 microseconds, making the
pack path 1.58x faster end to end. A separate MPH run showed a 32-worker
throughput edge, so both modes remain available; the final live headline and
launcher use sorted. The raw component and HTTP reports are in the sibling
benchmarks directory.

## Security boundary

- A non-loopback --host is rejected.
- No endpoint accepts a filesystem path.
- Audio URLs contain only a validated hexadecimal version and numeric ID.
- Manifest and legacy relative paths reject absolute paths and parent traversal.
- Manifest bundle files must canonicalize beneath the bundle root.
- Query length, term, reading, source, and user counts are bounded.
- There are no write endpoints and no outbound URL fetcher.
- Corrupt/truncated lookup tables, invalid references, source mismatches,
  invalid audio ranges, and a truncated pack are rejected.

## Build from source

Rust stable with the MSVC target:

    cargo test --release
    cargo build --release

The release profile uses opt-level 3, fat LTO, one codegen unit, stripped
symbols, and abort-on-panic. The generic release EXE is portable across normal
64-bit Windows machines; any host-native experimental build is labeled
separately.
