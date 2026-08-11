from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import time


CREATE_TABLE = """
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    expression TEXT NOT NULL,
    reading TEXT,
    source TEXT NOT NULL,
    speaker TEXT,
    display TEXT,
    file TEXT NOT NULL
)
"""
INSERT = (
    "INSERT INTO entries(expression,reading,source,speaker,display,file) "
    "VALUES (?,?,?,?,?,?)"
)


def rows(count: int):
    for index in range(count):
        yield (
            f"term-{index % 70000}",
            f"reading-{index % 50000}",
            ("nhk16", "shinmeikai8", "forvo", "jpod")[index % 4],
            f"speaker-{index % 32}" if index % 4 == 2 else None,
            f"display-{index % 128}" if index % 2 == 0 else None,
            f"audio/{index % 150000}.opus",
        )


def run(count: int, batched: bool, deferred_index: bool, batch_size: int) -> float:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute(CREATE_TABLE)
    if not deferred_index:
        connection.execute("CREATE INDEX idx_expr_reading ON entries(expression,reading)")
    started = time.perf_counter()
    if batched:
        batch = []
        for row in rows(count):
            batch.append(row)
            if len(batch) >= batch_size:
                connection.executemany(INSERT, batch)
                batch.clear()
        if batch:
            connection.executemany(INSERT, batch)
    else:
        cursor = connection.cursor()
        for row in rows(count):
            cursor.execute(INSERT, row)
        cursor.close()
    if deferred_index:
        connection.execute("CREATE INDEX idx_expr_reading ON entries(expression,reading)")
    connection.commit()
    elapsed = time.perf_counter() - started
    actual = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    connection.close()
    if actual != count:
        raise AssertionError(f"expected {count} rows, found {actual}")
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8192)
    args = parser.parse_args()
    variants = {
        "per_row_index_live": (False, False),
        "per_row_index_deferred": (False, True),
        "batch_8192_index_live": (True, False),
        "batch_8192_index_deferred": (True, True),
    }
    all_timings = {name: [] for name in variants}
    generator = random.Random(5051)
    for _ in range(args.runs):
        names = list(variants)
        generator.shuffle(names)
        for name in names:
            batched, deferred = variants[name]
            all_timings[name].append(
                run(args.rows, batched, deferred, args.batch_size)
            )
    results = {}
    for name, timings in all_timings.items():
        results[name] = {
            "median_seconds": statistics.median(timings),
            "runs_seconds": timings,
            "rows_per_second": args.rows / statistics.median(timings),
        }
    baseline = results["per_row_index_live"]["median_seconds"]
    for result in results.values():
        result["speedup_vs_original_shape"] = baseline / result["median_seconds"]
    print(
        json.dumps(
            {
                "rows": args.rows,
                "runs": args.runs,
                "batch_size": args.batch_size,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
