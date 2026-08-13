# Local data map (payloads omitted from Git)

The Git repository excludes the user's audio and every generated binary artifact. This document describes their shape so the complete setup can be recreated locally.

## Existing add-on collection

```text
1045800357/                         # installed add-on root (example ID)
  *.py, source/, meta.json          # code supplied by this repository
  user_files/                       # retained when code is overlaid
    config.json
    entries.db
    nhk16_files/                    # source folders; exact roots come from config
    shinmeikai8_files/
    daijisen_files/                 # Yomitan Ultimate Audio (AJT layout)
    taas_files/                     # Yomitan Ultimate Audio (AJT layout)
    forvo_files/
    forvo_ext_files/                # Yomitan Ultimate Audio (flat: audio files only)
    forvo_ext2_files/
    jpod_files/
    jpod_alternate_files/
    ozk5_files/
    fast_audio/                     # generated locally; never committed
```

The folder-picker accepts the add-on root, `user_files`, a collection root containing `entries.db` and source folders, or one recognized source folder. It validates paths and the database before publication. Files are copied only when metadata must move into the current add-on; audio originals are not moved or deleted.

## `entries.db`

Required `entries` columns:

```text
id INTEGER
expression TEXT
reading TEXT NULL
source TEXT
speaker TEXT NULL
display TEXT
file TEXT
```

The logical mapping is:

```text
(expression, reading) -> ordered rows
row -> (source, speaker, display name, relative audio path)
```

The measured collection contained 590,410 rows, 280,396 expressions, 242,976 non-null exact keys, five sources, and 373,911 distinct `(source,file)` assets. Candidate rows and voices remain distinct even when multiple mappings reference the same source path.

## Generated Anki fast pack

```text
user_files/fast_audio/
  active.json
  auto-build-v1.json                # one-time automatic-build fingerprint/status
  versions/<bundle-version>/
    manifest.json
    audio.idx                       # fixed row-ID -> offset/length/MIME records
    audio.pack                      # concatenated immutable audio bytes
```

In the measured collection, `audio.idx` was 9,446,640 bytes (64-byte header plus 590,411 16-byte records) and `audio.pack` was 1,713,724,873 bytes. The runtime memory-maps both lazily. Versioned `/v/<version>/audio/<row-id>` URLs remain immutable; `/v1/play` is deliberately `no-store`.

Build from Anki with **Tools -> Local Audio Server -> Build/rebuild fast desktop audio pack**, or use **Import/process existing audio folder...** to validate/copy metadata, remap source roots, build the pack, and activate it in one background operation.

## Generated Rust bundle

```text
bundle/
  manifest.json
  integrity.sha256.json
  versions/<bundle-version>/
    lookup.bin                      # sorted-hash + CHD lookup records and strings
    audio.pack
```

The measured Rust bundle held 280,396 terms, 590,410 mapping records, 373,911 logical assets, and the same 1,713,724,873 audio bytes. The compiler found zero byte-identical payloads at different paths in this dataset, so content deduplication saved 0 bytes; 216,499 additional mapping aliases were still retained.

Example local commands (from `rust-server/` after building a release binary):

```powershell
target\release\yomitan-audio-rs.exe compile --addon-root ..\anki-addon --output ..\bundle --pack-workers 8
target\release\yomitan-audio-rs.exe verify --bundle ..\bundle
target\release\yomitan-audio-rs.exe serve --bundle ..\bundle --host 127.0.0.1 --port 5052 --lookup-mode mph --asset-mode pack
```

Do not add the generated `bundle`, `user_files`, databases, packs, indexes, or executables to Git.
