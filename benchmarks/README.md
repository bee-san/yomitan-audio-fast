# Independent benchmark and parity suite

This folder compares the live installed add-on (`127.0.0.1:5050`), the copied optimized add-on (`127.0.0.1:5051`), and the standalone Rust server (`127.0.0.1:5052`). It never edits the installed add-on or audio collection.

The benchmark uses Anki's bundled Python and only its standard library. It reads the copied `entries.db` to build a deterministic real-data corpus, then treats the live original server as the behavior oracle.

## What it verifies

- Real hits from every populated source, misses, omitted readings, and the `expression=` alias.
- Source subsets and priority order plus real Forvo `user=` filters/order.
- Exact candidate count and name order.
- Source identity where URLs expose it, plus SHA-256/length parity of audio bytes for every checked candidate.
- Legacy HTTP bytes against the copied on-disk source file.
- Dedicated-shape `/healthz`, `/v1/info`, `/v1/candidates`, and `/v1/play` behavior, so the legacy catch-all cannot masquerade as a new endpoint.
- The complete rich candidate contract: `audioId`, `source`, `speaker`, `reading`, `name`, `mime`, and stable numeric-loopback `url`.
- Extension CORS headers and `OPTIONS` preflight behavior.
- Stable audio ETags and empty-body `If-None-Match` / `304 Not Modified` behavior.
- Audio `HEAD` plus prefix, middle, open-ended, full-span, oversized-suffix, unsatisfiable, and ranged-`HEAD` behavior (including exact bytes and `Content-Range`).
- Numeric `127.0.0.1` candidate URL policy so a test server cannot accidentally send benchmark downloads to the original hard-coded port.

## What it measures

- Warm lookup p50/p95/p99 and complete response size over repeated trials.
- Two-stage lookup-then-first-audio latency.
- Direct `/v1/play` latency when supported.
- Connection-close and keep-alive separately; JSON records whether the server actually closed each response.
- Lookup and full-audio concurrency at several worker counts, with throughput and tail latency.
- First timed post-setup lookup/audio touch, explicitly **not** a process-cold or cold-disk read.
- Status/headers, first body byte (TTFB), and complete-body latency separately; audio is also grouped by deterministic body-size class.
- Listener PID and Windows process working set/private bytes before and after.
- Optional fresh-process TCP-ready and HTTP-ready timing on an unused port.

The report keeps a separate control for the URL exactly returned by the original (`localhost`) versus the normalized numeric loopback host. On this machine, Anki Python consistently pays about two seconds for the former due to IPv6 connect/fallback. That control is never mixed into fair core endpoint comparisons, and browser/curl behavior may differ.

## Run

With all three servers already running:

```powershell
& .\run.ps1 -Profile standard
```

Fast harness validation against only the original:

```powershell
& .\run.ps1 -Profile smoke -ExtraArgs @(
  '--endpoint', 'original=http://127.0.0.1:5050',
  '--timeout', '5',
  '--max-audio-candidates', '128'
)
```

`standard` is the normal final run. `full` performs more repetitions and concurrency levels. Timestamped JSON/Markdown/CSV artifacts and `latest.*` copies are written to `results/`.

Fresh-process startup measurement is opt-in because the harness only terminates processes it launched itself and refuses to act if the target port is occupied. Copy `startup-spec.example.json`, point it at a server command using an unused port, then run:

```powershell
& .\run.ps1 -Profile standard -StartupSpec .\startup-spec.json
```

## Duplication audit

`analyze_duplication.py` distinguishes three concepts that must not be conflated:

1. Mapping aliases: multiple DB candidates reference one `(source,path)`.
2. Repeated path text across source roots: not proof of equal audio.
3. Distinct paths with byte-identical SHA-256 payloads: safe pack-level dedup opportunity.

Run it after other disk-heavy work is finished:

```powershell
& (Join-Path $env:LOCALAPPDATA 'AnkiProgramFiles\.venv\Scripts\python.exe') .\analyze_duplication.py --workers 16
```

The SHA-256 manifest checkpoints every 1,000 newly scanned files and is reused when size and modification time are unchanged. The report estimates 4 KiB NTFS allocation-unit slack but labels that as a model; it does not infer physical size from drive free-space changes.

## Fairness caveats

- Corpus construction reads SQLite first, so steady-state lookup results are intentionally warm.
- Windows standby cache is not flushed. Startup and first-touch results can retain OS cache state.
- The original add-on shares Anki's Python PID, so its whole-process working set cannot isolate the server precisely. Port 5051 uses the optimized add-on's standalone runner for an isolated benchmark (it shares Anki when installed). Rust is isolated.
- Every JSON trial records a boolean per response showing whether that sample opened a new socket or reused an existing one, plus server `will_close` evidence.
- Loopback excludes Chromium scheduling, decoding, and audio-device latency.
- Keep-alive has no reuse benefit when the server advertises/closes HTTP/1.0 responses; the JSON includes `will_close_responses` to make that visible.
