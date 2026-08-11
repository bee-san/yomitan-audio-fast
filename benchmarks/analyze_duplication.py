#!/usr/bin/env python3
"""Analyze mapping aliases, repeated paths, and byte-identical audio safely.

This scans only paths referenced by entries.db.  It never edits the dataset.
SHA-256 results are checkpointed to a CSV manifest so an interrupted scan can
resume without re-reading unchanged files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from benchmark import CorpusBuilder, DEFAULT_DB, fmt_bytes, rounded, utc_now


@dataclass(frozen=True)
class ReferencedFile:
    source: str
    relative_path: str
    absolute_path: str
    db_references: int


@dataclass(frozen=True)
class FileHash:
    source: str
    relative_path: str
    absolute_path: str
    db_references: int
    size: int
    mtime_ns: int
    sha256: str
    error: str = ""


def scan_mapping_rows(
    corpus: CorpusBuilder,
) -> tuple[list[ReferencedFile], list[dict[str, Any]], dict[str, Any]]:
    """One linear DB scan avoids several large SQLite temp GROUP BY sorts."""

    source_roots = {item.source_id: item.media_dir for item in corpus.sources}
    path_counts: Counter[tuple[str, str]] = Counter()
    seen_mappings: set[tuple[Any, ...]] = set()
    duplicate_mapping_keys: set[tuple[Any, ...]] = set()
    exact_extra = 0
    first_relative_source: dict[str, str] = {}
    cross_source_relative: dict[str, set[str]] = {}
    total_rows = 0

    for row in corpus.connection.execute(
        "SELECT expression, reading, source, speaker, display, file FROM entries"
    ):
        total_rows += 1
        if total_rows % 100_000 == 0:
            print(f"scanned {total_rows:,} mapping rows", flush=True)
        mapping = tuple(row)
        expression, reading, source, speaker, display, relative = mapping
        path_counts[(source, relative)] += 1
        if mapping in seen_mappings:
            exact_extra += 1
            duplicate_mapping_keys.add(mapping)
        else:
            seen_mappings.add(mapping)
        first_source = first_relative_source.get(relative)
        if first_source is None:
            first_relative_source[relative] = source
        elif first_source != source:
            sources = cross_source_relative.setdefault(relative, {first_source})
            sources.add(source)

    files: list[ReferencedFile] = []
    invalid: list[dict[str, Any]] = []
    for (source, relative), references in sorted(path_counts.items()):
        root = source_roots.get(source)
        if root is None:
            invalid.append(
                {"source": source, "relative_path": relative, "reason": "unknown source"}
            )
            continue
        absolute = (root / relative).resolve()
        try:
            absolute.relative_to(root)
        except ValueError:
            invalid.append(
                {"source": source, "relative_path": relative, "reason": "path escapes source root"}
            )
            continue
        files.append(ReferencedFile(source, relative, str(absolute), references))
    path_alias_groups = sum(count > 1 for count in path_counts.values())
    path_alias_extra = sum(count - 1 for count in path_counts.values())
    aliases = {
        "mapping_rows": total_rows,
        "unique_source_paths": len(path_counts),
        "path_alias_extra_mapping_rows": path_alias_extra,
        "paths_with_multiple_mapping_rows": path_alias_groups,
        "extra_rows_within_path_groups": path_alias_extra,
        "exact_duplicate_mapping_groups": len(duplicate_mapping_keys),
        "exact_duplicate_mapping_extra_rows": exact_extra,
        "relative_path_strings_used_by_multiple_sources": len(cross_source_relative),
        "source_paths_in_cross_source_relative_path_groups": sum(
            len(sources) for sources in cross_source_relative.values()
        ),
        "definitions": {
            "mapping_alias": "Multiple DB mapping rows refer to the same (source, relative path); these preserve term/reading/name behavior but do not imply duplicated audio bytes on disk.",
            "exact_duplicate_mapping": "All mapping columns are identical; extra rows may create truly repeated candidates.",
            "repeated_relative_path": "The same relative path text appears under different source roots; byte equality is not assumed.",
        },
    }
    return files, invalid, aliases


def read_manifest(path: Path) -> dict[tuple[str, str, int, int], FileHash]:
    cached: dict[tuple[str, str, int, int], FileHash] = {}
    if not path.is_file():
        return cached
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                item = FileHash(
                    source=row["source"],
                    relative_path=row["relative_path"],
                    absolute_path=row["absolute_path"],
                    db_references=int(row["db_references"]),
                    size=int(row["size"]),
                    mtime_ns=int(row["mtime_ns"]),
                    sha256=row["sha256"],
                    error=row.get("error", ""),
                )
                cached[(item.source, item.relative_path, item.size, item.mtime_ns)] = item
            except (KeyError, TypeError, ValueError):
                continue
    return cached


def hash_one(item: ReferencedFile) -> FileHash:
    path = Path(item.absolute_path)
    try:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return FileHash(
            source=item.source,
            relative_path=item.relative_path,
            absolute_path=item.absolute_path,
            db_references=item.db_references,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=digest.hexdigest(),
        )
    except Exception as exc:
        return FileHash(
            source=item.source,
            relative_path=item.relative_path,
            absolute_path=item.absolute_path,
            db_references=item.db_references,
            size=0,
            mtime_ns=0,
            sha256="",
            error=f"{type(exc).__name__}: {exc}",
        )


def current_cache_key(item: ReferencedFile) -> tuple[str, str, int, int] | None:
    try:
        stat = Path(item.absolute_path).stat()
        return item.source, item.relative_path, stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def write_manifest_header(handle: Any) -> csv.DictWriter:
    fields = [
        "source",
        "relative_path",
        "absolute_path",
        "db_references",
        "size",
        "mtime_ns",
        "sha256",
        "error",
    ]
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    return writer


def hash_files(
    files: Sequence[ReferencedFile],
    manifest: Path,
    workers: int,
) -> tuple[list[FileHash], dict[str, Any]]:
    temporary = manifest.with_suffix(manifest.suffix + ".tmp")
    cached = read_manifest(manifest)
    # A flushed .tmp is a valid interruption checkpoint even though it was not
    # atomically promoted to the final manifest yet.
    cached.update(read_manifest(temporary))
    reused: list[FileHash] = []
    pending: list[ReferencedFile]
    if cached:
        pending = []
        for item in files:
            key = current_cache_key(item)
            match = cached.get(key) if key else None
            if match and match.sha256 and not match.error:
                reused.append(
                    FileHash(
                        source=item.source,
                        relative_path=item.relative_path,
                        absolute_path=item.absolute_path,
                        db_references=item.db_references,
                        size=match.size,
                        mtime_ns=match.mtime_ns,
                        sha256=match.sha256,
                    )
                )
            else:
                pending.append(item)
    else:
        # A first run can go directly to hash_one, which performs the one stat
        # needed for the manifest. Avoid a redundant metadata walk over every
        # small file when there is nothing cached to validate.
        pending = list(files)

    temporary.parent.mkdir(parents=True, exist_ok=True)
    all_hashes = list(reused)
    started = time.perf_counter()
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = write_manifest_header(handle)
        for item in reused:
            writer.writerow(asdict(item))
        handle.flush()
        if pending:
            worker_count = max(1, workers)
            # Python 3.13's Executor.map eagerly submits its entire iterable.
            # Batch it so 373k tiny-file tasks do not become 373k live Future
            # objects while still keeping enough work queued for NVMe latency.
            batch_size = max(256, worker_count * 16)
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                for batch_start in range(0, len(pending), batch_size):
                    batch = pending[batch_start : batch_start + batch_size]
                    for result in executor.map(hash_one, batch):
                        completed += 1
                        all_hashes.append(result)
                        writer.writerow(asdict(result))
                        if completed % 1000 == 0:
                            handle.flush()
                            elapsed = max(time.perf_counter() - started, 0.001)
                            print(
                                f"hashed {completed:,}/{len(pending):,} new files "
                                f"({completed / elapsed:,.0f} files/s)",
                                flush=True,
                            )
    os.replace(temporary, manifest)
    return all_hashes, {
        "manifest": str(manifest),
        "cached_files_reused": len(reused),
        "files_hashed_this_run": len(pending),
        "workers": max(1, workers),
        "submission_batch_size": max(256, max(1, workers) * 16),
        "elapsed_seconds": time.perf_counter() - started,
    }


def analyze_hash_groups(items: Sequence[FileHash], cluster_size: int) -> dict[str, Any]:
    valid = [item for item in items if item.sha256 and not item.error]
    errors = [asdict(item) for item in items if item.error or not item.sha256]
    groups: dict[tuple[int, str], list[FileHash]] = defaultdict(list)
    per_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"files": 0, "logical_bytes": 0, "estimated_allocated_bytes": 0}
    )
    for item in valid:
        groups[(item.size, item.sha256)].append(item)
        per_source[item.source]["files"] += 1
        per_source[item.source]["logical_bytes"] += item.size
        per_source[item.source]["estimated_allocated_bytes"] += math.ceil(item.size / cluster_size) * cluster_size

    duplicate_groups = [group for group in groups.values() if len(group) > 1]
    logical_bytes = sum(item.size for item in valid)
    allocated_estimate = sum(math.ceil(item.size / cluster_size) * cluster_size for item in valid)
    unique_content_bytes = sum(size for size, _ in groups)
    packed_allocated_estimate = math.ceil(unique_content_bytes / cluster_size) * cluster_size
    byte_savings = logical_bytes - unique_content_bytes
    allocation_slack = allocated_estimate - logical_bytes

    cross_source_groups = [
        group for group in duplicate_groups if len({item.source for item in group}) > 1
    ]
    within_source_groups = [
        group
        for group in duplicate_groups
        if any(count > 1 for count in Counter(item.source for item in group).values())
    ]

    top_groups = []
    for group in sorted(
        duplicate_groups,
        key=lambda group: ((len(group) - 1) * group[0].size, len(group)),
        reverse=True,
    )[:100]:
        top_groups.append(
            {
                "sha256": group[0].sha256,
                "bytes_each": group[0].size,
                "copies": len(group),
                "dedup_savings_bytes": (len(group) - 1) * group[0].size,
                "sources": sorted({item.source for item in group}),
                "paths": [
                    {"source": item.source, "relative_path": item.relative_path}
                    for item in group[:20]
                ],
                "paths_truncated": len(group) > 20,
            }
        )

    return {
        "referenced_existing_files": len(valid),
        "hash_errors": errors,
        "logical_audio_bytes": logical_bytes,
        "estimated_ntfs_allocated_bytes": allocated_estimate,
        "estimated_per_file_allocation_slack_bytes": allocation_slack,
        "unique_sha256_payloads": len(groups),
        "unique_payload_bytes": unique_content_bytes,
        "byte_identical_duplicate_groups": len(duplicate_groups),
        "extra_files_in_duplicate_groups": sum(len(group) - 1 for group in duplicate_groups),
        "byte_dedup_logical_savings_bytes": byte_savings,
        "byte_dedup_logical_savings_percent": 100.0 * byte_savings / logical_bytes if logical_bytes else 0.0,
        "groups_spanning_sources": len(cross_source_groups),
        "groups_with_duplicates_inside_a_source": len(within_source_groups),
        "single_pack_allocated_bytes_estimate": packed_allocated_estimate,
        "combined_byte_dedup_plus_small_file_allocation_savings_bytes": allocated_estimate
        - packed_allocated_estimate,
        "combined_savings_percent_of_estimated_current_allocation": 100.0
        * (allocated_estimate - packed_allocated_estimate)
        / allocated_estimate
        if allocated_estimate
        else 0.0,
        "cluster_size_assumption_bytes": cluster_size,
        "per_source": dict(per_source),
        "top_duplicate_groups": top_groups,
        "definitions": {
            "byte_identical_cross_path": "Distinct (source, path) files with identical byte length and SHA-256; unlike mapping aliases, these consume repeated file payload on disk.",
            "physical_estimate": "ceil(file_size / cluster_size) for each ordinary file versus one concatenated unique-payload pack; excludes directory/MFT/index/pack-index overhead and sparse/compressed-file effects.",
        },
    }


def markdown_report(result: dict[str, Any]) -> str:
    aliases = result["database_aliases"]
    hashes = result["audio_hashes"]
    lines = [
        "# Audio duplication and pack-savings analysis",
        "",
        f"Captured: `{result['captured_at']}`  ",
        f"Database: `{result['database']}`  ",
        f"Hash: SHA-256 over {hashes['referenced_existing_files']:,} referenced physical files",
        "",
        "## Three distinct kinds of duplication",
        "",
        "| Kind | Count | Meaning | Audio storage savings |",
        "|---|---:|---|---:|",
        f"| Mapping aliases | {aliases['path_alias_extra_mapping_rows']:,} extra mapping rows | More than one term/reading/name mapping points at one `(source,path)` | 0 B; retain mappings for candidate parity |",
        f"| Exact duplicate mappings | {aliases['exact_duplicate_mapping_extra_rows']:,} extra identical rows | Every mapping field repeats; may intentionally repeat candidates | 0 B unless behavior is changed (not recommended) |",
        f"| Repeated relative paths across source roots | {aliases['relative_path_strings_used_by_multiple_sources']:,} path strings | Textual path collision only; files can differ | Only when SHA-256 also matches |",
        f"| Byte-identical distinct paths | {hashes['extra_files_in_duplicate_groups']:,} extra files in {hashes['byte_identical_duplicate_groups']:,} hash groups | Distinct source/path files have identical complete bytes | {fmt_bytes(hashes['byte_dedup_logical_savings_bytes'])} logical |",
        "",
        "Mapping aliases and even exact duplicate rows must stay in the lookup postings if preserving candidate count/order/voice names exactly. Packs can still store each distinct payload once and let every candidate point to the same offset/length.",
        "",
        "## Storage model",
        "",
        "| Measure | Size |",
        "|---|---:|",
        f"| Current referenced audio, logical | {fmt_bytes(hashes['logical_audio_bytes'])} ({hashes['logical_audio_bytes']:,} B exact) |",
        f"| Estimated current NTFS allocation ({hashes['cluster_size_assumption_bytes']:,}-byte clusters) | {fmt_bytes(hashes['estimated_ntfs_allocated_bytes'])} |",
        f"| Unique payload bytes after SHA-256 dedup | {fmt_bytes(hashes['unique_payload_bytes'])} ({hashes['unique_payload_bytes']:,} B exact) |",
        f"| One packed payload allocation estimate | {fmt_bytes(hashes['single_pack_allocated_bytes_estimate'])} |",
        f"| Logical byte-dedup saving | {fmt_bytes(hashes['byte_dedup_logical_savings_bytes'])} ({hashes['byte_dedup_logical_savings_percent']:.2f}%) |",
        f"| Combined dedup + per-file cluster-slack saving estimate | {fmt_bytes(hashes['combined_byte_dedup_plus_small_file_allocation_savings_bytes'])} ({hashes['combined_savings_percent_of_estimated_current_allocation']:.2f}%) |",
        "",
        "The exact logical-audio byte count covers only physical assets referenced by `entries.db`. It excludes the database, configuration, indexes, compiled artifacts, and loose files that no DB row references, so it is not the full copied-project size.",
        "",
        "The allocation calculation is a model, not a filesystem accounting claim. It excludes NTFS MFT/directory metadata, pack index size, and sparse/compressed behavior. A contemporaneous drive free-space delta was confounded by a Rust toolchain installation and other artifact creation, so it is deliberately not used as evidence here.",
        "The lab intentionally keeps the loose copied corpus for drop-in compatibility, so the counterfactual loose-files-to-pack saving is not a claim that the current project folder became smaller. The add-on and Rust servers do hardlink the same pack, avoiding a second physical pack payload allocation on this volume.",
        "",
        "## Per source",
        "",
        "| Source | Files | Logical | Estimated allocation |",
        "|---|---:|---:|---:|",
    ]
    for source, stats in sorted(hashes["per_source"].items()):
        lines.append(
            f"| {source} | {stats['files']:,} | {fmt_bytes(stats['logical_bytes'])} | {fmt_bytes(stats['estimated_allocated_bytes'])} |"
        )
    cross_check = result.get("compiler_cross_check")
    if cross_check:
        lines.extend(
            [
                "",
                "## Independent audit vs native compiler manifest",
                "",
                "| Counter | Independent SHA-256 audit | Compiler | Match |",
                "|---|---:|---:|---:|",
            ]
        )
        for item in cross_check.get("counters", []):
            lines.append(
                f"| {item['name']} | {item['independent']:,} | {item['compiler']:,} | {'yes' if item['match'] else 'NO'} |"
            )
    lines.extend(
        [
            "",
            f"Hash/read errors: {len(hashes['hash_errors'])}. Full error records and the top 100 duplicate groups are in the JSON report.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parent / "results" / "audio-sha256-manifest.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "results" / "duplication.json",
    )
    parser.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 4))
    parser.add_argument("--cluster-size", type=int, default=4096)
    parser.add_argument(
        "--compiler-report",
        type=Path,
        default=Path(__file__).resolve().parent / "architecture-variants.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    with CorpusBuilder(args.db) as corpus:
        print("Reading unique (source,path) references...", flush=True)
        files, invalid, aliases = scan_mapping_rows(corpus)
        print(f"Hashing/checking {len(files):,} referenced files...", flush=True)
        hashes, scan = hash_files(files, args.manifest, args.workers)
    analysis = analyze_hash_groups(hashes, args.cluster_size)
    compiler_cross_check = None
    if args.compiler_report.is_file():
        compiler_document = json.loads(args.compiler_report.read_text(encoding="utf-8"))
        compiler = compiler_document.get("native_bundle_compile", {})
        pairs = [
            ("mapping rows", aliases["mapping_rows"], compiler.get("mapping_rows")),
            (
                "unique source paths",
                aliases["unique_source_paths"],
                compiler.get("unique_source_paths"),
            ),
            (
                "additional path-alias references",
                aliases["path_alias_extra_mapping_rows"],
                compiler.get("additional_repeated_path_alias_references"),
            ),
            (
                "unique exact-byte payloads",
                analysis["unique_sha256_payloads"],
                compiler.get("exact_byte_unique_payloads"),
            ),
            (
                "content-duplicate extra assets",
                analysis["extra_files_in_duplicate_groups"],
                compiler.get("content_duplicate_assets"),
            ),
        ]
        counters = [
            {
                "name": name,
                "independent": independent,
                "compiler": compiled,
                "match": independent == compiled,
            }
            for name, independent, compiled in pairs
            if compiled is not None
        ]
        compiler_cross_check = {
            "compiler_report": str(args.compiler_report),
            "counters": counters,
            "all_match": all(item["match"] for item in counters),
        }
    result = rounded(
        {
            "schema_version": 1,
            "captured_at": utc_now(),
            "database": str(args.db.resolve()),
            "database_aliases": aliases,
            "referenced_paths": len(files),
            "invalid_references": invalid,
            "hash_scan": scan,
            "audio_hashes": analysis,
            "compiler_cross_check": compiler_cross_check,
            "total_elapsed_seconds": time.perf_counter() - started,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = args.output.with_suffix(".md")
    markdown.write_text(markdown_report(result), encoding="utf-8")
    print(f"JSON: {args.output}")
    print(f"Markdown: {markdown}")
    cross_check_failed = bool(
        compiler_cross_check and not compiler_cross_check.get("all_match")
    )
    return 1 if invalid or analysis["hash_errors"] or cross_check_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
