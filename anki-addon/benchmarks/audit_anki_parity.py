from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import itertools
import json
import os
import sqlite3
import sys
import time

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional


PACKAGE = "_local_audio_fast_parity_audit"


def load_addon(root: Path):
    os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
    specification = importlib.util.spec_from_file_location(
        PACKAGE,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load add-on package from {root}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[PACKAGE] = module
    specification.loader.exec_module(module)
    return module


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA mmap_size=268435456")
    connection.execute("PRAGMA cache_size=-8192")
    return connection


class DifferentialAudit:
    def __init__(self, root: Path, db_path: Path, progress_every: int) -> None:
        load_addon(root)
        config = importlib.import_module(f"{PACKAGE}.config")
        store_module = importlib.import_module(f"{PACKAGE}.fast_store")
        self.LookupRequest = store_module.LookupRequest
        self.sources = config.ALL_SOURCES
        self.source_order = tuple(self.sources)
        self.templates = {
            source_id: source.data.display for source_id, source in self.sources.items()
        }
        self.db_path = db_path.resolve(strict=True)
        self.connection = readonly_connection(self.db_path)
        self.key_connection = readonly_connection(self.db_path)
        self.store = store_module.LookupStore(
            self.db_path,
            self.sources,
            "http://127.0.0.1:5051",
            root / "user_files" / "fast_audio",
            response_cache_size=0,
            row_cache_size=0,
            lookup_mode="sqlite",
        )
        self.progress_every = max(1, progress_every)
        self.total_cases = 0

    def close(self) -> None:
        self.store.close()
        self.connection.close()
        self.key_connection.close()

    def reference_rows(
        self,
        expression: str,
        reading: Optional[str],
        sources: tuple[str, ...],
        users: tuple[str, ...],
    ) -> list[tuple]:
        """Independent transcription of add-on 1.7.0's dynamic legacy SQL."""

        if reading is None:
            parameters: list[object] = [expression]
            where = "expression = ?"
        else:
            parameters = [expression, reading]
            where = "expression = ? AND (reading IS NULL OR reading = ?)"
        if len(sources) != len(self.source_order):
            placeholders = ",".join("?" for _ in sources)
            where += f" AND source IN ({placeholders})"
            parameters.extend(sources)
        if users:
            placeholders = ",".join("?" for _ in users)
            where += f" AND (speaker IS NULL OR speaker IN ({placeholders}))"
            parameters.extend(users)
        source_case = "CASE source " + " ".join(
            f"WHEN ? THEN {index}" for index in range(len(sources))
        ) + " END"
        parameters.extend(sources)
        order = source_case
        if users:
            speaker_case = "CASE speaker " + " ".join(
                f"WHEN ? THEN {index}" for index in range(len(users))
            ) + " END"
            parameters.extend(users)
            order += ", " + speaker_case
        sql = (
            "SELECT id,expression,reading,source,speaker,display,file "
            f"FROM entries WHERE ({where}) ORDER BY {order}, reading"
        )
        return self.connection.execute(sql, parameters).fetchall()

    def reference_signature(self, rows: Iterable[tuple]) -> list[tuple]:
        result = []
        for row_id, _expression, reading, source, speaker, display, filename in rows:
            template = self.templates.get(source)
            if template is None:
                continue
            name = template % display if display is not None else template
            result.append((row_id, reading, source, speaker, display, name, filename))
        return result

    def optimized_signature(
        self,
        expression: str,
        reading: Optional[str],
        sources: tuple[str, ...],
        users: tuple[str, ...],
    ) -> list[tuple]:
        request = self.LookupRequest(expression, reading, sources, users)
        result = []
        for row_id, row_reading, source, speaker, display, filename in self.store.selected_rows(
            request
        ):
            name = self.store._display_name(source, display)
            if name is not None:
                result.append(
                    (row_id, row_reading, source, speaker, display, name, filename)
                )
        return result

    @staticmethod
    def _digest_update(digest: "hashlib._Hash", key: tuple, rows: list[tuple]) -> None:
        payload = json.dumps(
            (key, rows), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)

    def audit_cases(self, name: str, cases: Iterable[tuple]) -> dict:
        started = time.perf_counter()
        reference_digest = hashlib.sha256()
        optimized_digest = hashlib.sha256()
        mismatches = []
        mismatch_count = 0
        case_count = 0
        reference_rows = 0
        optimized_rows = 0
        for expression, reading, sources, users in cases:
            case_count += 1
            self.total_cases += 1
            key = (expression, reading, sources, users)
            try:
                expected = self.reference_signature(
                    self.reference_rows(expression, reading, sources, users)
                )
                reference_error = None
            except Exception as error:
                expected = []
                reference_error = f"{type(error).__name__}: {error}"
            try:
                actual = self.optimized_signature(expression, reading, sources, users)
                optimized_error = None
            except Exception as error:
                actual = []
                optimized_error = f"{type(error).__name__}: {error}"
            reference_rows += len(expected)
            optimized_rows += len(actual)
            self._digest_update(reference_digest, key, expected)
            self._digest_update(optimized_digest, key, actual)
            if expected != actual or reference_error != optimized_error:
                mismatch_count += 1
                if len(mismatches) < 25:
                    mismatches.append(
                        {
                            "expression": expression,
                            "reading": reading,
                            "sources": sources,
                            "users": users,
                            "reference_error": reference_error,
                            "optimized_error": optimized_error,
                            "reference_count": len(expected),
                            "optimized_count": len(actual),
                            "reference_first_25": expected[:25],
                            "optimized_first_25": actual[:25],
                        }
                    )
            if case_count % self.progress_every == 0:
                print(
                    f"[{name}] {case_count:,} cases; mismatches seen: "
                    f"{mismatch_count:,}",
                    file=sys.stderr,
                    flush=True,
                )
        return {
            "cases": case_count,
            "reference_rows": reference_rows,
            "optimized_rows": optimized_rows,
            "reference_sha256": reference_digest.hexdigest(),
            "optimized_sha256": optimized_digest.hexdigest(),
            "mismatch_count": mismatch_count,
            "mismatch_samples": mismatches,
            "passed": mismatch_count == 0
            and reference_digest.digest() == optimized_digest.digest(),
            "elapsed_seconds": time.perf_counter() - started,
        }

    def term_cases(self):
        cursor = self.key_connection.execute(
            "SELECT DISTINCT expression FROM entries ORDER BY expression"
        )
        for (expression,) in cursor:
            yield expression, None, self.source_order, ()

    def exact_cases(self):
        cursor = self.key_connection.execute(
            "SELECT DISTINCT expression,reading FROM entries "
            "WHERE reading IS NOT NULL ORDER BY expression,reading"
        )
        for expression, reading in cursor:
            yield expression, reading, self.source_order, ()

    def representative_terms(self) -> list[str]:
        terms: set[str] = set()
        terms.update(
            expression
            for (expression,) in self.connection.execute(
                "SELECT expression FROM entries GROUP BY expression "
                "ORDER BY COUNT(*) DESC,expression LIMIT 64"
            )
        )
        for source in self.source_order:
            terms.update(
                expression
                for (expression,) in self.connection.execute(
                    "SELECT expression FROM entries WHERE source=? "
                    "GROUP BY expression ORDER BY COUNT(*) DESC,expression LIMIT 8",
                    (source,),
                )
            )
        return sorted(terms)

    def source_matrix_cases(self):
        terms = self.representative_terms()
        ordered_subsets = [
            values
            for length in range(1, len(self.source_order) + 1)
            for values in itertools.permutations(self.source_order, length)
        ]
        readings = {
            expression: (
                self.connection.execute(
                    "SELECT reading FROM entries WHERE expression=? "
                    "AND reading IS NOT NULL ORDER BY reading LIMIT 1",
                    (expression,),
                ).fetchone()
            )
            for expression in terms
        }
        for expression in terms:
            reading_row = readings[expression]
            for sources in ordered_subsets:
                yield expression, None, sources, ()
                if reading_row is not None:
                    yield expression, reading_row[0], sources, ()

    def speaker_matrix_cases(self):
        speakers_by_expression: dict[str, list[str]] = defaultdict(list)
        cursor = self.connection.execute(
            "SELECT DISTINCT expression,speaker FROM entries "
            "WHERE speaker IS NOT NULL ORDER BY expression,speaker"
        )
        for expression, speaker in cursor:
            speakers_by_expression[expression].append(speaker)
        missing = "__local_audio_fast_missing_speaker__"
        for expression, speakers in speakers_by_expression.items():
            seen: set[tuple[str, ...]] = set()
            user_orders: list[tuple[str, ...]] = []

            def add(users: tuple[str, ...]) -> None:
                if users not in seen:
                    seen.add(users)
                    user_orders.append(users)

            for speaker in speakers:
                add((speaker,))
            for left, right in itertools.permutations(speakers, 2):
                add((left, right))
            add(tuple(speakers))
            add(tuple(reversed(speakers)))
            add((missing,))
            for users in user_orders:
                yield expression, None, self.source_order, users


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exhaustive real-DB parity audit for the optimized Anki add-on."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--progress-every", type=int, default=25000)
    parser.add_argument(
        "--case-limit",
        type=int,
        help="Smoke-test limit applied independently to each audit section.",
    )
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    db_path = (args.db or root / "user_files" / "entries.db").resolve(strict=True)
    started = time.perf_counter()
    audit = DifferentialAudit(root, db_path, args.progress_every)
    try:
        def limited(cases: Iterable[tuple]) -> Iterable[tuple]:
            if args.case_limit is None:
                return cases
            return itertools.islice(cases, max(0, args.case_limit))

        quick_check = audit.connection.execute("PRAGMA quick_check").fetchone()[0]
        database = {
            "path": str(db_path),
            "bytes": db_path.stat().st_size,
            "mtime_ns": db_path.stat().st_mtime_ns,
            "quick_check": quick_check,
            "rows": audit.connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
            "expressions": audit.connection.execute(
                "SELECT COUNT(DISTINCT expression) FROM entries"
            ).fetchone()[0],
            "exact_non_null_keys": audit.connection.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT expression,reading "
                "FROM entries WHERE reading IS NOT NULL)"
            ).fetchone()[0],
            "source_order": audit.source_order,
            "source_counts": dict(
                audit.connection.execute(
                    "SELECT source,COUNT(*) FROM entries GROUP BY source ORDER BY source"
                )
            ),
            "distinct_speakers": audit.connection.execute(
                "SELECT COUNT(DISTINCT speaker) FROM entries WHERE speaker IS NOT NULL"
            ).fetchone()[0],
            "speaker_rows": audit.connection.execute(
                "SELECT COUNT(*) FROM entries WHERE speaker IS NOT NULL"
            ).fetchone()[0],
        }
        sections = {
            "all_term_only_expressions": audit.audit_cases(
                "term-only", limited(audit.term_cases())
            ),
            "all_exact_non_null_keys": audit.audit_cases(
                "exact", limited(audit.exact_cases())
            ),
            "all_ordered_source_subsets_on_representative_terms": audit.audit_cases(
                "source-matrix", limited(audit.source_matrix_cases())
            ),
            "comprehensive_forvo_speaker_matrix": audit.audit_cases(
                "speaker-matrix", limited(audit.speaker_matrix_cases())
            ),
        }
        store_info = audit.store.info()
        passed = quick_check == "ok" and all(section["passed"] for section in sections.values())
        result = {
            "schema_version": 1,
            "audit": "anki-real-db-legacy-differential",
            "passed": passed,
            "database": database,
            "sections": sections,
            "total_cases": audit.total_cases,
            "store_after_audit": store_info,
            "elapsed_seconds": time.perf_counter() - started,
        }
    finally:
        audit.close()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
