#!/usr/bin/env python3
"""Reproducible loopback benchmark/parity suite for Yomitan audio servers.

The suite intentionally uses only the Python standard library so it can run with
Anki's bundled Python on a stock Windows installation.  It treats the installed
legacy server as the behavioural oracle, while also checking its responses
against the real entries.db and audio files where possible.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import ctypes
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import platform
import random
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_DB = Path(
    os.environ.get(
        "YOMITAN_AUDIO_DB",
        Path(__file__).resolve().parents[1] / "anki-addon" / "user_files" / "entries.db",
    )
)
DEFAULT_ENDPOINTS = {
    "original": "http://127.0.0.1:5050",
    "anki_optimized": "http://127.0.0.1:5051",
    "rust": "http://127.0.0.1:5052",
}
SOURCE_MIME = {
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}

PROFILE_DEFAULTS = {
    "smoke": {
        "repeats": 2,
        "lookup_iterations": 40,
        "e2e_iterations": 8,
        "play_iterations": 12,
        "concurrency_requests": 30,
        "workers": [1, 4],
        "warmup": 5,
        "parity_cases": 32,
    },
    "standard": {
        "repeats": 5,
        "lookup_iterations": 160,
        "e2e_iterations": 40,
        "play_iterations": 80,
        "concurrency_requests": 160,
        "workers": [1, 8, 32],
        "warmup": 20,
        "parity_cases": 256,
    },
    "full": {
        "repeats": 7,
        "lookup_iterations": 400,
        "e2e_iterations": 100,
        "play_iterations": 200,
        "concurrency_requests": 400,
        "workers": [1, 4, 8, 16, 32],
        "warmup": 40,
        "parity_cases": 1000,
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def distribution(values: Iterable[float]) -> dict[str, Any]:
    vals = [float(v) for v in values]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": min(vals),
        "mean": statistics.fmean(vals),
        "p50": nearest_rank(vals, 0.50),
        "p95": nearest_rank(vals, 0.95),
        "p99": nearest_rank(vals, 0.99),
        "max": max(vals),
        "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
    }


def rounded(value: Any, digits: int = 3) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: rounded(v, digits) for k, v in value.items()}
    if isinstance(value, list):
        return [rounded(v, digits) for v in value]
    return value


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str

    @property
    def parsed(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.base_url)

    @property
    def port(self) -> int:
        parsed = self.parsed
        return int(parsed.port or (443 if parsed.scheme == "https" else 80))


@dataclass
class HttpResult:
    requested_url: str
    method: str
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    headers_ms: float
    first_body_byte_ms: float | None
    http_version: str
    will_close: bool
    retry_count: int = 0
    connection_was_new: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class HttpSession:
    """Tiny measured HTTP client with explicit keep-alive/close semantics."""

    def __init__(self, keep_alive: bool, timeout: float = 10.0):
        self.keep_alive = keep_alive
        self.timeout = timeout
        self._connections: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
        self.connections_created = 0

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported URL scheme: {url}")
        if not parsed.hostname:
            raise ValueError(f"URL has no host: {url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname, port

    def _new_connection(self, origin: tuple[str, str, int]) -> http.client.HTTPConnection:
        scheme, host, port = origin
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        self.connections_created += 1
        return cls(host, port, timeout=self.timeout)

    def close(self) -> None:
        for conn in self._connections.values():
            with contextlib.suppress(Exception):
                conn.close()
        self._connections.clear()

    def __enter__(self) -> "HttpSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResult:
        parsed = urllib.parse.urlsplit(url)
        origin = self._origin(url)
        # http.client requires an ASCII request target. Browser URL APIs perform
        # this quoting automatically, but legacy Forvo URLs may contain raw kana.
        quoted_path = urllib.parse.quote(
            parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"
        )
        quoted_query = urllib.parse.quote(
            parsed.query, safe="=&%:@!$'()*+,;/?-._~"
        )
        path = urllib.parse.urlunsplit(("", "", quoted_path, quoted_query, ""))
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "*/*")
        request_headers.setdefault("User-Agent", "yomitan-audio-benchmark/1")
        request_headers["Connection"] = "keep-alive" if self.keep_alive else "close"

        retry_count = 0
        connection_was_new = False
        started = time.perf_counter_ns()
        while True:
            conn = self._connections.get(origin)
            if conn is None:
                conn = self._new_connection(origin)
                connection_was_new = True
                if self.keep_alive:
                    self._connections[origin] = conn
            try:
                conn.request(method, path, body=body, headers=request_headers)
                response = conn.getresponse()
                headers_ms = (time.perf_counter_ns() - started) / 1_000_000.0
                first_chunk = b"" if method.upper() == "HEAD" else response.read(1)
                first_body_byte_ms = (
                    (time.perf_counter_ns() - started) / 1_000_000.0
                    if first_chunk
                    else None
                )
                response_body = first_chunk + response.read()
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                version = {10: "HTTP/1.0", 11: "HTTP/1.1"}.get(
                    response.version, f"HTTP/{response.version}"
                )
                will_close = bool(response.will_close)
                result = HttpResult(
                    requested_url=url,
                    method=method,
                    status=response.status,
                    reason=response.reason,
                    headers=response_headers,
                    body=response_body,
                    elapsed_ms=elapsed_ms,
                    headers_ms=headers_ms,
                    first_body_byte_ms=first_body_byte_ms,
                    http_version=version,
                    will_close=will_close,
                    retry_count=retry_count,
                    connection_was_new=connection_was_new,
                )
                if not self.keep_alive or will_close:
                    conn.close()
                    self._connections.pop(origin, None)
                return result
            except (BrokenPipeError, ConnectionResetError, http.client.RemoteDisconnected):
                conn.close()
                self._connections.pop(origin, None)
                if retry_count >= 1:
                    raise
                # A server is permitted to close an idle persistent connection.
                retry_count += 1
            except Exception:
                conn.close()
                self._connections.pop(origin, None)
                raise


def query_url(endpoint: Endpoint, params: Mapping[str, str], path: str = "/") -> str:
    encoded = urllib.parse.urlencode(list(params.items()), quote_via=urllib.parse.quote)
    return endpoint.base_url.rstrip("/") + path + ("?" + encoded if encoded else "")


def loopback_equivalent(first: urllib.parse.SplitResult, second: urllib.parse.SplitResult) -> bool:
    first_port = first.port or (443 if first.scheme == "https" else 80)
    second_port = second.port or (443 if second.scheme == "https" else 80)
    return (
        first.scheme == second.scheme
        and first_port == second_port
        and (first.hostname or "").lower() in LOOPBACK_NAMES
        and (second.hostname or "").lower() in LOOPBACK_NAMES
    )


def resolve_audio_url(endpoint: Endpoint, returned_url: str) -> tuple[str, bool]:
    """Resolve URLs and keep all benchmark fetches on the endpoint's loopback origin.

    An implementation running on a benchmark port must not accidentally make the
    benchmark download bytes from the original server's hard-coded port 5050.
    The bool indicates that such an origin mismatch was observed.
    """

    absolute = urllib.parse.urljoin(endpoint.base_url.rstrip("/") + "/", returned_url)
    candidate = urllib.parse.urlsplit(absolute)
    expected = endpoint.parsed
    if loopback_equivalent(candidate, expected):
        # Canonicalize localhost to the endpoint's numeric loopback host.  On
        # this Windows machine localhost tries an unbound IPv6 address first,
        # adding ~2 seconds to every legacy audio request even though the server
        # itself responds in milliseconds.
        canonical = urllib.parse.urlunsplit(
            (expected.scheme, expected.netloc, candidate.path, candidate.query, candidate.fragment)
        )
        return canonical, False
    if (candidate.hostname or "").lower() in LOOPBACK_NAMES:
        rebased = urllib.parse.urlunsplit(
            (expected.scheme, expected.netloc, candidate.path, candidate.query, candidate.fragment)
        )
        return rebased, True
    # Never let a parity target redirect the audit into an arbitrary external
    # fetch. Rebase only for safe local diagnosis and retain the mismatch flag
    # so correctness necessarily fails.
    rebased = urllib.parse.urlunsplit(
        (expected.scheme, expected.netloc, candidate.path, candidate.query, candidate.fragment)
    )
    return rebased, True


def numeric_loopback_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname or "").lower() not in LOOPBACK_NAMES:
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"127.0.0.1:{port}", parsed.path, parsed.query, parsed.fragment)
    )


def parse_legacy_payload(body: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "top-level JSON is not an object"
    if payload.get("type") != "audioSourceList":
        return None, f"type is {payload.get('type')!r}, expected 'audioSourceList'"
    candidates = payload.get("audioSources")
    if not isinstance(candidates, list):
        return None, "audioSources is not an array"
    for index, item in enumerate(candidates):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(
            item.get("url"), str
        ):
            return None, f"audioSources[{index}] is not a name/url object"
    return payload, None


def dedicated_candidate_item_valid(item: Any) -> bool:
    """Require the stable rich candidate contract, allowing future extra fields."""

    if not isinstance(item, dict):
        return False
    audio_id = item.get("audioId")
    return bool(
        isinstance(audio_id, int)
        and not isinstance(audio_id, bool)
        and audio_id >= 0
        and isinstance(item.get("source"), str)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("mime"), str)
        and item.get("mime")
        and isinstance(item.get("url"), str)
        and item.get("url")
        and "speaker" in item
        and (item.get("speaker") is None or isinstance(item.get("speaker"), str))
        and "reading" in item
        and (item.get("reading") is None or isinstance(item.get("reading"), str))
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_and_file_from_legacy_url(url: str, source_ids: Sequence[str]) -> tuple[str | None, str | None]:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/")
    source, separator, relative = path.partition("/")
    if separator and source in source_ids:
        return source, relative
    return None, None


def powershell_json(script: str, timeout: float = 20.0) -> Any:
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)
    except Exception:
        return None


def listener_pid(port: int) -> int | None:
    value = powershell_json(
        "$x=Get-NetTCPConnection -State Listen -LocalPort "
        + str(int(port))
        + " -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; "
        + "if($null -ne $x){$x | ConvertTo-Json -Compress}"
    )
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def process_identity(pid: int | None) -> dict[str, Any] | None:
    if not pid:
        return None
    return powershell_json(
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
        + str(int(pid))
        + "\" -ErrorAction SilentlyContinue; "
        + "if($p){$p | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress}"
    )


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def windows_process_memory(pid: int | None) -> dict[str, int] | None:
    if not pid or os.name != "nt":
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        )
        if not ok:
            return None
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_bytes": int(counters.PrivateUsage),
            "page_fault_count": int(counters.PageFaultCount),
        }
    finally:
        kernel32.CloseHandle(handle)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def physical_memory() -> dict[str, int] | None:
    if os.name != "nt":
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes_at_start": int(status.ullAvailPhys),
    }


def machine_metadata() -> dict[str, Any]:
    windows_details = powershell_json(
        "$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed;"
        "$os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber;"
        "$disk=Get-Volume -DriveLetter C -ErrorAction SilentlyContinue | Select-Object FileSystem,DriveType,Size,SizeRemaining;"
        "$physical=Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object FriendlyName,MediaType,BusType,Size;"
        "[pscustomobject]@{cpu=$cpu;os=$os;volume=$disk;physicalDisks=$physical} | ConvertTo-Json -Compress -Depth 4"
    )
    try:
        rust_version = subprocess.run(
            ["rustc.exe", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    except Exception:
        rust_version = None
    return {
        "captured_at": utc_now(),
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "physical_memory": physical_memory(),
        "windows": windows_details,
        "rustc": rust_version,
        "timer": {
            "implementation": time.get_clock_info("perf_counter").implementation,
            "resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "monotonic": time.get_clock_info("perf_counter").monotonic,
        },
    }


@dataclass
class SourceConfig:
    source_id: str
    source_type: str
    media_dir: Path
    display_template: str


@dataclass
class QueryCase:
    case_id: str
    category: str
    params: dict[str, str]
    note: str = ""


class CorpusBuilder:
    def __init__(self, db_path: Path, seed: int = 5050):
        self.db_path = db_path.resolve()
        self.seed = seed
        uri = "file:" + self.db_path.as_posix() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.addon_root = self.db_path.parent.parent
        self.sources = self._load_sources()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CorpusBuilder":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _load_sources(self) -> list[SourceConfig]:
        config_path = self.addon_root / "default_config.json"
        user_config_path = self.db_path.parent / "config.json"
        if not config_path.is_file():
            raise FileNotFoundError(
                f"Could not find default_config.json relative to database: {config_path}"
            )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if user_config_path.is_file():
            override = json.loads(user_config_path.read_text(encoding="utf-8"))
            config.update(override)
        result = []
        for item in config["sources"]:
            result.append(
                SourceConfig(
                    source_id=item["id"],
                    source_type=item["type"],
                    media_dir=(self.addon_root / item["path"]).resolve(),
                    display_template=item["display"],
                )
            )
        return result

    @property
    def source_ids(self) -> list[str]:
        return [source.source_id for source in self.sources]

    def database_stats(self) -> dict[str, Any]:
        cursor = self.connection
        count, expressions, files = cursor.execute(
            "SELECT COUNT(*), COUNT(DISTINCT expression), COUNT(DISTINCT file) FROM entries"
        ).fetchone()
        per_source = []
        for row in cursor.execute(
            "SELECT source, COUNT(*), COUNT(DISTINCT file), "
            "SUM(CASE WHEN speaker IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM entries GROUP BY source ORDER BY source"
        ):
            per_source.append(
                {
                    "source": row[0],
                    "rows": row[1],
                    "distinct_files": row[2],
                    "rows_with_speaker": row[3],
                }
            )
        indexes = [
            {"name": row[0], "sql": row[1]}
            for row in cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='entries' ORDER BY name"
            )
        ]
        return {
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size,
            "rows": count,
            "distinct_expressions": expressions,
            "distinct_file_values": files,
            "per_source": per_source,
            "indexes": indexes,
            "source_order": self.source_ids,
        }

    def _rows_spread_across_source(self, source: str, count: int = 4) -> list[sqlite3.Row]:
        bounds = self.connection.execute(
            "SELECT MIN(id), MAX(id), COUNT(*) FROM entries WHERE source=?", (source,)
        ).fetchone()
        if not bounds or bounds[0] is None:
            return []
        low, high, _ = bounds
        rows: list[sqlite3.Row] = []
        for index in range(count * 3):
            fraction = (index + 1) / (count * 3 + 1)
            target = int(low + (high - low) * fraction)
            row = self.connection.execute(
                "SELECT id, expression, reading, source, speaker, file FROM entries "
                "WHERE source=? AND id>=? ORDER BY id LIMIT 1",
                (source, target),
            ).fetchone()
            if row and all(row["expression"] != existing["expression"] for existing in rows):
                rows.append(row)
            if len(rows) >= count:
                break
        return rows

    def _multi_source_rows(self, limit: int = 12) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT expression, reading, COUNT(*) AS row_count, "
                "COUNT(DISTINCT source) AS source_count FROM entries "
                "GROUP BY expression, reading "
                "HAVING COUNT(DISTINCT source)>=2 AND COUNT(*) BETWEEN 2 AND 12 "
                "ORDER BY source_count DESC, row_count ASC, expression LIMIT ?",
                (limit,),
            )
        )

    def _speaker_case(self) -> tuple[str, str | None, list[str]] | None:
        row = self.connection.execute(
            "SELECT expression, reading, COUNT(DISTINCT speaker) AS speakers "
            "FROM entries WHERE source='forvo' AND speaker IS NOT NULL "
            "GROUP BY expression, reading HAVING speakers>=2 AND COUNT(*)<=20 "
            "ORDER BY speakers DESC, expression LIMIT 1"
        ).fetchone()
        if not row:
            return None
        speakers = [
            item[0]
            for item in self.connection.execute(
                "SELECT DISTINCT speaker FROM entries WHERE source='forvo' "
                "AND expression=? AND reading IS ? AND speaker IS NOT NULL ORDER BY speaker LIMIT 4",
                (row["expression"], row["reading"]),
            )
        ]
        return row["expression"], row["reading"], speakers

    @staticmethod
    def _base_params(expression: str, reading: str | None, key: str = "term") -> dict[str, str]:
        params = {key: expression}
        if reading is not None:
            params["reading"] = reading
        return params

    def build(self) -> list[QueryCase]:
        cases: list[QueryCase] = []
        used: set[str] = set()
        per_source_rows: dict[str, list[sqlite3.Row]] = {}
        for source in self.source_ids:
            rows = self._rows_spread_across_source(source)
            per_source_rows[source] = rows
            if not rows:
                continue
            row = rows[0]
            params = self._base_params(row["expression"], row["reading"])
            params["sources"] = source
            cases.append(
                QueryCase(
                    f"source_only_{source}",
                    "source_filter",
                    params,
                    f"Real hit constrained to {source}",
                )
            )
            for sample_index, sample in enumerate(rows[1:3], 1):
                key = canonical_json([sample["expression"], sample["reading"]])
                if key in used:
                    continue
                used.add(key)
                cases.append(
                    QueryCase(
                        f"hit_{source}_{sample_index}",
                        "hit",
                        self._base_params(sample["expression"], sample["reading"]),
                        f"Spread sample anchored in {source}",
                    )
                )

        multi_rows = self._multi_source_rows()
        for index, row in enumerate(multi_rows[:5]):
            cases.append(
                QueryCase(
                    f"multi_source_{index + 1}",
                    "multi_source",
                    self._base_params(row["expression"], row["reading"]),
                    f"DB pair spans {row['source_count']} sources and {row['row_count']} rows",
                )
            )
        if multi_rows:
            row = multi_rows[0]
            reversed_sources = list(reversed(self.source_ids))
            params = self._base_params(row["expression"], row["reading"])
            params["sources"] = ",".join(reversed_sources)
            cases.append(
                QueryCase(
                    "source_order_reversed",
                    "source_order",
                    params,
                    "All configured sources in reverse priority order",
                )
            )
            if len(self.source_ids) >= 2:
                params = self._base_params(row["expression"], row["reading"])
                params["sources"] = ",".join(reversed_sources[:2])
                cases.append(
                    QueryCase(
                        "source_subset_reversed",
                        "source_filter",
                        params,
                        "Two-source subset in explicit priority order",
                    )
                )
            cases.append(
                QueryCase(
                    "expression_alias",
                    "compatibility",
                    self._base_params(row["expression"], row["reading"], key="expression"),
                    "Uses the legacy expression= alias instead of term=",
                )
            )
            cases.append(
                QueryCase(
                    "reading_omitted",
                    "compatibility",
                    self._base_params(row["expression"], None),
                    "Reading omitted: server must query expression alone",
                )
            )

        speaker = self._speaker_case()
        if speaker:
            expression, reading, speakers = speaker
            if speakers:
                params = self._base_params(expression, reading)
                params.update({"sources": "forvo", "user": speakers[0]})
                cases.append(QueryCase("speaker_single", "user_filter", params, "One Forvo user"))
            if len(speakers) >= 2:
                params = self._base_params(expression, reading)
                params.update({"sources": "forvo", "user": ",".join(reversed(speakers[:2]))})
                cases.append(
                    QueryCase(
                        "speaker_order_reversed",
                        "user_order",
                        params,
                        "Two Forvo users in explicit reverse priority order",
                    )
                )

        cases.extend(
            [
                QueryCase(
                    "miss_ascii",
                    "miss",
                    {"term": "__yomitan_benchmark_missing_7f5d9c2e__", "reading": "__none__"},
                    "Deterministic ASCII miss",
                ),
                QueryCase(
                    "miss_unicode",
                    "miss",
                    {"term": "不存在語彙〆七五九", "reading": "ふそんざい"},
                    "Deterministic Unicode miss and URL-encoding check",
                ),
            ]
        )
        return cases

    def build_performance_cases(self, count: int) -> list[QueryCase]:
        """Return disjoint-ish real hits spread through rowid space.

        Each query is constrained to the row's source.  The caller consumes each
        once, allowing a warm SQLite/filesystem but cold response-cache workload.
        """

        bounds = self.connection.execute("SELECT MIN(id), MAX(id) FROM entries").fetchone()
        if not bounds or bounds[0] is None:
            return []
        low, high = int(bounds[0]), int(bounds[1])
        cases: list[QueryCase] = []
        seen: set[str] = set()
        attempts = max(count * 4, count + 1)
        for index in range(attempts):
            fraction = (index + 0.5) / attempts
            target = int(low + (high - low) * fraction)
            row = self.connection.execute(
                "SELECT expression, reading, source FROM entries WHERE id>=? ORDER BY id LIMIT 1",
                (target,),
            ).fetchone()
            if not row:
                continue
            params = self._base_params(row["expression"], row["reading"])
            params["sources"] = row["source"]
            key = canonical_json(params)
            if key in seen:
                continue
            seen.add(key)
            cases.append(
                QueryCase(
                    f"perf_real_hit_{len(cases) + 1}",
                    "performance_real_hit",
                    params,
                    "Used once per endpoint/mode to avoid response-cache hits",
                )
            )
            if len(cases) >= count:
                break
        return cases

    def file_path(self, source_id: str, relative_file: str) -> Path | None:
        source = next((item for item in self.sources if item.source_id == source_id), None)
        if source is None:
            return None
        candidate = (source.media_dir / relative_file).resolve()
        try:
            candidate.relative_to(source.media_dir)
        except ValueError:
            return None
        return candidate


def endpoint_probe(endpoint: Endpoint, timeout: float) -> dict[str, Any]:
    paths = ["/"] if endpoint.name == "original" else ["/healthz", "/"]
    errors: list[str] = []
    for path in paths:
        url = endpoint.base_url.rstrip("/") + path
        try:
            with HttpSession(False, timeout=timeout) as session:
                response = session.request(url)
            if 200 <= response.status < 500:
                return {
                    "available": True,
                    "probe_url": url,
                    "status": response.status,
                    "content_type": response.content_type,
                    "body_preview": safe_decode(response.body[:200]),
                    "latency_ms": response.elapsed_ms,
                    "http_version": response.http_version,
                    "will_close": response.will_close,
                }
            errors.append(f"{path}: HTTP {response.status}")
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return {"available": False, "errors": errors}


def reference_cases(
    endpoint: Endpoint,
    cases: Sequence[QueryCase],
    timeout: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with HttpSession(False, timeout=timeout) as session:
        for case in cases:
            url = query_url(endpoint, case.params)
            try:
                response = session.request(url)
                payload, schema_error = parse_legacy_payload(response.body)
                candidates = payload["audioSources"] if payload else []
                results.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "params": case.params,
                        "note": case.note,
                        "status": response.status,
                        "content_type": response.content_type,
                        "http_version": response.http_version,
                        "will_close": response.will_close,
                        "elapsed_ms": response.elapsed_ms,
                        "body_bytes": len(response.body),
                        "body_sha256": sha256_bytes(response.body),
                        "schema_error": schema_error,
                        "candidates": candidates,
                    }
                )
                if response.status != 200 or schema_error:
                    errors.append(
                        f"{case.case_id}: HTTP {response.status}; {schema_error or 'unexpected status'}"
                    )
            except Exception as exc:
                message = f"{case.case_id}: {type(exc).__name__}: {exc}"
                errors.append(message)
                results.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "params": case.params,
                        "note": case.note,
                        "error": message,
                        "candidates": [],
                    }
                )
    return results, errors


def choose_work_cases(reference: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    hits = [
        item
        for item in reference
        if item.get("status") == 200
        and not item.get("schema_error")
        and 1 <= len(item.get("candidates", [])) <= 20
        and item.get("category")
        in {
            "hit",
            "multi_source",
            "source_filter",
            "source_order",
            "user_filter",
            "user_order",
            "compatibility",
        }
    ]
    misses = [
        item
        for item in reference
        if item.get("status") == 200
        and not item.get("schema_error")
        and len(item.get("candidates", [])) == 0
    ]
    # Include hit and miss paths without letting misses dominate the cache-friendly workload.
    selected = hits[:24]
    if misses:
        selected.append(misses[0])
    return selected


def first_touch_probe(
    endpoint: Endpoint,
    work_case: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Measure first timed post-setup query/audio touch, not a cache flush."""

    output: dict[str, Any] = {
        "qualification": (
            "First explicitly timed request after corpus/reference setup. The original has already "
            "served oracle queries, servers may have existed before the run, and Windows standby/file "
            "caches were not flushed. This is not a cold-start or physical-disk cold read."
        )
    }
    try:
        with HttpSession(False, timeout=timeout) as session:
            query_response = session.request(query_url(endpoint, work_case["params"]))
        payload, schema_error = parse_legacy_payload(query_response.body)
        output["lookup"] = {
            "status": query_response.status,
            "latency_ms": query_response.elapsed_ms,
            "headers_ms": query_response.headers_ms,
            "ttfb_ms": query_response.first_body_byte_ms,
            "body_bytes": len(query_response.body),
            "schema_error": schema_error,
        }
        candidates = payload["audioSources"] if payload else []
        if not candidates:
            output["audio"] = {"supported": False, "reason": "lookup returned no candidates"}
            return output
        audio_url, mismatch = resolve_audio_url(endpoint, candidates[0]["url"])
        with HttpSession(False, timeout=timeout) as session:
            audio = session.request(audio_url)
        output["audio"] = {
            "supported": audio.status == 200 and audio.content_type.startswith("audio/"),
            "status": audio.status,
            "latency_ms": audio.elapsed_ms,
            "headers_ms": audio.headers_ms,
            "ttfb_ms": audio.first_body_byte_ms,
            "body_bytes": len(audio.body),
            "sha256": sha256_bytes(audio.body),
            "content_type": audio.content_type,
            "returned_url_origin_mismatch": mismatch,
        }
    except Exception as exc:
        output["error"] = f"{type(exc).__name__}: {exc}"
    return output


def fetch_cached(
    cache: dict[str, HttpResult],
    url: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> HttpResult:
    key = canonical_json([url, sorted((headers or {}).items())])
    if key not in cache:
        with HttpSession(False, timeout=timeout) as session:
            cache[key] = session.request(url, headers=headers)
    return cache[key]


def validate_correctness(
    endpoint: Endpoint,
    reference: Sequence[dict[str, Any]],
    source_ids: Sequence[str],
    corpus: CorpusBuilder,
    timeout: float,
    max_audio_candidates: int,
) -> dict[str, Any]:
    response_cache: dict[str, HttpResult] = {}
    audio_cache: dict[str, HttpResult] = {}
    original_audio_cache: dict[str, HttpResult] = {}
    case_results: list[dict[str, Any]] = []
    audio_budget = max_audio_candidates

    for expected in reference:
        case_result: dict[str, Any] = {
            "case_id": expected["case_id"],
            "category": expected["category"],
        }
        try:
            lookup = fetch_cached(
                response_cache, query_url(endpoint, expected["params"]), timeout
            )
            payload, schema_error = parse_legacy_payload(lookup.body)
            actual_candidates = payload["audioSources"] if payload else []
            expected_candidates = expected.get("candidates", [])
            expected_names = [item["name"] for item in expected_candidates]
            actual_names = [item["name"] for item in actual_candidates]
            case_result.update(
                {
                    "status": lookup.status,
                    "content_type": lookup.content_type,
                    "schema_error": schema_error,
                    "response_bytes": len(lookup.body),
                    "expected_response_bytes": expected.get("body_bytes"),
                    "candidate_count": len(actual_candidates),
                    "expected_candidate_count": len(expected_candidates),
                    "candidate_count_match": len(actual_candidates) == len(expected_candidates),
                    "candidate_name_order_match": actual_names == expected_names,
                    "expected_names": expected_names,
                    "actual_names": actual_names,
                    "audio_checked": 0,
                    "audio_mismatches": [],
                    "direct_source_mismatches": [],
                    "url_origin_mismatches": 0,
                    "url_host_policy_mismatches": 0,
                    "disk_reference_mismatches": [],
                }
            )

            if endpoint.name != "original":
                dedicated = fetch_cached(
                    response_cache,
                    query_url(endpoint, expected["params"], path="/v1/candidates"),
                    timeout,
                )
                try:
                    dedicated_payload = json.loads(dedicated.body.decode("utf-8"))
                except Exception as exc:
                    dedicated_payload = None
                    case_result["dedicated_candidates_error"] = f"invalid JSON: {exc}"
                dedicated_items = (
                    dedicated_payload.get("candidates", [])
                    if isinstance(dedicated_payload, dict)
                    and isinstance(dedicated_payload.get("version"), str)
                    and bool(dedicated_payload.get("version"))
                    and isinstance(dedicated_payload.get("candidates"), list)
                    else []
                )
                expected_sources = [
                    source_and_file_from_legacy_url(item["url"], source_ids)[0]
                    for item in expected_candidates
                ]
                dedicated_names = [item.get("name") for item in dedicated_items]
                dedicated_sources = [item.get("source") for item in dedicated_items]
                dedicated_urls = [item.get("url") for item in dedicated_items]
                actual_urls = [item.get("url") for item in actual_candidates]
                dedicated_items_valid = all(
                    dedicated_candidate_item_valid(item) for item in dedicated_items
                )
                dedicated_numeric_hosts = all(
                    (urllib.parse.urlsplit(str(url)).hostname or "").lower() == "127.0.0.1"
                    for url in dedicated_urls
                )
                case_result.update(
                    {
                        "dedicated_candidates_status": dedicated.status,
                        "dedicated_candidates_shape": bool(
                            isinstance(dedicated_payload, dict)
                            and isinstance(dedicated_payload.get("version"), str)
                            and bool(dedicated_payload.get("version"))
                            and isinstance(dedicated_payload.get("candidates"), list)
                        ),
                        "dedicated_candidate_count_match": len(dedicated_items)
                        == len(expected_candidates),
                        "dedicated_candidate_name_order_match": dedicated_names == expected_names,
                        "dedicated_candidate_source_order_match": dedicated_sources
                        == expected_sources,
                        "dedicated_candidate_item_shape": dedicated_items_valid,
                        "dedicated_candidate_url_order_match": dedicated_urls == actual_urls,
                        "dedicated_candidate_numeric_hosts": dedicated_numeric_hosts,
                    }
                )
            else:
                case_result.update(
                    {
                        "dedicated_candidates_status": None,
                        "dedicated_candidates_shape": None,
                        "dedicated_candidate_count_match": None,
                        "dedicated_candidate_name_order_match": None,
                        "dedicated_candidate_source_order_match": None,
                        "dedicated_candidate_item_shape": None,
                        "dedicated_candidate_url_order_match": None,
                        "dedicated_candidate_numeric_hosts": None,
                    }
                )

            pair_count = min(len(expected_candidates), len(actual_candidates), audio_budget)
            for index in range(pair_count):
                expected_candidate = expected_candidates[index]
                actual_candidate = actual_candidates[index]
                expected_url = expected_candidate["url"]
                expected_fetch_url = numeric_loopback_url(expected_url)
                actual_fetch_url, origin_mismatch = resolve_audio_url(
                    endpoint, actual_candidate["url"]
                )
                if origin_mismatch:
                    case_result["url_origin_mismatches"] += 1
                returned_host = (urllib.parse.urlsplit(actual_candidate["url"]).hostname or "").lower()
                if endpoint.name != "original" and returned_host != "127.0.0.1":
                    case_result["url_host_policy_mismatches"] += 1
                reference_audio = fetch_cached(original_audio_cache, expected_fetch_url, timeout)
                actual_audio = fetch_cached(audio_cache, actual_fetch_url, timeout)
                expected_hash = sha256_bytes(reference_audio.body)
                actual_hash = sha256_bytes(actual_audio.body)
                expected_source, expected_file = source_and_file_from_legacy_url(
                    expected_url, source_ids
                )
                actual_source, actual_file = source_and_file_from_legacy_url(
                    actual_candidate["url"], source_ids
                )
                if actual_source is not None and actual_source != expected_source:
                    case_result["direct_source_mismatches"].append(
                        {
                            "index": index,
                            "expected": expected_source,
                            "actual": actual_source,
                        }
                    )
                audio_matches = (
                    reference_audio.status == actual_audio.status == 200
                    and expected_hash == actual_hash
                    and len(reference_audio.body) == len(actual_audio.body)
                    and reference_audio.content_type == actual_audio.content_type
                )
                if not audio_matches:
                    case_result["audio_mismatches"].append(
                        {
                            "index": index,
                            "name": expected_candidate["name"],
                            "expected_status": reference_audio.status,
                            "actual_status": actual_audio.status,
                            "expected_bytes": len(reference_audio.body),
                            "actual_bytes": len(actual_audio.body),
                            "expected_sha256": expected_hash,
                            "actual_sha256": actual_hash,
                            "expected_content_type": reference_audio.content_type,
                            "actual_content_type": actual_audio.content_type,
                            "actual_url": actual_fetch_url,
                        }
                    )
                if expected_source and expected_file:
                    disk_path = corpus.file_path(expected_source, expected_file)
                    if disk_path and disk_path.is_file():
                        disk_hash = hashlib.sha256(disk_path.read_bytes()).hexdigest()
                        if disk_hash != expected_hash:
                            case_result["disk_reference_mismatches"].append(
                                {
                                    "index": index,
                                    "path": str(disk_path),
                                    "disk_sha256": disk_hash,
                                    "http_sha256": expected_hash,
                                }
                            )
                case_result["audio_checked"] += 1
                audio_budget -= 1

            case_result["audio_check_limited"] = pair_count < min(
                len(expected_candidates), len(actual_candidates)
            )
            case_result["pass"] = bool(
                lookup.status == expected.get("status") == 200
                and schema_error is None
                and case_result["candidate_count_match"]
                and case_result["candidate_name_order_match"]
                and not case_result["audio_mismatches"]
                and not case_result["direct_source_mismatches"]
                and not case_result["disk_reference_mismatches"]
                and case_result["url_origin_mismatches"] == 0
                and case_result["url_host_policy_mismatches"] == 0
                and (
                    endpoint.name == "original"
                    or (
                        case_result.get("dedicated_candidates_status") == 200
                        and case_result.get("dedicated_candidates_shape")
                        and case_result.get("dedicated_candidate_count_match")
                        and case_result.get("dedicated_candidate_name_order_match")
                        and case_result.get("dedicated_candidate_source_order_match")
                        and case_result.get("dedicated_candidate_item_shape")
                        and case_result.get("dedicated_candidate_url_order_match")
                        and case_result.get("dedicated_candidate_numeric_hosts")
                    )
                )
            )
        except Exception as exc:
            case_result.update(
                {
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        case_results.append(case_result)

    passed = sum(bool(item.get("pass")) for item in case_results)
    audio_checked = sum(int(item.get("audio_checked", 0)) for item in case_results)
    return {
        "endpoint": endpoint.name,
        "cases_total": len(case_results),
        "cases_passed": passed,
        "cases_failed": len(case_results) - passed,
        "audio_candidates_checked": audio_checked,
        "audio_budget": max_audio_candidates,
        "pass": passed == len(case_results),
        "cases": case_results,
    }


def feature_checks(
    endpoint: Endpoint,
    work_case: dict[str, Any],
    reference_first_candidate: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    for feature_name, path in (("healthz", "/healthz"), ("info", "/v1/info")):
        try:
            with HttpSession(False, timeout=timeout) as session:
                response = session.request(endpoint.base_url.rstrip("/") + path)
            parsed_json: Any = None
            with contextlib.suppress(Exception):
                parsed_json = json.loads(response.body.decode("utf-8"))
            if feature_name == "healthz":
                dedicated_shape = bool(
                    isinstance(parsed_json, dict) and parsed_json.get("status") == "ok"
                )
            else:
                dedicated_shape = bool(
                    isinstance(parsed_json, dict)
                    and isinstance(parsed_json.get("lookupMode"), str)
                    and isinstance(parsed_json.get("sources"), list)
                )
            checks[feature_name] = {
                "supported": response.status == 200
                and response.content_type == "application/json"
                and dedicated_shape,
                "status": response.status,
                "content_type": response.content_type,
                "body_bytes": len(response.body),
                "dedicated_shape": dedicated_shape,
                "json_shape": type(parsed_json).__name__ if parsed_json is not None else None,
                "body_preview": safe_decode(response.body[:500]),
            }
        except Exception as exc:
            checks[feature_name] = {
                "supported": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    try:
        cors_url = query_url(endpoint, work_case["params"])
        with HttpSession(False, timeout=timeout) as session:
            cors_response = session.request(
                cors_url,
                headers={"Origin": "chrome-extension://yomitan-benchmark"},
            )
        allow_origin = cors_response.headers.get("access-control-allow-origin")
        checks["cors"] = {
            "supported": cors_response.status == 200 and allow_origin == "*",
            "status": cors_response.status,
            "access_control_allow_origin": allow_origin,
        }
    except Exception as exc:
        checks["cors"] = {
            "supported": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with HttpSession(False, timeout=timeout) as session:
            options_response = session.request(
                endpoint.base_url.rstrip("/") + "/",
                method="OPTIONS",
                headers={
                    "Origin": "chrome-extension://yomitan-benchmark",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Range, If-None-Match",
                },
            )
        allow_origin = options_response.headers.get("access-control-allow-origin")
        allow_methods = options_response.headers.get("access-control-allow-methods", "")
        allow_headers = options_response.headers.get("access-control-allow-headers", "")
        allowed_header_names = {
            item.strip().lower() for item in allow_headers.split(",") if item.strip()
        }
        checks["options"] = {
            "supported": options_response.status in (200, 204)
            and allow_origin == "*"
            and "GET" in {item.strip().upper() for item in allow_methods.split(",")}
            and {"range", "if-none-match"}.issubset(allowed_header_names),
            "status": options_response.status,
            "access_control_allow_origin": allow_origin,
            "access_control_allow_methods": allow_methods,
            "access_control_allow_headers": allow_headers,
            "body_bytes": len(options_response.body),
        }
    except Exception as exc:
        checks["options"] = {
            "supported": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        candidates_url = query_url(endpoint, work_case["params"], path="/v1/candidates")
        with HttpSession(False, timeout=timeout) as session:
            response = session.request(candidates_url)
        parsed_json: Any = None
        with contextlib.suppress(Exception):
            parsed_json = json.loads(response.body.decode("utf-8"))
        dedicated_shape = bool(
            isinstance(parsed_json, dict)
            and isinstance(parsed_json.get("version"), str)
            and bool(parsed_json.get("version"))
            and isinstance(parsed_json.get("candidates"), list)
        )
        expected_names = [item["name"] for item in work_case.get("candidates", [])]
        expected_sources = [
            urllib.parse.unquote(urllib.parse.urlsplit(item["url"]).path)
            .lstrip("/")
            .partition("/")[0]
            for item in work_case.get("candidates", [])
        ]
        dedicated_candidates = parsed_json.get("candidates", []) if dedicated_shape else []
        actual_names = [item.get("name") for item in dedicated_candidates]
        actual_sources = [item.get("source") for item in dedicated_candidates]
        item_shape = all(
            dedicated_candidate_item_valid(item) for item in dedicated_candidates
        )
        numeric_hosts = all(
            (urllib.parse.urlsplit(str(item.get("url"))).hostname or "").lower()
            == "127.0.0.1"
            for item in dedicated_candidates
        )
        dedicated_parity = bool(
            dedicated_shape
            and item_shape
            and numeric_hosts
            and actual_names == expected_names
            and actual_sources == expected_sources
        )
        checks["v1_candidates"] = {
            "supported": response.status == 200
            and response.content_type == "application/json"
            and dedicated_shape
            and item_shape
            and numeric_hosts,
            "status": response.status,
            "content_type": response.content_type,
            "body_bytes": len(response.body),
            "json_shape": type(parsed_json).__name__ if parsed_json is not None else None,
            "dedicated_shape": dedicated_shape,
            "candidate_item_shape": item_shape,
            "all_numeric_loopback": numeric_hosts,
            "candidate_name_order_match": actual_names == expected_names if dedicated_shape else False,
            "candidate_source_order_match": actual_sources == expected_sources
            if dedicated_shape
            else False,
            "parity": dedicated_parity,
            "expected_names": expected_names,
            "actual_names": actual_names,
            "expected_sources": expected_sources,
            "actual_sources": actual_sources,
            "legacy_catch_all_alias": bool(
                isinstance(parsed_json, dict)
                and parsed_json.get("type") == "audioSourceList"
                and isinstance(parsed_json.get("audioSources"), list)
            ),
            "body_preview": safe_decode(response.body[:500]),
        }
    except Exception as exc:
        checks["v1_candidates"] = {
            "supported": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    expected_url = reference_first_candidate["url"]
    expected_source_ids = [
        urllib.parse.unquote(urllib.parse.urlsplit(expected_url).path).lstrip("/").partition("/")[0]
    ]
    try:
        with HttpSession(False, timeout=timeout) as session:
            expected_audio = session.request(numeric_loopback_url(expected_url))
        expected_hash = sha256_bytes(expected_audio.body)
    except Exception as exc:
        expected_audio = None
        expected_hash = None
        checks["reference_audio_error"] = f"{type(exc).__name__}: {exc}"

    try:
        lookup_url = query_url(endpoint, work_case["params"])
        with HttpSession(False, timeout=timeout) as session:
            lookup = session.request(lookup_url)
        payload, _ = parse_legacy_payload(lookup.body)
        with HttpSession(False, timeout=timeout) as session:
            lookup_repeat = session.request(lookup_url)
        repeat_payload, repeat_schema_error = parse_legacy_payload(lookup_repeat.body)
        first_urls = [item["url"] for item in payload["audioSources"]] if payload else []
        repeat_urls = (
            [item["url"] for item in repeat_payload["audioSources"]]
            if repeat_payload
            else []
        )
        checks["stable_urls"] = {
            "supported": bool(first_urls)
            and repeat_schema_error is None
            and first_urls == repeat_urls
            and (
                endpoint.name == "original"
                or all(
                    (urllib.parse.urlsplit(url).hostname or "").lower() == "127.0.0.1"
                    for url in first_urls
                )
            ),
            "first_urls": first_urls,
            "repeat_urls": repeat_urls,
            "exact_repeat_match": first_urls == repeat_urls,
            "all_numeric_loopback": all(
                (urllib.parse.urlsplit(url).hostname or "").lower() == "127.0.0.1"
                for url in first_urls
            ),
        }
        actual_returned = payload["audioSources"][0]["url"] if payload and payload["audioSources"] else None
        if not actual_returned:
            raise RuntimeError("legacy lookup did not yield an audio URL")
        audio_url, mismatch = resolve_audio_url(endpoint, actual_returned)
        with HttpSession(False, timeout=timeout) as session:
            head = session.request(audio_url, method="HEAD")
        expected_length = len(expected_audio.body) if expected_audio else None
        checks["head"] = {
            "supported": head.status == 200
            and head.headers.get("content-length") == str(expected_length)
            and head.headers.get("accept-ranges", "").lower() == "bytes"
            and len(head.body) == 0
            and (expected_audio is None or head.content_type == expected_audio.content_type),
            "status": head.status,
            "content_length": head.headers.get("content-length"),
            "accept_ranges": head.headers.get("accept-ranges"),
            "body_bytes": len(head.body),
            "body_is_empty": len(head.body) == 0,
            "url_origin_mismatch": mismatch,
        }
        etag = head.headers.get("etag")
        if etag:
            with HttpSession(False, timeout=timeout) as session:
                conditional = session.request(
                    audio_url, headers={"If-None-Match": etag}
                )
            checks["etag"] = {
                "supported": conditional.status == 304
                and len(conditional.body) == 0
                and conditional.headers.get("etag") == etag,
                "etag": etag,
                "conditional_status": conditional.status,
                "conditional_body_bytes": len(conditional.body),
                "conditional_etag": conditional.headers.get("etag"),
            }
        else:
            checks["etag"] = {
                "supported": False,
                "reason": "audio HEAD response omitted ETag",
            }
        reference_body = expected_audio.body if expected_audio else b""
        size = len(reference_body)
        # A 416 response may carry an explanatory representation.  Its
        # protocol requirements here are the status and unsatisfied
        # Content-Range; unlike a 206 payload, its body is not audio data that
        # can be compared byte-for-byte with the reference asset.
        range_specs: list[tuple[str, int, bytes | None, str | None]] = [
            (
                "prefix_0_1023",
                206,
                reference_body[:1024],
                f"bytes 0-{min(1023, max(0, size - 1))}/{size}",
            ),
            (
                "middle_10_19",
                206,
                reference_body[10:20],
                f"bytes 10-{min(19, max(0, size - 1))}/{size}",
            ),
            (
                "suffix_32",
                206,
                reference_body[-32:],
                f"bytes {max(0, size - 32)}-{max(0, size - 1)}/{size}",
            ),
            (
                "full_span",
                206,
                reference_body,
                f"bytes 0-{max(0, size - 1)}/{size}",
            ),
            (
                "open_from_10",
                206,
                reference_body[10:],
                f"bytes 10-{max(0, size - 1)}/{size}",
            ),
            (
                "suffix_larger_than_file",
                206,
                reference_body,
                f"bytes 0-{max(0, size - 1)}/{size}",
            ),
            (
                "unsatisfiable",
                416,
                None,
                f"bytes */{size}",
            ),
        ]
        range_headers = [
            "bytes=0-1023",
            "bytes=10-19",
            "bytes=-32",
            f"bytes=0-{max(0, size - 1)}",
            "bytes=10-",
            f"bytes=-{size + 100}",
            f"bytes={size + 100}-{size + 200}",
        ]
        range_matrix: list[dict[str, Any]] = []
        for (label, expected_status, expected_bytes, expected_content_range), header in zip(
            range_specs, range_headers
        ):
            with HttpSession(False, timeout=timeout) as session:
                ranged = session.request(audio_url, headers={"Range": header})
            item_pass = (
                ranged.status == expected_status
                and (expected_bytes is None or ranged.body == expected_bytes)
                and ranged.headers.get("content-range") == expected_content_range
            )
            range_matrix.append(
                {
                    "case": label,
                    "request": header,
                    "pass": item_pass,
                    "status": ranged.status,
                    "expected_status": expected_status,
                    "content_range": ranged.headers.get("content-range"),
                    "expected_content_range": expected_content_range,
                    "body_bytes": len(ranged.body),
                    "expected_body_bytes": (
                        len(expected_bytes) if expected_bytes is not None else None
                    ),
                    "body_policy": (
                        "byte_exact" if expected_bytes is not None else "representation_allowed"
                    ),
                    "bytes_match_reference": (
                        ranged.body == expected_bytes if expected_bytes is not None else None
                    ),
                }
            )
        checks["range"] = {
            "supported": all(item["pass"] for item in range_matrix),
            "matrix": range_matrix,
        }
        with HttpSession(False, timeout=timeout) as session:
            ranged_head = session.request(
                audio_url, method="HEAD", headers={"Range": "bytes=0-15"}
            )
        checks["range_head"] = {
            "supported": ranged_head.status == 206
            and len(ranged_head.body) == 0
            and ranged_head.headers.get("content-length") == str(min(16, size))
            and ranged_head.headers.get("content-range")
            == f"bytes 0-{min(15, max(0, size - 1))}/{size}",
            "status": ranged_head.status,
            "body_bytes": len(ranged_head.body),
            "content_length": ranged_head.headers.get("content-length"),
            "content_range": ranged_head.headers.get("content-range"),
        }
    except Exception as exc:
        checks["head"] = checks.get(
            "head", {"supported": False, "error": f"{type(exc).__name__}: {exc}"}
        )
        checks["range"] = {
            "supported": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        checks.setdefault(
            "range_head",
            {"supported": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        checks.setdefault(
            "etag",
            {"supported": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        checks.setdefault(
            "stable_urls",
            {"supported": False, "error": f"{type(exc).__name__}: {exc}"},
        )

    try:
        play_url = query_url(endpoint, work_case["params"], path="/v1/play")
        with HttpSession(False, timeout=timeout) as session:
            play = session.request(play_url)
        play_hash = sha256_bytes(play.body)
        checks["v1_play"] = {
            "supported": (
                play.status == 200
                and play.content_type.startswith("audio/")
                and expected_hash is not None
                and play_hash == expected_hash
            ),
            "status": play.status,
            "content_type": play.content_type,
            "body_bytes": len(play.body),
            "sha256": play_hash,
            "matches_first_legacy_candidate": play_hash == expected_hash,
        }
        with HttpSession(False, timeout=timeout) as session:
            play_head = session.request(play_url, method="HEAD")
        checks["v1_play_head"] = {
            "supported": play_head.status == 200
            and expected_audio is not None
            and play_head.headers.get("content-length") == str(len(expected_audio.body))
            and len(play_head.body) == 0
            and play_head.content_type == expected_audio.content_type,
            "status": play_head.status,
            "content_type": play_head.content_type,
            "content_length": play_head.headers.get("content-length"),
            "body_bytes": len(play_head.body),
        }
        with HttpSession(False, timeout=timeout) as session:
            play_range = session.request(play_url, headers={"Range": "bytes=0-1023"})
        play_prefix = expected_audio.body[:1024] if expected_audio else b""
        checks["v1_play_range"] = {
            "supported": play_range.status == 206
            and play_range.body == play_prefix
            and play_range.headers.get("content-range", "").startswith("bytes 0-"),
            "status": play_range.status,
            "content_range": play_range.headers.get("content-range"),
            "body_bytes": len(play_range.body),
            "prefix_matches_reference": play_range.body == play_prefix,
        }
    except Exception as exc:
        checks["v1_play"] = {
            "supported": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        checks.setdefault(
            "v1_play_head",
            {"supported": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        checks.setdefault(
            "v1_play_range",
            {"supported": False, "error": f"{type(exc).__name__}: {exc}"},
        )
    return checks


def legacy_returned_hostname_probe(
    reference_endpoint: Endpoint,
    returned_url: str,
    timeout: float,
    repeats: int,
) -> dict[str, Any]:
    normalized_url, _ = resolve_audio_url(reference_endpoint, returned_url)
    variants = {
        "as_returned_localhost": returned_url,
        "normalized_127_0_0_1": normalized_url,
    }
    output: dict[str, Any] = {
        "purpose": (
            "Isolates the legacy JSON hostname effect. These observations are excluded from "
            "the fair endpoint benchmark tables."
        ),
        "returned_url": returned_url,
        "normalized_url": normalized_url,
        "repeats": repeats,
        "variants": {},
    }
    dns_latencies: list[float] = []
    dns_errors: list[str] = []
    returned_parts = urllib.parse.urlsplit(returned_url)
    for _ in range(repeats):
        started = time.perf_counter_ns()
        try:
            socket.getaddrinfo(
                returned_parts.hostname or "localhost",
                returned_parts.port or 80,
                type=socket.SOCK_STREAM,
            )
            dns_latencies.append((time.perf_counter_ns() - started) / 1_000_000.0)
        except Exception as exc:
            dns_errors.append(f"{type(exc).__name__}: {exc}")
    output["getaddrinfo_latency_ms"] = distribution(dns_latencies)
    output["getaddrinfo_errors"] = dns_errors
    expected_hash: str | None = None
    for name, url in variants.items():
        latencies: list[float] = []
        headers_ms: list[float] = []
        ttfb_ms: list[float] = []
        hashes: list[str] = []
        errors: list[str] = []
        for _ in range(repeats):
            try:
                with HttpSession(False, timeout=timeout) as session:
                    response = session.request(url)
                latencies.append(response.elapsed_ms)
                headers_ms.append(response.headers_ms)
                if response.first_body_byte_ms is not None:
                    ttfb_ms.append(response.first_body_byte_ms)
                hashes.append(sha256_bytes(response.body))
                if response.status != 200:
                    errors.append(f"HTTP {response.status}")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        if hashes and expected_hash is None:
            expected_hash = hashes[0]
        output["variants"][name] = {
            "latency_ms": distribution(latencies),
            "headers_ms": distribution(headers_ms),
            "ttfb_ms": distribution(ttfb_ms),
            "errors": errors,
            "all_bytes_identical": bool(hashes)
            and len(set(hashes)) == 1
            and (expected_hash is None or hashes[0] == expected_hash),
        }
    raw = output["variants"]["as_returned_localhost"]["latency_ms"].get("p50")
    normalized = output["variants"]["normalized_127_0_0_1"]["latency_ms"].get("p50")
    if raw is not None and normalized not in (None, 0):
        output["p50_penalty_ms"] = raw - normalized
        output["p50_slowdown_x"] = raw / normalized
    output["qualification"] = (
        "Measured with Python's socket resolver/client on this machine. Chromium, curl, DNS cache "
        "state, and Happy Eyeballs implementations can behave differently."
    )
    return output


def summarize_trials(trials: Sequence[dict[str, Any]]) -> dict[str, Any]:
    all_latencies = [
        latency
        for trial in trials
        for latency in trial.get("latencies_ms", [])
    ]
    throughputs = [trial["throughput_rps"] for trial in trials if "throughput_rps" in trial]
    body_sizes = [
        size
        for trial in trials
        for size in trial.get("body_sizes", [])
    ]
    headers = [value for trial in trials for value in trial.get("headers_ms", [])]
    ttfb = [value for trial in trials for value in trial.get("ttfb_ms", [])]
    return {
        "latency_ms": distribution(all_latencies),
        "headers_ms": distribution(headers),
        "ttfb_ms": distribution(ttfb),
        "trial_throughput_rps": distribution(throughputs),
        "response_body_bytes": distribution(body_sizes),
        "requests": sum(int(trial.get("requests", 0)) for trial in trials),
        "successes": sum(int(trial.get("successes", 0)) for trial in trials),
        "errors": sum(int(trial.get("errors", 0)) for trial in trials),
        "protocol_counts": dict(
            Counter(
                protocol
                for trial in trials
                for protocol in trial.get("protocols", [])
            )
        ),
        "will_close_responses": sum(int(trial.get("will_close_responses", 0)) for trial in trials),
        "transport_retries": sum(int(trial.get("transport_retries", 0)) for trial in trials),
        "new_connection_responses": sum(
            int(trial.get("new_connection_responses", 0)) for trial in trials
        ),
        "reused_connection_responses": sum(
            int(trial.get("reused_connection_responses", 0)) for trial in trials
        ),
    }


def audio_size_class(size: int) -> str:
    if size <= 16 * 1024:
        return "tiny_0_16KiB"
    if size <= 64 * 1024:
        return "small_16_64KiB"
    if size <= 256 * 1024:
        return "medium_64_256KiB"
    return "large_over_256KiB"


def audio_size_class_summary(
    sizes: Sequence[int],
    ttfb_ms: Sequence[float | None],
    complete_ms: Sequence[float],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"sizes": [], "ttfb": [], "complete": []}
    )
    for size, ttfb, complete in zip(sizes, ttfb_ms, complete_ms):
        bucket = grouped[audio_size_class(int(size))]
        bucket["sizes"].append(float(size))
        if ttfb is not None:
            bucket["ttfb"].append(float(ttfb))
        bucket["complete"].append(float(complete))
    return {
        name: {
            "n": len(values["complete"]),
            "body_bytes": distribution(values["sizes"]),
            "ttfb_ms": distribution(values["ttfb"]),
            "complete_ms": distribution(values["complete"]),
        }
        for name, values in sorted(grouped.items())
    }


def benchmark_lookup(
    endpoint: Endpoint,
    work_cases: Sequence[dict[str, Any]],
    keep_alive: bool,
    repeats: int,
    iterations: int,
    warmup: int,
    timeout: float,
    unique_stream: bool = False,
    require_hit: bool = False,
) -> dict[str, Any]:
    urls = [query_url(endpoint, case["params"]) for case in work_cases]
    if not urls:
        return {"supported": False, "reason": "no usable corpus cases"}
    trials: list[dict[str, Any]] = []
    for trial_number in range(repeats):
        latencies: list[float] = []
        headers_ms: list[float] = []
        ttfb_ms: list[float] = []
        body_sizes: list[int] = []
        protocols: list[str] = []
        errors: list[str] = []
        will_close = 0
        retries = 0
        new_connections = 0
        reused_connections = 0
        connection_new_flags: list[bool] = []
        with HttpSession(keep_alive, timeout=timeout) as session:
            for index in range(warmup):
                with contextlib.suppress(Exception):
                    session.request(urls[(index + trial_number) % len(urls)])
            wall_started = time.perf_counter_ns()
            for index in range(iterations):
                if unique_stream:
                    url = urls[(trial_number * iterations + index) % len(urls)]
                else:
                    url = urls[(index * 7 + trial_number) % len(urls)]
                try:
                    response = session.request(url)
                    latencies.append(response.elapsed_ms)
                    headers_ms.append(response.headers_ms)
                    if response.first_body_byte_ms is not None:
                        ttfb_ms.append(response.first_body_byte_ms)
                    body_sizes.append(len(response.body))
                    protocols.append(response.http_version)
                    will_close += int(response.will_close)
                    retries += response.retry_count
                    new_connections += int(response.connection_was_new)
                    reused_connections += int(not response.connection_was_new)
                    connection_new_flags.append(response.connection_was_new)
                    if response.status != 200:
                        errors.append(f"HTTP {response.status}")
                    else:
                        payload, schema_error = parse_legacy_payload(response.body)
                        if schema_error:
                            errors.append(schema_error)
                        elif require_hit and payload and not payload["audioSources"]:
                            errors.append("real-hit workload unexpectedly returned no candidates")
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
            wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000.0
        successes = iterations - len(errors)
        trials.append(
            {
                "trial": trial_number + 1,
                "requests": iterations,
                "successes": successes,
                "errors": len(errors),
                "error_samples": errors[:10],
                "wall_ms": wall_ms,
                "throughput_rps": successes / (wall_ms / 1000.0),
                "latencies_ms": latencies,
                "headers_ms": headers_ms,
                "ttfb_ms": ttfb_ms,
                "body_sizes": body_sizes,
                "protocols": protocols,
                "will_close_responses": will_close,
                "transport_retries": retries,
                "new_connection_responses": new_connections,
                "reused_connection_responses": reused_connections,
                "connection_new_flags": connection_new_flags,
                "summary": distribution(latencies),
            }
        )
    return {
        "supported": True,
        "endpoint": endpoint.name,
        "connection_mode": "keep_alive" if keep_alive else "connection_close",
        "repeats": repeats,
        "iterations_per_trial": iterations,
        "warmup_per_trial": warmup,
        "unique_stream": unique_stream,
        "require_hit": require_hit,
        "trials": trials,
        "aggregate": summarize_trials(trials),
    }


def benchmark_two_stage(
    endpoint: Endpoint,
    hit_cases: Sequence[dict[str, Any]],
    keep_alive: bool,
    repeats: int,
    iterations: int,
    warmup: int,
    timeout: float,
) -> dict[str, Any]:
    cases = [case for case in hit_cases if case.get("candidates")]
    if not cases:
        return {"supported": False, "reason": "no hit cases"}
    trials: list[dict[str, Any]] = []
    for trial_number in range(repeats):
        total_latencies: list[float] = []
        lookup_latencies: list[float] = []
        audio_latencies: list[float] = []
        lookup_headers_ms: list[float] = []
        lookup_ttfb_ms: list[float] = []
        audio_headers_ms: list[float] = []
        audio_ttfb_ms: list[float | None] = []
        audio_body_sizes: list[int] = []
        body_sizes: list[int] = []
        protocols: list[str] = []
        errors: list[str] = []
        will_close = 0
        retries = 0
        new_connections = 0
        reused_connections = 0
        connection_new_flags: list[bool] = []

        def one_operation(session: HttpSession, index: int) -> None:
            nonlocal will_close, retries, new_connections, reused_connections
            case = cases[(index * 5 + trial_number) % len(cases)]
            op_started = time.perf_counter_ns()
            lookup = session.request(query_url(endpoint, case["params"]))
            payload, schema_error = parse_legacy_payload(lookup.body)
            if lookup.status != 200 or schema_error or not payload or not payload["audioSources"]:
                raise RuntimeError(
                    f"lookup failed: HTTP {lookup.status}, schema={schema_error}, candidates="
                    f"{len(payload['audioSources']) if payload else 0}"
                )
            audio_url, _ = resolve_audio_url(endpoint, payload["audioSources"][0]["url"])
            audio = session.request(audio_url)
            if audio.status != 200 or not audio.content_type.startswith("audio/"):
                raise RuntimeError(f"audio failed: HTTP {audio.status}, {audio.content_type}")
            total_latencies.append((time.perf_counter_ns() - op_started) / 1_000_000.0)
            lookup_latencies.append(lookup.elapsed_ms)
            audio_latencies.append(audio.elapsed_ms)
            lookup_headers_ms.append(lookup.headers_ms)
            if lookup.first_body_byte_ms is not None:
                lookup_ttfb_ms.append(lookup.first_body_byte_ms)
            audio_headers_ms.append(audio.headers_ms)
            audio_ttfb_ms.append(audio.first_body_byte_ms)
            audio_body_sizes.append(len(audio.body))
            body_sizes.append(len(lookup.body) + len(audio.body))
            protocols.extend([lookup.http_version, audio.http_version])
            will_close += int(lookup.will_close) + int(audio.will_close)
            retries += lookup.retry_count + audio.retry_count
            new_connections += int(lookup.connection_was_new) + int(audio.connection_was_new)
            reused_connections += int(not lookup.connection_was_new) + int(
                not audio.connection_was_new
            )
            connection_new_flags.extend(
                [lookup.connection_was_new, audio.connection_was_new]
            )

        with HttpSession(keep_alive, timeout=timeout) as session:
            for index in range(warmup):
                with contextlib.suppress(Exception):
                    one_operation(session, index)
            # Discard warmup observations appended by one_operation.
            total_latencies.clear()
            lookup_latencies.clear()
            audio_latencies.clear()
            lookup_headers_ms.clear()
            lookup_ttfb_ms.clear()
            audio_headers_ms.clear()
            audio_ttfb_ms.clear()
            audio_body_sizes.clear()
            body_sizes.clear()
            protocols.clear()
            will_close = 0
            retries = 0
            new_connections = 0
            reused_connections = 0
            connection_new_flags.clear()
            wall_started = time.perf_counter_ns()
            for index in range(iterations):
                try:
                    one_operation(session, index)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
            wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000.0
        trials.append(
            {
                "trial": trial_number + 1,
                "requests": iterations,
                "successes": iterations - len(errors),
                "errors": len(errors),
                "error_samples": errors[:10],
                "wall_ms": wall_ms,
                "throughput_rps": (iterations - len(errors)) / (wall_ms / 1000.0),
                "latencies_ms": total_latencies,
                "lookup_latencies_ms": lookup_latencies,
                "audio_latencies_ms": audio_latencies,
                "headers_ms": audio_headers_ms,
                "ttfb_ms": [value for value in audio_ttfb_ms if value is not None],
                "lookup_headers_ms": lookup_headers_ms,
                "lookup_ttfb_ms": lookup_ttfb_ms,
                "audio_headers_ms": audio_headers_ms,
                "audio_ttfb_ms": audio_ttfb_ms,
                "audio_body_sizes": audio_body_sizes,
                "body_sizes": body_sizes,
                "protocols": protocols,
                "will_close_responses": will_close,
                "transport_retries": retries,
                "new_connection_responses": new_connections,
                "reused_connection_responses": reused_connections,
                "connection_new_flags": connection_new_flags,
                "summary": distribution(total_latencies),
                "lookup_summary": distribution(lookup_latencies),
                "audio_summary": distribution(audio_latencies),
            }
        )
    output = {
        "supported": True,
        "endpoint": endpoint.name,
        "connection_mode": "keep_alive" if keep_alive else "connection_close",
        "repeats": repeats,
        "iterations_per_trial": iterations,
        "warmup_per_trial": warmup,
        "trials": trials,
        "aggregate": summarize_trials(trials),
    }
    output["aggregate"]["lookup_latency_ms"] = distribution(
        latency for trial in trials for latency in trial["lookup_latencies_ms"]
    )
    output["aggregate"]["audio_latency_ms"] = distribution(
        latency for trial in trials for latency in trial["audio_latencies_ms"]
    )
    all_audio_sizes = [size for trial in trials for size in trial["audio_body_sizes"]]
    all_audio_ttfb = [value for trial in trials for value in trial["audio_ttfb_ms"]]
    all_audio_complete = [
        value for trial in trials for value in trial["audio_latencies_ms"]
    ]
    output["aggregate"]["audio_size_classes"] = audio_size_class_summary(
        all_audio_sizes, all_audio_ttfb, all_audio_complete
    )
    return output


def benchmark_play(
    endpoint: Endpoint,
    work_cases: Sequence[dict[str, Any]],
    keep_alive: bool,
    repeats: int,
    iterations: int,
    warmup: int,
    timeout: float,
) -> dict[str, Any]:
    urls = [
        query_url(endpoint, case["params"], path="/v1/play")
        for case in work_cases
        if case.get("candidates")
    ]
    if not urls:
        return {"supported": False, "reason": "no hit cases for direct play"}
    trials: list[dict[str, Any]] = []
    for trial_number in range(repeats):
        latencies: list[float] = []
        headers_ms: list[float] = []
        ttfb_ms: list[float | None] = []
        body_sizes: list[int] = []
        protocols: list[str] = []
        errors: list[str] = []
        will_close = 0
        retries = 0
        new_connections = 0
        reused_connections = 0
        connection_new_flags: list[bool] = []
        with HttpSession(keep_alive, timeout=timeout) as session:
            for index in range(warmup):
                with contextlib.suppress(Exception):
                    session.request(urls[(index + trial_number) % len(urls)])
            wall_started = time.perf_counter_ns()
            for index in range(iterations):
                try:
                    url = urls[(index * 7 + trial_number) % len(urls)]
                    response = session.request(url)
                    latencies.append(response.elapsed_ms)
                    headers_ms.append(response.headers_ms)
                    ttfb_ms.append(response.first_body_byte_ms)
                    body_sizes.append(len(response.body))
                    protocols.append(response.http_version)
                    will_close += int(response.will_close)
                    retries += response.retry_count
                    new_connections += int(response.connection_was_new)
                    reused_connections += int(not response.connection_was_new)
                    connection_new_flags.append(response.connection_was_new)
                    if response.status != 200 or not response.content_type.startswith("audio/"):
                        errors.append(f"HTTP {response.status}, {response.content_type}")
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
            wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000.0
        trials.append(
            {
                "trial": trial_number + 1,
                "requests": iterations,
                "successes": iterations - len(errors),
                "errors": len(errors),
                "error_samples": errors[:10],
                "wall_ms": wall_ms,
                "throughput_rps": (iterations - len(errors)) / (wall_ms / 1000.0),
                "latencies_ms": latencies,
                "headers_ms": headers_ms,
                "ttfb_ms": [value for value in ttfb_ms if value is not None],
                "audio_ttfb_ms": ttfb_ms,
                "body_sizes": body_sizes,
                "protocols": protocols,
                "will_close_responses": will_close,
                "transport_retries": retries,
                "new_connection_responses": new_connections,
                "reused_connection_responses": reused_connections,
                "connection_new_flags": connection_new_flags,
                "summary": distribution(latencies),
            }
        )
    output = {
        "supported": True,
        "endpoint": endpoint.name,
        "connection_mode": "keep_alive" if keep_alive else "connection_close",
        "repeats": repeats,
        "iterations_per_trial": iterations,
        "warmup_per_trial": warmup,
        "hotset_urls": len(urls),
        "trials": trials,
        "aggregate": summarize_trials(trials),
    }
    output["aggregate"]["audio_size_classes"] = audio_size_class_summary(
        [size for trial in trials for size in trial["body_sizes"]],
        [value for trial in trials for value in trial["audio_ttfb_ms"]],
        [value for trial in trials for value in trial["latencies_ms"]],
    )
    return output


def benchmark_concurrency(
    endpoint: Endpoint,
    urls: Sequence[str],
    workload: str,
    keep_alive: bool,
    workers: int,
    requests: int,
    repeats: int,
    timeout: float,
) -> dict[str, Any]:
    if not urls:
        return {"supported": False, "reason": "no workload URLs"}
    trials: list[dict[str, Any]] = []
    for trial_number in range(repeats):
        local = threading.local()
        sessions: list[HttpSession] = []
        sessions_lock = threading.Lock()

        def task(
            index: int,
        ) -> tuple[float, int, str, bool, int, bool, float, float | None, str | None]:
            session = getattr(local, "session", None)
            if session is None:
                session = HttpSession(keep_alive, timeout=timeout)
                local.session = session
                with sessions_lock:
                    sessions.append(session)
            try:
                response = session.request(urls[(index * 13 + trial_number) % len(urls)])
                error = None
                if response.status != 200:
                    error = f"HTTP {response.status}"
                return (
                    response.elapsed_ms,
                    len(response.body),
                    response.http_version,
                    response.will_close,
                    response.retry_count,
                    response.connection_was_new,
                    response.headers_ms,
                    response.first_body_byte_ms,
                    error,
                )
            except Exception as exc:
                return 0.0, 0, "error", True, 0, True, 0.0, None, f"{type(exc).__name__}: {exc}"

        wall_started = time.perf_counter_ns()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            observations = list(executor.map(task, range(requests)))
        wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000.0
        for session in sessions:
            session.close()
        errors = [item[8] for item in observations if item[8] is not None]
        successful_observations = [item for item in observations if item[8] is None]
        latencies = [item[0] for item in successful_observations]
        body_sizes = [item[1] for item in successful_observations]
        trials.append(
            {
                "trial": trial_number + 1,
                "requests": requests,
                "successes": requests - len(errors),
                "errors": len(errors),
                "error_samples": errors[:10],
                "wall_ms": wall_ms,
                "throughput_rps": (requests - len(errors)) / (wall_ms / 1000.0),
                "latencies_ms": latencies,
                "headers_ms": [item[6] for item in successful_observations],
                "ttfb_ms": [item[7] for item in successful_observations if item[7] is not None],
                "audio_ttfb_ms": [item[7] for item in successful_observations],
                "body_sizes": body_sizes,
                "protocols": [item[2] for item in observations],
                "will_close_responses": sum(int(item[3]) for item in observations),
                "transport_retries": sum(item[4] for item in observations),
                "new_connection_responses": sum(int(item[5]) for item in observations),
                "reused_connection_responses": sum(int(not item[5]) for item in observations),
                "connection_new_flags": [bool(item[5]) for item in observations],
                "summary": distribution(latencies),
            }
        )
    output = {
        "supported": True,
        "endpoint": endpoint.name,
        "workload": workload,
        "connection_mode": "keep_alive" if keep_alive else "connection_close",
        "workers": workers,
        "requests_per_trial": requests,
        "repeats": repeats,
        "trials": trials,
        "aggregate": summarize_trials(trials),
    }
    if workload == "audio":
        output["aggregate"]["audio_size_classes"] = audio_size_class_summary(
            [size for trial in trials for size in trial["body_sizes"]],
            [value for trial in trials for value in trial["audio_ttfb_ms"]],
            [value for trial in trials for value in trial["latencies_ms"]],
        )
    return output


def port_accepts_connections(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def terminate_owned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Anki's venv python.exe is a launcher which owns the actual CPython
        # child. Kill only the tree rooted at the PID this harness created so a
        # startup trial cannot orphan a child listener.
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5.0)
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def benchmark_startup_spec(spec: Mapping[str, Any], default_repeats: int) -> dict[str, Any]:
    name = str(spec["name"])
    command = [str(item) for item in spec["command"]]
    cwd = str(spec.get("cwd")) if spec.get("cwd") else None
    base_url = str(spec["url"]).rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    ready_path = str(spec.get("ready_path", "/healthz"))
    repeats = int(spec.get("repeats", default_repeats))
    timeout_seconds = float(spec.get("timeout_seconds", 30.0))
    environment = os.environ.copy()
    environment.update({str(k): str(v) for k, v in spec.get("env", {}).items()})
    trials: list[dict[str, Any]] = []

    if port_accepts_connections(host, port):
        return {
            "name": name,
            "supported": False,
            "reason": f"startup target {host}:{port} is already listening; refusing to kill it",
            "command": command,
        }

    for trial_number in range(repeats):
        started_ns = time.perf_counter_ns()
        process: subprocess.Popen[Any] | None = None
        serving_pid: int | None = None
        tcp_ready_ms: float | None = None
        http_ready_ms: float | None = None
        error: str | None = None
        ready_response: dict[str, Any] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.perf_counter() + timeout_seconds
            while time.perf_counter() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"process exited early with code {process.returncode}")
                if tcp_ready_ms is None and port_accepts_connections(host, port, timeout=0.05):
                    tcp_ready_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                if tcp_ready_ms is not None:
                    try:
                        with HttpSession(False, timeout=0.5) as session:
                            response = session.request(base_url + ready_path)
                        if 200 <= response.status < 300:
                            http_ready_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                            ready_response = {
                                "status": response.status,
                                "body_preview": safe_decode(response.body[:200]),
                            }
                            break
                    except Exception:
                        pass
                time.sleep(0.005)
            if http_ready_ms is None:
                raise TimeoutError(f"did not become HTTP-ready within {timeout_seconds}s")
            serving_pid = listener_pid(port) or process.pid
            memory = windows_process_memory(serving_pid)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            serving_pid = listener_pid(port) if process else None
            memory = windows_process_memory(serving_pid or (process.pid if process else None))
        finally:
            if process is not None:
                terminate_owned_process(process)
            # Ensure the owned process released the port before the next trial.
            release_deadline = time.perf_counter() + 5.0
            while port_accepts_connections(host, port, timeout=0.05) and time.perf_counter() < release_deadline:
                time.sleep(0.01)
        trials.append(
            {
                "trial": trial_number + 1,
                "tcp_ready_ms": tcp_ready_ms,
                "http_ready_ms": http_ready_ms,
                "ready_response": ready_response,
                "process_memory_at_ready": memory,
                "launcher_pid": process.pid if process else None,
                "listener_pid": serving_pid,
                "error": error,
            }
        )
    successful = [item for item in trials if item["http_ready_ms"] is not None]
    return {
        "name": name,
        "supported": bool(successful),
        "command": command,
        "cwd": cwd,
        "url": base_url,
        "ready_path": ready_path,
        "trials": trials,
        "successful_trials": len(successful),
        "tcp_ready_ms": distribution(
            item["tcp_ready_ms"] for item in successful if item["tcp_ready_ms"] is not None
        ),
        "http_ready_ms": distribution(item["http_ready_ms"] for item in successful),
        "working_set_at_ready_bytes": distribution(
            item["process_memory_at_ready"]["working_set_bytes"]
            for item in successful
            if item.get("process_memory_at_ready")
            and item["process_memory_at_ready"].get("working_set_bytes") is not None
        ),
        "private_at_ready_bytes": distribution(
            item["process_memory_at_ready"]["private_bytes"]
            for item in successful
            if item.get("process_memory_at_ready")
            and item["process_memory_at_ready"].get("private_bytes") is not None
        ),
    }


def run_startup_specs(path: Path | None, default_repeats: int) -> list[dict[str, Any]]:
    if path is None:
        return []
    specs = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(specs, dict):
        specs = specs.get("servers", [])
    if not isinstance(specs, list):
        raise ValueError("startup spec must be an array or an object with a servers array")
    return [benchmark_startup_spec(spec, default_repeats) for spec in specs]


def fmt_ms(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def fmt_num(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}"


def fmt_bytes(value: Any) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024.0 or unit == "GiB":
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} GiB"


def observed_transport(record: Mapping[str, Any]) -> str:
    mode = record.get("connection_mode")
    aggregate = record.get("aggregate", {})
    responses = sum((aggregate.get("protocol_counts") or {}).values())
    closed = int(aggregate.get("will_close_responses", 0) or 0)
    new = int(aggregate.get("new_connection_responses", 0) or 0)
    reused = int(aggregate.get("reused_connection_responses", 0) or 0)
    if mode == "connection_close":
        return f"new {new}; reused {reused}"
    if not responses:
        return "no successful samples"
    if closed == 0:
        return f"new {new}; reused {reused}"
    if closed == responses:
        return f"server closed all; new {new}"
    return f"mixed close {closed}/{responses}; new {new}; reused {reused}"


def performance_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("category"),
        record.get("connection_mode"),
        record.get("workload"),
        record.get("workers"),
    )


def add_speedups(records: list[dict[str, Any]]) -> None:
    baseline = {
        performance_key(record): record
        for record in records
        if record.get("endpoint") == "original" and record.get("supported")
    }
    for record in records:
        if not record.get("supported") or record.get("endpoint") == "original":
            continue
        original = baseline.get(performance_key(record))
        if not original:
            continue
        old_latency = original.get("aggregate", {}).get("latency_ms", {})
        new_latency = record.get("aggregate", {}).get("latency_ms", {})
        old_throughput = original.get("aggregate", {}).get("trial_throughput_rps", {}).get("p50")
        new_throughput = record.get("aggregate", {}).get("trial_throughput_rps", {}).get("p50")
        speedup: dict[str, Any] = {}
        for percentile in ("p50", "p95", "p99"):
            old = old_latency.get(percentile)
            new = new_latency.get(percentile)
            if old is not None and new not in (None, 0):
                speedup[f"{percentile}_latency_x"] = old / new
        if old_throughput not in (None, 0) and new_throughput is not None:
            speedup["throughput_x"] = new_throughput / old_throughput
        record["speedup_vs_original"] = speedup

    two_stage_by_endpoint = {
        (record.get("endpoint"), record.get("connection_mode")): record
        for record in records
        if record.get("category") == "two_stage" and record.get("supported")
    }
    for record in records:
        if record.get("category") != "play" or not record.get("supported"):
            continue
        two_stage = two_stage_by_endpoint.get(
            (record.get("endpoint"), record.get("connection_mode"))
        )
        old = (
            two_stage.get("aggregate", {}).get("latency_ms", {}).get("p50")
            if two_stage
            else None
        )
        new = record.get("aggregate", {}).get("latency_ms", {}).get("p50")
        if old is not None and new not in (None, 0):
            record["speedup_vs_own_two_stage"] = old / new


def make_markdown(results: Mapping[str, Any]) -> str:
    lines = [
        "# Yomitan local-audio benchmark report",
        "",
        f"Run: `{results['run_id']}`  ",
        f"Captured: `{results['started_at']}` to `{results['finished_at']}`  ",
        f"Profile: `{results['config']['profile']}`  ",
        f"Overall: **{'PASS' if results.get('overall_pass') else 'FAIL'}**",
        "",
        "## Test system and data",
        "",
    ]
    machine = results["machine"]
    windows = machine.get("windows") or {}
    cpu = (windows.get("cpu") or {}).get("Name") if isinstance(windows, dict) else None
    os_info = windows.get("os") or {} if isinstance(windows, dict) else {}
    physical_disks = windows.get("physicalDisks") if isinstance(windows, dict) else None
    if isinstance(physical_disks, dict):
        physical_disks = [physical_disks]
    storage_text = ", ".join(
        f"{item.get('FriendlyName')} ({item.get('MediaType')}, {item.get('BusType')}, {fmt_bytes(item.get('Size'))})"
        for item in (physical_disks or [])
    )
    lines.extend(
        [
            f"- CPU: {cpu or machine.get('processor') or 'unknown'} ({machine.get('logical_cpu_count')} logical CPUs)",
            f"- OS: {os_info.get('Caption', machine.get('platform'))} build {os_info.get('BuildNumber', 'unknown')}",
            f"- RAM: {fmt_bytes((machine.get('physical_memory') or {}).get('total_bytes'))}",
            f"- Storage: {storage_text or 'unknown'}",
            f"- Python: `{machine.get('python', '').splitlines()[0]}`",
            f"- SQLite: `{results['database'].get('sqlite_version', 'unknown')}`",
            f"- Rust: `{machine.get('rustc') or 'unknown'}`",
            f"- Database: `{results['database']['path']}` ({fmt_bytes(results['database']['size_bytes'])}, {results['database']['rows']:,} rows)",
            f"- Audio collection copy: 382,509 files, 1.853 GiB logical (rounded copy-log total; an exact full-copy byte sum was not retained). Physical allocation is analyzed separately; a contemporaneous drive free-space delta was confounded by Rust toolchain and artifact creation and is not treated as a measurement.",
            "",
            "The benchmark corpus is deterministic and comes from the real `entries.db`; it includes hits from every populated source, multi-source ordering, source subsets, real Forvo user filters, omitted readings, the `expression=` alias, and misses.",
            "",
            "### Run design and sample counts",
            "",
            "| Setting | Value |",
            "|---|---:|",
            f"| Repeated sequential trials | {results['config'].get('repeats')} |",
            f"| Lookup operations per trial | {results['config'].get('lookup_iterations')} |",
            f"| Two-stage operations per trial | {results['config'].get('e2e_iterations')} (two HTTP requests each) |",
            f"| Direct-play operations per trial | {results['config'].get('play_iterations')} |",
            f"| Warmup operations per sequential trial | {results['config'].get('warmup')} (two-stage uses one quarter) |",
            f"| Concurrency requests per trial | {results['config'].get('concurrency_requests')} |",
            f"| Concurrency worker levels | {', '.join(str(v) for v in results['config'].get('workers', []))} |",
            f"| Concurrency repetitions | {min(3, int(results['config'].get('repeats', 0)))} |",
            "| Concurrency explicit warmups | 0 (runs after sequential warm workloads) |",
            f"| Broad real-query parity cases | {results['config'].get('parity_cases')} plus focused filter/compatibility cases |",
            f"| Maximum audio candidates SHA-256 checked per endpoint | {results['config'].get('max_audio_candidates')} |",
            "",
            "Percentiles use nearest-rank over all successful observations from the repeated trials. The CSV also retains each trial independently. No parametric confidence interval is claimed: repeated-trial spread, p95/p99, min/max, and standard deviation are provided in JSON/CSV instead.",
            "",
            "## Endpoint status and memory",
            "",
            "| Endpoint | URL | Available | Process / PID | Working set before/after | Private before/after |",
            "|---|---|---:|---|---:|---:|",
        ]
    )
    for name, endpoint in results["endpoints"].items():
        before = (endpoint.get("memory_before") or {}).get("working_set_bytes")
        after = (endpoint.get("memory_after") or {}).get("working_set_bytes")
        private_before = (endpoint.get("memory_before") or {}).get("private_bytes")
        private_after = (endpoint.get("memory_after") or {}).get("private_bytes")
        process = endpoint.get("process") or {}
        process_label = f"{process.get('Name', 'unknown')} / {endpoint.get('pid') or '—'}"
        if endpoint.get("pid_after") and endpoint.get("pid_after") != endpoint.get("pid"):
            process_label += f" → {endpoint.get('pid_after')}"
        lines.append(
            f"| {name} | `{endpoint['base_url']}` | {'yes' if endpoint.get('available') else 'no'} | "
            f"{process_label} | "
            f"{fmt_bytes(before)}/{fmt_bytes(after)} | {fmt_bytes(private_before)}/{fmt_bytes(private_after)} |"
        )
    lines.extend(
        [
            "",
            "The original add-on shares Anki's Python process, so its working set is not an isolated server allocation. The optimized add-on is benchmarked through its separate standalone runner on port 5051 (isolated here, but shared with Anki when installed). Rust is isolated when it owns its listener.",
            "",
            "## Correctness and parity",
            "",
            "| Endpoint | Cases passed | Audio candidates byte-checked | Overall |",
            "|---|---:|---:|---:|",
        ]
    )
    for name, correctness in results.get("correctness", {}).items():
        lines.append(
            f"| {name} | {correctness.get('cases_passed', 0)}/{correctness.get('cases_total', 0)} | "
            f"{correctness.get('audio_candidates_checked', 0)} | {'PASS' if correctness.get('pass') else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "A case passes only when HTTP/schema behavior, candidate count, exact candidate name/order, dedicated candidate source/order and the full `audioId/source/speaker/reading/name/mime/url` item contract (for new servers), and SHA-256 audio bytes match the live original. Dedicated and compatibility URL order must agree and use numeric loopback. Legacy audio bytes are additionally checked against the copied on-disk file whenever the legacy URL exposes source and relative path.",
        ]
    )
    for name, correctness in results.get("correctness", {}).items():
        failed_cases = [case for case in correctness.get("cases", []) if not case.get("pass")]
        feature_failures = correctness.get("required_feature_failures", [])
        if failed_cases or feature_failures:
            lines.extend(["", f"Failures for `{name}`:"])
            if feature_failures:
                lines.append(f"- Required feature failures: {', '.join(feature_failures)}")
            for case in failed_cases[:20]:
                reasons = []
                if case.get("schema_error"):
                    reasons.append(str(case["schema_error"]))
                if not case.get("candidate_count_match", True):
                    reasons.append("candidate count")
                if not case.get("candidate_name_order_match", True):
                    reasons.append("candidate name/order")
                if case.get("dedicated_candidates_shape") is False:
                    reasons.append("dedicated candidate schema")
                if case.get("dedicated_candidate_count_match") is False:
                    reasons.append("dedicated candidate count")
                if case.get("dedicated_candidate_name_order_match") is False:
                    reasons.append("dedicated candidate name/order")
                if case.get("dedicated_candidate_source_order_match") is False:
                    reasons.append("dedicated candidate source/order")
                if case.get("dedicated_candidate_item_shape") is False:
                    reasons.append("dedicated rich item schema")
                if case.get("dedicated_candidate_url_order_match") is False:
                    reasons.append("dedicated/legacy URL order")
                if case.get("dedicated_candidate_numeric_hosts") is False:
                    reasons.append("dedicated non-numeric URL host")
                if case.get("audio_mismatches"):
                    reasons.append(f"{len(case['audio_mismatches'])} audio byte/MIME mismatches")
                if case.get("direct_source_mismatches"):
                    reasons.append(f"{len(case['direct_source_mismatches'])} source mismatches")
                if case.get("url_origin_mismatches"):
                    reasons.append(f"{case['url_origin_mismatches']} URL-origin mismatches")
                if case.get("url_host_policy_mismatches"):
                    reasons.append(f"{case['url_host_policy_mismatches']} non-numeric URL hosts")
                if case.get("error"):
                    reasons.append(str(case["error"]))
                lines.append(f"- `{case.get('case_id')}`: {', '.join(reasons) or 'unspecified mismatch'}")
            if len(failed_cases) > 20:
                lines.append(f"- …and {len(failed_cases) - 20} more; see correctness CSV/JSON.")
    duplication = results.get("duplication_analysis")
    if duplication:
        aliases = duplication.get("database_aliases", {})
        hashes = duplication.get("audio_hashes", {})
        lines.extend(
            [
                "",
                "## Duplication and pack savings",
                "",
                "| Category | Amount | Can remove mappings? | Pack/storage effect |",
                "|---|---:|---:|---:|",
                f"| Mapping aliases (extra DB rows sharing one source/path) | {aliases.get('path_alias_extra_mapping_rows', 0):,} | no | no repeated audio file |",
                f"| Exact duplicate mapping rows | {aliases.get('exact_duplicate_mapping_extra_rows', 0):,} | no, for strict count/order parity | no repeated audio file |",
                f"| Repeated relative path strings across source roots | {aliases.get('relative_path_strings_used_by_multiple_sources', 0):,} | no | dedup only after SHA-256 match |",
                f"| Byte-identical files at distinct source/paths | {hashes.get('extra_files_in_duplicate_groups', 0):,} | no | {fmt_bytes(hashes.get('byte_dedup_logical_savings_bytes'))} logical saving |",
                "",
                f"Referenced audio is {fmt_bytes(hashes.get('logical_audio_bytes'))} logical. A unique-payload pack is {fmt_bytes(hashes.get('unique_payload_bytes'))}; estimated counterfactual dedup plus per-file cluster-slack saving is {fmt_bytes(hashes.get('combined_byte_dedup_plus_small_file_allocation_savings_bytes'))}. This is a 4 KiB-cluster model, not direct NTFS accounting. The lab retains loose files for drop-in compatibility, so it has not realized that removal saving; the add-on and Rust pack paths are hardlinks and therefore avoid a second physical pack payload allocation. See `duplication.md` for definitions and caveats.",
            ]
        )

    architecture = results.get("architecture_variants")
    if architecture:
        lines.extend(
            [
                "",
                "## Lookup architecture experiments",
                "",
                f"Component figures below are implementation microbenchmarks, distinct from the end-to-end HTTP measurements that follow. Python lookup variants each used {architecture.get('python_lookup_component_operations_per_mode', 0):,} mixed serialized operations; native counts are disclosed below.",
                "",
                "| Runtime | Index / lookup | Serialized lookup p50/p95/p99 | Hot cache | Mode setup | Incremental WS | Decision |",
                "|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for item in architecture.get("variants", []):
            lines.append(
                f"| {item.get('runtime', '—')} | {item.get('index', '—')} | "
                f"{fmt_num(item.get('serialized_lookup_us'), 1)}/{fmt_num(item.get('p95_lookup_us'), 1)}/{fmt_num(item.get('p99_lookup_us'), 1)} µs | "
                f"{fmt_num(item.get('cache_hit_us'), 1)} µs | "
                f"{fmt_num(item.get('startup_ms'), 1)} ms | "
                f"{fmt_num(item.get('incremental_working_set_mb'), 2)} MiB | "
                f"{item.get('decision', '—')} |"
            )
        audio_component = architecture.get("audio_component", {})
        if audio_component:
            lines.extend(
                [
                    "",
                    f"Add-on audio component ({audio_component.get('sample_files', 0):,} real files, p50/p95): individual open/read {fmt_num(audio_component.get('individual_file_open_read_us'), 1)}/{fmt_num(audio_component.get('individual_file_open_read_p95_us'), 1)} µs vs mmap pack view {fmt_num(audio_component.get('mmap_pack_view_us'), 1)}/{fmt_num(audio_component.get('mmap_pack_view_p95_us'), 1)} µs; keep-alive HTTP {fmt_num(audio_component.get('http_individual_files_keep_alive_us'), 1)}/{fmt_num(audio_component.get('http_individual_files_keep_alive_p95_us'), 1)} µs vs {fmt_num(audio_component.get('http_mmap_pack_keep_alive_us'), 1)}/{fmt_num(audio_component.get('http_mmap_pack_keep_alive_p95_us'), 1)} µs.",
                ]
            )
        native_component = architecture.get("native_component", {})
        if native_component:
            lines.extend(
                [
                    "",
                    f"Native shared bundle/index validation-open was {fmt_num(native_component.get('bundle_index_open_ms'), 3)} ms before per-mode setup. "
                    f"On {native_component.get('audio_sample_files', 0):,} warm real assets ({fmt_bytes(native_component.get('audio_sample_bytes'))}), the exact HTTP zero-copy mmap owner was "
                    f"{fmt_num(native_component.get('mmap_pack_zero_copy_median_us'), 1)}/{fmt_num(native_component.get('mmap_pack_zero_copy_p95_us'), 1)} µs p50/p95 versus "
                    f"{fmt_num(native_component.get('individual_file_open_read_median_us'), 1)}/{fmt_num(native_component.get('individual_file_open_read_p95_us'), 1)} µs for individual open/read; output checksums matched. "
                    "This native test touches and hashes each payload, whereas the add-on's 1.2 µs figure is pack-slice-view creation only; they are intentionally not compared across runtimes. "
                    f"Sorted, MPH, and preload serialized bodies were byte-identical. A separate {native_component.get('sqlite_parity_queries', 0):,}-query SQLite-oracle differential run over ordered `(name, source, speaker, reading)` tuples {'passed' if native_component.get('sqlite_parity_pass') else 'FAILED'}; "
                    "its whole-body checksum differs by design because SQLite emits legacy row-ID URLs while compiled modes emit bundle-audio-ID URLs. "
                    f"For bundle construction, eight-worker read+hash prefetch processed {fmt_num(native_component.get('compiler_prefetch_eight_worker_files_per_second'), 0)} files/s versus "
                    f"{fmt_num(native_component.get('compiler_prefetch_one_worker_files_per_second'), 0)} with one worker ({fmt_num(native_component.get('compiler_prefetch_eight_vs_one_speedup'), 2)}x).",
                ]
            )
        native_http = architecture.get("native_http_architecture", {})
        if native_http:
            lines.extend(
                [
                    "",
                    "### Native architecture HTTP matrix",
                    "",
                    f"Each mode used {native_http.get('repeats', 0)} fresh-process trials with the same {native_http.get('corpus_queries', 0):,}-query corpus, response cache disabled: "
                    f"{native_http.get('mixed_requests_per_run', 0):,} mixed lookups, {native_http.get('hot_requests_per_run', 0):,} hot lookups, "
                    f"{native_http.get('concurrent_requests_per_run', 0):,} requests at {native_http.get('concurrency', 0)} workers, and {native_http.get('audio_requests_per_run', 0):,} audio GETs per trial. "
                    "Rows are medians of the three trial-level values, not pooled requests.",
                    "",
                    "| Mode | startup ms | WS/private at ready | mixed lookup p50/p95 | hot lookup p50 | 32-worker req/s | audio p50/p95 | Decision |",
                    "|---|---:|---:|---:|---:|---:|---:|---|",
                ]
            )
            for item in native_http.get("trial_median_rows", []):
                lines.append(
                    f"| {item.get('name', '—')} | {fmt_num(item.get('startup_ms'), 3)} | "
                    f"{fmt_bytes(item.get('startup_working_set_bytes'))}/{fmt_bytes(item.get('startup_private_bytes'))} | "
                    f"{fmt_num(item.get('mixed_lookup_p50_us'), 1)}/{fmt_num(item.get('mixed_lookup_p95_us'), 1)} µs | "
                    f"{fmt_num(item.get('hot_lookup_p50_us'), 1)} µs | {fmt_num(item.get('concurrency_rps'), 0)} | "
                    f"{fmt_num(item.get('audio_p50_us'), 1)}/{fmt_num(item.get('audio_p95_us'), 1)} µs | {item.get('decision', '—')} |"
                )
            lines.extend(
                [
                    "",
                    native_http.get("selection_rationale", ""),
                    "",
                    native_http.get("qualification", ""),
                ]
            )
        selected_assets = architecture.get("selected_addon_asset_path", {})
        if selected_assets:
            lines.extend(
                [
                    "",
                    "Selected add-on asset path: retained read-only SQLite preserves source/user filtering and candidate order as DB row IDs; "
                    f"each row ID then resolves through a {selected_assets.get('index_record_bytes', 0)}-byte fixed mmap record into the lazily mapped "
                    f"{fmt_bytes(selected_assets.get('pack_bytes'))} pack. The {fmt_bytes(selected_assets.get('row_id_index_file_bytes'))} index "
                    f"({selected_assets.get('row_id_record_count', 0):,} × {selected_assets.get('index_record_bytes', 0)}-byte records = {fmt_bytes(selected_assets.get('row_id_records_bytes'))}, "
                    f"plus a {fmt_bytes(selected_assets.get('index_header_bytes'))} header) and pack are not bulk-loaded: "
                    f"the isolated live process was {fmt_num(selected_assets.get('live_process_before_requests_working_set_mib'), 2)} MiB working set / "
                    f"{fmt_num(selected_assets.get('live_process_before_requests_private_mib'), 2)} MiB private before requests. One-time Rust-bundle import took "
                    f"{fmt_num(selected_assets.get('one_time_rust_bundle_import_seconds'), 3)} s; hardened SHA-256 reimport took {fmt_num(selected_assets.get('hardened_verified_reimport_seconds'), 3)} s "
                    f"({fmt_num(selected_assets.get('hardened_verified_reimport_wall_seconds'), 3)} s wall); a full lookup/index + pack BLAKE3 verification took "
                    f"{fmt_num(selected_assets.get('full_lookup_and_pack_blake3_verify_seconds'), 3)} s. The add-on pack is an NTFS hardlink to the Rust pack, so these two implementations share one physical pack allocation.",
                ]
            )
        addon_verification = architecture.get("addon_verification", {})
        if addon_verification:
            lines.extend(
                [
                    "",
                    "### Add-on regression and lifecycle audit",
                    "",
                    f"The bounded suite passed {addon_verification.get('tests_passed', 0)}/{addon_verification.get('tests_total', 0)} in "
                    f"{fmt_num(addon_verification.get('elapsed_seconds'), 3)} s under {addon_verification.get('python', '—')} with `ResourceWarning` promoted to an error. "
                    "It deterministically reproduced the old stale-publication race, then verified that the epoch/lease fix waits for the active DB reader and that the next lookup returns new data. "
                    f"Two packed leases overlapped; reload completed while an old lease remained open, that immutable old version still returned exact `{addon_verification.get('old_immutable_version_bytes', '—')}` bytes, and all holder/test threads completed. "
                    f"A {addon_verification.get('packed_soak_clients', 0)}-client packed soak completed {addon_verification.get('packed_soak_requests', 0):,} requests with "
                    f"{addon_verification.get('packed_soak_errors', 0)} errors and {addon_verification.get('packed_soak_reconnects', 0)} reconnects; the connection pool peaked at "
                    f"{addon_verification.get('packed_soak_pool_peak_connections', 0)} and ended with {addon_verification.get('packed_soak_pool_idle_at_end', 0)} idle. "
                    f"The soak ran {fmt_num(addon_verification.get('packed_soak_elapsed_seconds'), 3)} s; lookup p50/p95/p99 was "
                    f"{fmt_ms(addon_verification.get('packed_soak_lookup_p50_ms'))}/{fmt_ms(addon_verification.get('packed_soak_lookup_p95_ms'))}/{fmt_ms(addon_verification.get('packed_soak_lookup_p99_ms'))} ms and audio/protocol was "
                    f"{fmt_ms(addon_verification.get('packed_soak_audio_p50_ms'))}/{fmt_ms(addon_verification.get('packed_soak_audio_p95_ms'))}/{fmt_ms(addon_verification.get('packed_soak_audio_p99_ms'))} ms. "
                    f"Working set moved {fmt_bytes(addon_verification.get('packed_soak_working_set_before_bytes'))} → {fmt_bytes(addon_verification.get('packed_soak_working_set_after_bytes'))}; private "
                    f"{fmt_bytes(addon_verification.get('packed_soak_private_before_bytes'))} → {fmt_bytes(addon_verification.get('packed_soak_private_after_bytes'))}. "
                    f"Coverage: {addon_verification.get('coverage', '—')}",
                ]
            )
        exhaustive = architecture.get("addon_exhaustive_parity", {})
        if exhaustive:
            lines.extend(
                [
                    "",
                    "### Exhaustive add-on compatibility audit",
                    "",
                    f"An independent legacy SQL/reference implementation compared {exhaustive.get('total_cases', 0):,} real cases in {fmt_num(exhaustive.get('elapsed_seconds'), 3)} s with "
                    f"{exhaustive.get('mismatches', 0)} mismatches. SQLite `quick_check` was `{exhaustive.get('sqlite_quick_check', '—')}`; the cache-disabled audit ended with "
                    f"{exhaustive.get('idle_read_only_connections_at_end', 0)} idle read-only connection and {exhaustive.get('cache_growth', 0)} cache growth.",
                    "",
                    "| Matrix | cases | returned rows | ordered SHA-256 |",
                    "|---|---:|---:|---|",
                ]
            )
            for item in exhaustive.get("matrices", []):
                lines.append(
                    f"| {item.get('name', '—')} | {item.get('cases', 0):,} | {item.get('returned_rows', 0):,} | `{item.get('ordered_sha256', '—')}` |"
                )
        addon_http = architecture.get("addon_http_architecture", {})
        if addon_http:
            lines.extend(
                [
                    "",
                    "### Add-on HTTP server architecture search",
                    "",
                    "| Path | cached keep-alive | unique keep-alive | new connection cached | Decision |",
                    "|---|---:|---:|---:|---|",
                ]
            )
            for item in addon_http.get("rows", []):
                lines.append(
                    f"| {item.get('name', '—')} | {fmt_num(item.get('cached_keep_alive_us'), 1)} µs | "
                    f"{fmt_num(item.get('unique_keep_alive_us'), 1)} µs | {fmt_num(item.get('new_connection_cached_us'), 1)} µs | {item.get('decision', '—')} |"
                )
        regeneration = architecture.get("desktop_database_regeneration", {})
        if regeneration:
            lines.extend(
                [
                    "",
                    "### Desktop database regeneration experiment",
                    "",
                    f"Representative {regeneration.get('fixture_rows', 0):,}-row in-memory fixture; {regeneration.get('trials_per_mode', 0)} trials per design.",
                    "",
                    "| Insert/index plan | median | rows/s | speedup |",
                    "|---|---:|---:|---:|",
                ]
            )
            for item in regeneration.get("rows", []):
                lines.append(
                    f"| {item.get('name', '—')} | {fmt_num(item.get('median_seconds'), 4)} s | {item.get('rows_per_second', 0):,} | {fmt_num(item.get('speedup_x'), 2)}x{' selected' if item.get('decision') == 'selected' else ''} |"
                )
        bundle_compile = architecture.get("native_bundle_compile", {})
        if bundle_compile:
            lines.extend(
                [
                    "",
                    f"Native bundle compile: {bundle_compile.get('mapping_rows', 0):,} mappings / {bundle_compile.get('unique_source_paths', 0):,} source paths, {bundle_compile.get('additional_repeated_path_alias_references', 0):,} alias references retained, {bundle_compile.get('content_duplicate_assets', 0):,} cross-path exact-byte duplicates, {fmt_bytes(bundle_compile.get('pack_bytes'))} ({bundle_compile.get('pack_bytes', 0):,} B exact) pack, {fmt_num(bundle_compile.get('total_compile_seconds'), 3)} s total ({fmt_num(bundle_compile.get('pack_stage_seconds'), 3)} s pack stage).",
                ]
            )
        native_binary = architecture.get("native_binary", {})
        if native_binary:
            lines.extend(
                [
                    "",
                    f"Final portable native executable: {fmt_bytes(native_binary.get('bytes'))}, SHA-256 `{native_binary.get('sha256', '—')}`; "
                    f"release tests {native_binary.get('release_tests_passed', 0)}/{native_binary.get('release_tests_total', 0)} passed. "
                    f"Rich candidate contract: `{native_binary.get('candidate_contract', '—')}`.",
                ]
            )
        lines.extend(
            [
                "",
                "Sorted mmap and minimal-perfect-hash (MPH) designs both map a `(term, reading)` key to an immutable postings slice while retaining every source/name/path occurrence. Sorted mmap offers simple binary search and nearly zero startup; MPH offers O(1) key lookup but still needs postings metadata for source/user filtering and exact order. The selected native mode is determined by the separately measured component and end-to-end results below, not by asymptotic complexity alone.",
            ]
        )
    lines.extend(
        [
            "",
            "## Warm steady-state latency",
            "",
            "All latency values are milliseconds. TTFB runs from request start through the first body byte; full latency includes the complete response read. For `two_stage`, the TTFB column is the audio subrequest while full latency is the combined lookup+audio operation; its audio-only completion is broken out in the size table. Throughput is the median of repeated trial operation rates: a two-stage operation contains two HTTP requests, while every other row's operation is one request. `lookup_hotset` and direct `play` cycle a bounded interactive real-hit set and permit response-cache hits; `lookup_unique_real_hit` consumes DB-backed hits exactly once, disjoint across transports and from oracle/parity materialization (warm DB/filesystem, cold response cache). `x old` is speedup versus the original for the same workload/transport; the final column compares direct play with that endpoint's own two-request path.",
            "",
            "| Endpoint | Workload | Requested transport | Observed transport | Workers | n | TTFB p50 | Full p50 | Full p95 | Full p99 | mean body | ops/s | p50 x old | ops/s x old | play x own 2-stage |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for record in results.get("benchmarks", []):
        if not record.get("supported"):
            continue
        aggregate = record["aggregate"]
        latency = aggregate["latency_ms"]
        throughput = aggregate["trial_throughput_rps"].get("p50")
        speedup = record.get("speedup_vs_original", {}).get("p50_latency_x")
        throughput_speedup = record.get("speedup_vs_original", {}).get("throughput_x")
        mean_body = aggregate.get("response_body_bytes", {}).get("mean")
        ttfb_p50 = aggregate.get("ttfb_ms", {}).get("p50")
        workload = record.get("workload") or record.get("category")
        lines.append(
            f"| {record['endpoint']} | {workload} | {record.get('connection_mode', '—')} | "
            f"{observed_transport(record)} | {record.get('workers', '—')} | {latency.get('n', 0)} | {fmt_ms(ttfb_p50)} | {fmt_ms(latency.get('p50'))} | "
            f"{fmt_ms(latency.get('p95'))} | {fmt_ms(latency.get('p99'))} | {fmt_bytes(mean_body)} | {fmt_num(throughput)} | "
            f"{fmt_num(speedup, 2)} | {fmt_num(throughput_speedup, 2)} | {fmt_num(record.get('speedup_vs_own_two_stage'), 2)} |"
        )

    size_rows = []
    for record in results.get("benchmarks", []):
        classes = record.get("aggregate", {}).get("audio_size_classes", {})
        for class_name, metrics in classes.items():
            size_rows.append((record, class_name, metrics))
    if size_rows:
        lines.extend(
            [
                "",
                "### Audio time-to-first-byte and completion by response size",
                "",
                "TTFB is measured after the request begins through the first body byte; complete includes the entire body read and is the timing used for SHA-256 correctness. HEAD/empty responses have no first-body-byte observation.",
                "",
                "| Endpoint | Workload | Transport | Workers | Size class | n | mean bytes | TTFB p50/p95 | complete p50/p95 |",
                "|---|---|---|---:|---|---:|---:|---:|---:|",
            ]
        )
        for record, class_name, metrics in size_rows:
            workload = record.get("workload") or record.get("category")
            ttfb = metrics.get("ttfb_ms", {})
            complete = metrics.get("complete_ms", {})
            body = metrics.get("body_bytes", {})
            lines.append(
                f"| {record.get('endpoint')} | {workload} | {record.get('connection_mode')} | "
                f"{record.get('workers', '—')} | {class_name} | {metrics.get('n', 0)} | "
                f"{fmt_bytes(body.get('mean'))} | {fmt_ms(ttfb.get('p50'))}/{fmt_ms(ttfb.get('p95'))} | "
                f"{fmt_ms(complete.get('p50'))}/{fmt_ms(complete.get('p95'))} |"
            )

    lines.extend(
        [
            "",
            "## First measured post-setup touch and optional HTTP features",
            "",
            "| Endpoint | First lookup full ms | First audio TTFB ms | First audio full ms | `/healthz` | `/v1/info` | CORS/OPTIONS | `/v1/candidates` | `/v1/play` | Stable URLs | ETag/304 | Audio HEAD/Range/Range-HEAD | Play HEAD/Range |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in results["endpoints"]:
        first = results.get("first_touch", {}).get(name, {})
        features = results.get("features", {}).get(name, {})
        support = lambda key: "yes" if features.get(key, {}).get("supported") else "no"
        lines.append(
            f"| {name} | {fmt_ms((first.get('lookup') or {}).get('latency_ms'))} | "
            f"{fmt_ms((first.get('audio') or {}).get('ttfb_ms'))} | "
            f"{fmt_ms((first.get('audio') or {}).get('latency_ms'))} | {support('healthz')} | "
            f"{support('info')} | {support('cors')}/{support('options')} | {support('v1_candidates')} | {support('v1_play')} | "
            f"{support('stable_urls')} | {support('etag')} | {support('head')}/{support('range')}/{support('range_head')} | {support('v1_play_head')}/{support('v1_play_range')} |"
        )

    legacy_host = results.get("legacy_returned_hostname_probe")
    if legacy_host:
        raw = legacy_host["variants"]["as_returned_localhost"]["latency_ms"]
        normalized = legacy_host["variants"]["normalized_127_0_0_1"]["latency_ms"]
        raw_ttfb = legacy_host["variants"]["as_returned_localhost"].get("ttfb_ms", {})
        normalized_ttfb = legacy_host["variants"]["normalized_127_0_0_1"].get(
            "ttfb_ms", {}
        )
        lines.extend(
            [
                "",
                "### Legacy `localhost` URL penalty (excluded from core comparisons)",
                "",
                "| URL exactly as returned | n | raw TTFB/full p50 | raw full p95 | numeric TTFB/full p50 | Added full p50 | Slowdown |",
                "|---|---:|---:|---:|---:|---:|---:|",
                f"| `{legacy_host['returned_url']}` | {raw.get('n', 0)} | {fmt_ms(raw_ttfb.get('p50'))}/{fmt_ms(raw.get('p50'))} ms | "
                f"{fmt_ms(raw.get('p95'))} ms | {fmt_ms(normalized_ttfb.get('p50'))}/{fmt_ms(normalized.get('p50'))} ms | "
                f"{fmt_ms(legacy_host.get('p50_penalty_ms'))} ms | {fmt_num(legacy_host.get('p50_slowdown_x'), 1)}x |",
                "",
                f"The legacy response hard-codes `localhost`. On this client, Windows first attempts the unbound IPv6 loopback route and falls back after roughly two seconds. `getaddrinfo` p50 in this run was {fmt_ms(legacy_host.get('getaddrinfo_latency_ms', {}).get('p50'))} ms, supporting connect/fallback—not DNS lookup—as the cause. Browser/curl Happy Eyeballs behavior may differ. Core server comparisons rebase equivalent loopback URLs to `127.0.0.1`, and new implementations are required to return numeric-loopback URLs directly.",
            ]
        )

    startups = results.get("startup", [])
    if startups:
        lines.extend(
            [
                "",
                "## Cold process startup-to-ready",
                "",
                "| Server | Successful trials | TCP ready p50 ms | HTTP ready p50/p95 ms | WS at ready p50 | Private at ready p50 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for startup in startups:
            lines.append(
                f"| {startup['name']} | {startup.get('successful_trials', 0)}/{len(startup.get('trials', []))} | "
                f"{fmt_ms(startup.get('tcp_ready_ms', {}).get('p50'))} | "
                f"{fmt_ms(startup.get('http_ready_ms', {}).get('p50'))}/{fmt_ms(startup.get('http_ready_ms', {}).get('p95'))} | "
                f"{fmt_bytes(startup.get('working_set_at_ready_bytes', {}).get('p50'))} | "
                f"{fmt_bytes(startup.get('private_at_ready_bytes', {}).get('p50'))} |"
            )

    lines.extend(
        [
            "",
            "## Interpretation caveats",
            "",
            "- Warm results deliberately follow corpus construction and warmups; they measure the interactive steady state users normally experience after Anki/server startup.",
            "- “First measured post-setup touch” follows corpus construction and original-oracle queries; servers may also predate the run. Windows standby cache was not flushed, so it is neither process-cold nor a physical-media cold test.",
            "- Startup trials launch a fresh owned process only when a startup spec uses an unused port. They include process creation through successful readiness HTTP, but the OS file cache can remain warm between trials.",
            "- Connection-close creates a fresh TCP connection for every HTTP request. Keep-alive reuses one connection per sequential client/thread when the server supports it. `will_close_responses` in JSON proves whether reuse was actually possible; the original Python HTTP/1.0 server normally closes every response.",
            "- Loopback results exclude browser decoding, extension scheduling, speaker output, and antivirus variability. Background Anki/GSM work and CPU power state can still add jitter; use p50 for typical latency and p95/p99 for tail behavior.",
            "- Full audio concurrency measures warm filesystem delivery of a small deterministic set. It does not model hundreds of clients or network transfer; the target is one desktop user with bursty Yomitan requests.",
            "",
            "Machine-readable per-request observations and every correctness mismatch are in the adjacent JSON; aggregate metrics and trial rows are in CSV files.",
            "",
        ]
    )
    return "\n".join(lines)


def benchmark_csv_rows(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if not record.get("supported"):
            rows.append(
                {
                    "endpoint": record.get("endpoint"),
                    "category": record.get("category"),
                    "workload": record.get("workload"),
                    "connection_mode": record.get("connection_mode"),
                    "workers": record.get("workers"),
                    "row_type": "unsupported",
                    "error": record.get("reason"),
                }
            )
            continue
        for trial in record.get("trials", []):
            summary = trial.get("summary", {})
            headers_summary = distribution(trial.get("headers_ms", []))
            ttfb_summary = distribution(trial.get("ttfb_ms", []))
            rows.append(
                {
                    "endpoint": record.get("endpoint"),
                    "category": record.get("category"),
                    "workload": record.get("workload"),
                    "connection_mode": record.get("connection_mode"),
                    "workers": record.get("workers"),
                    "row_type": "trial",
                    "trial": trial.get("trial"),
                    "n": summary.get("n"),
                    "latency_min_ms": summary.get("min"),
                    "latency_mean_ms": summary.get("mean"),
                    "latency_p50_ms": summary.get("p50"),
                    "latency_p95_ms": summary.get("p95"),
                    "latency_p99_ms": summary.get("p99"),
                    "latency_max_ms": summary.get("max"),
                    "headers_p50_ms": headers_summary.get("p50"),
                    "ttfb_p50_ms": ttfb_summary.get("p50"),
                    "ttfb_p95_ms": ttfb_summary.get("p95"),
                    "throughput_rps": trial.get("throughput_rps"),
                    "successes": trial.get("successes"),
                    "errors": trial.get("errors"),
                    "will_close_responses": trial.get("will_close_responses"),
                    "transport_retries": trial.get("transport_retries"),
                    "new_connection_responses": trial.get("new_connection_responses"),
                    "reused_connection_responses": trial.get("reused_connection_responses"),
                    "response_body_mean_bytes": statistics.fmean(trial["body_sizes"])
                    if trial.get("body_sizes")
                    else None,
                }
            )
        aggregate = record.get("aggregate", {})
        latency = aggregate.get("latency_ms", {})
        throughput = aggregate.get("trial_throughput_rps", {})
        rows.append(
            {
                "endpoint": record.get("endpoint"),
                "category": record.get("category"),
                "workload": record.get("workload"),
                "connection_mode": record.get("connection_mode"),
                "workers": record.get("workers"),
                "row_type": "aggregate",
                "trial": "all",
                "n": latency.get("n"),
                "latency_min_ms": latency.get("min"),
                "latency_mean_ms": latency.get("mean"),
                "latency_p50_ms": latency.get("p50"),
                "latency_p95_ms": latency.get("p95"),
                "latency_p99_ms": latency.get("p99"),
                "latency_max_ms": latency.get("max"),
                "headers_p50_ms": aggregate.get("headers_ms", {}).get("p50"),
                "ttfb_p50_ms": aggregate.get("ttfb_ms", {}).get("p50"),
                "ttfb_p95_ms": aggregate.get("ttfb_ms", {}).get("p95"),
                "throughput_rps": throughput.get("p50"),
                "successes": aggregate.get("successes"),
                "errors": aggregate.get("errors"),
                "will_close_responses": aggregate.get("will_close_responses"),
                "transport_retries": aggregate.get("transport_retries"),
                "new_connection_responses": aggregate.get("new_connection_responses"),
                "reused_connection_responses": aggregate.get("reused_connection_responses"),
                "response_body_mean_bytes": aggregate.get("response_body_bytes", {}).get("mean"),
                "p50_latency_speedup_vs_original": record.get("speedup_vs_original", {}).get(
                    "p50_latency_x"
                ),
                "p95_latency_speedup_vs_original": record.get("speedup_vs_original", {}).get(
                    "p95_latency_x"
                ),
                "throughput_speedup_vs_original": record.get("speedup_vs_original", {}).get(
                    "throughput_x"
                ),
                "play_speedup_vs_own_two_stage": record.get("speedup_vs_own_two_stage"),
            }
        )
    return rows


def correctness_csv_rows(correctness: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for endpoint, result in correctness.items():
        for case in result.get("cases", []):
            rows.append(
                {
                    "endpoint": endpoint,
                    "case_id": case.get("case_id"),
                    "category": case.get("category"),
                    "pass": case.get("pass"),
                    "status": case.get("status"),
                    "schema_error": case.get("schema_error"),
                    "candidate_count": case.get("candidate_count"),
                    "expected_candidate_count": case.get("expected_candidate_count"),
                    "candidate_count_match": case.get("candidate_count_match"),
                    "candidate_name_order_match": case.get("candidate_name_order_match"),
                    "dedicated_candidates_shape": case.get("dedicated_candidates_shape"),
                    "dedicated_candidate_count_match": case.get(
                        "dedicated_candidate_count_match"
                    ),
                    "dedicated_candidate_name_order_match": case.get(
                        "dedicated_candidate_name_order_match"
                    ),
                    "dedicated_candidate_source_order_match": case.get(
                        "dedicated_candidate_source_order_match"
                    ),
                    "dedicated_candidate_item_shape": case.get(
                        "dedicated_candidate_item_shape"
                    ),
                    "dedicated_candidate_url_order_match": case.get(
                        "dedicated_candidate_url_order_match"
                    ),
                    "dedicated_candidate_numeric_hosts": case.get(
                        "dedicated_candidate_numeric_hosts"
                    ),
                    "audio_checked": case.get("audio_checked"),
                    "audio_mismatch_count": len(case.get("audio_mismatches", [])),
                    "source_mismatch_count": len(case.get("direct_source_mismatches", [])),
                    "url_origin_mismatches": case.get("url_origin_mismatches"),
                    "url_host_policy_mismatches": case.get("url_host_policy_mismatches"),
                    "disk_reference_mismatch_count": len(
                        case.get("disk_reference_mismatches", [])
                    ),
                    "response_bytes": case.get("response_bytes"),
                    "expected_response_bytes": case.get("expected_response_bytes"),
                    "error": case.get("error"),
                }
            )
    return rows


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare_audio_urls(
    endpoint: Endpoint,
    hit_cases: Sequence[dict[str, Any]],
    timeout: float,
) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    errors: list[str] = []
    with HttpSession(True, timeout=timeout) as session:
        for case in hit_cases[:12]:
            try:
                response = session.request(query_url(endpoint, case["params"]))
                payload, schema_error = parse_legacy_payload(response.body)
                if schema_error or not payload or not payload["audioSources"]:
                    raise RuntimeError(schema_error or "no candidates")
                resolved, _ = resolve_audio_url(endpoint, payload["audioSources"][0]["url"])
                if resolved not in urls:
                    urls.append(resolved)
            except Exception as exc:
                errors.append(f"{case['case_id']}: {type(exc).__name__}: {exc}")
    return urls, errors


def parse_endpoint_arguments(values: Sequence[str] | None) -> list[Endpoint]:
    configured = DEFAULT_ENDPOINTS.copy() if not values else {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--endpoint must be NAME=URL, got {value!r}")
        name, url = value.split("=", 1)
        configured[name.strip()] = url.strip().rstrip("/")
    return [Endpoint(name, url) for name, url in configured.items()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark and byte-validate legacy, optimized Anki, and Rust Yomitan audio servers."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Read-only entries.db path")
    parser.add_argument(
        "--endpoint",
        action="append",
        metavar="NAME=URL",
        help="Endpoint; repeat to override the default three-endpoint set",
    )
    parser.add_argument("--reference", default="original", help="Endpoint used as live oracle")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFAULTS), default="standard")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=5050)
    parser.add_argument("--max-audio-candidates", type=int, default=512)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
    )
    parser.add_argument(
        "--startup-spec",
        type=Path,
        help="Optional JSON describing fresh-process startup trials on unused ports",
    )
    parser.add_argument(
        "--duplication-report",
        type=Path,
        default=Path(__file__).resolve().parent / "results" / "duplication.json",
        help="Optional duplication JSON to embed when it exists",
    )
    parser.add_argument(
        "--architecture-report",
        type=Path,
        default=Path(__file__).resolve().parent / "architecture-variants.json",
        help="Optional architecture-variant JSON to embed when it exists",
    )
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--lookup-iterations", type=int)
    parser.add_argument("--e2e-iterations", type=int)
    parser.add_argument("--play-iterations", type=int)
    parser.add_argument("--concurrency-requests", type=int)
    parser.add_argument("--workers", help="Comma-separated concurrency levels")
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--parity-cases", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = dict(PROFILE_DEFAULTS[args.profile])
    for key in (
        "repeats",
        "lookup_iterations",
        "e2e_iterations",
        "play_iterations",
        "concurrency_requests",
        "warmup",
        "parity_cases",
    ):
        override = getattr(args, key)
        if override is not None:
            profile[key] = override
    if args.workers:
        profile["workers"] = [int(item) for item in args.workers.split(",") if item.strip()]
    endpoints = parse_endpoint_arguments(args.endpoint)
    endpoint_by_name = {endpoint.name: endpoint for endpoint in endpoints}
    if args.reference not in endpoint_by_name:
        raise SystemExit(f"Reference endpoint {args.reference!r} is not configured")
    reference_endpoint = endpoint_by_name[args.reference]
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    started_at = utc_now()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{utc_now()}] Reading corpus from {args.db}", flush=True)
    with CorpusBuilder(args.db, seed=args.seed) as corpus:
        db_stats = corpus.database_stats()
        db_stats["sqlite_version"] = sqlite3.sqlite_version
        cases = corpus.build()
        unique_cases_per_mode = profile["repeats"] * profile["lookup_iterations"]
        timed_performance_cases = unique_cases_per_mode * 2
        # Keep oracle/parity materialization disjoint from both timed transport
        # streams. That guarantees the "unique" workload has not populated a
        # response cache on any endpoint before its one measured use.
        requested_performance_cases = timed_performance_cases + profile["parity_cases"]
        focused_case_keys = {canonical_json(case.params) for case in cases}
        performance_pool = corpus.build_performance_cases(
            requested_performance_cases + len(focused_case_keys)
        )
        performance_cases = [
            case
            for case in performance_pool
            if canonical_json(case.params) not in focused_case_keys
        ][:requested_performance_cases]
        if len(performance_cases) < requested_performance_cases:
            print(
                f"Warning: requested {requested_performance_cases} unique performance cases, "
                f"built {len(performance_cases)}",
                file=sys.stderr,
                flush=True,
            )
        parity_cases = cases + performance_cases[
            timed_performance_cases : timed_performance_cases + profile["parity_cases"]
        ]
        print(f"[{utc_now()}] Materializing {len(parity_cases)} reference cases", flush=True)
        reference, reference_errors = reference_cases(
            reference_endpoint, parity_cases, args.timeout
        )
        if reference_errors:
            print("Reference errors:", *reference_errors, sep="\n  ", file=sys.stderr, flush=True)
        work_cases = choose_work_cases(reference)
        hit_cases = [case for case in work_cases if case.get("candidates")]
        if not hit_cases:
            raise SystemExit("Reference server yielded no bounded real hits; cannot benchmark audio")
        first_hit = hit_cases[0]
        first_candidate = first_hit["candidates"][0]

        results: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": started_at,
            "config": {
                "profile": args.profile,
                **profile,
                "timeout_seconds": args.timeout,
                "seed": args.seed,
                "max_audio_candidates": args.max_audio_candidates,
                "reference_endpoint": args.reference,
                "percentile_method": "nearest-rank",
            },
            "machine": machine_metadata(),
            "database": db_stats,
            "corpus": [asdict(case) for case in cases],
            "performance_corpus": [asdict(case) for case in performance_cases],
            "reference": reference,
            "reference_errors": reference_errors,
            "endpoints": {},
            "first_touch": {},
            "features": {},
            "benchmarks": [],
            "correctness": {},
            "startup": [],
            "legacy_returned_hostname_probe": {},
            "duplication_analysis": json.loads(args.duplication_report.read_text(encoding="utf-8"))
            if args.duplication_report and args.duplication_report.is_file()
            else None,
            "architecture_variants": json.loads(
                args.architecture_report.read_text(encoding="utf-8")
            )
            if args.architecture_report and args.architecture_report.is_file()
            else None,
            "methodology": {
                "warm": "Corpus/database setup, explicit warmups, then measured repeated trials.",
                "first_touch": "First explicitly timed request after corpus/oracle setup; pre-existing server and Windows filesystem caches are not flushed.",
                "connection_close": "New HTTP connection for every request with Connection: close.",
                "keep_alive": "One persistent connection per sequential client or worker thread when server permits.",
                "connection_evidence": "Every trial records connection_new_flags (true=new socket for that response; false=reused), plus aggregate new/reused counts and server will-close counts.",
                "latency_clock": "time.perf_counter_ns captures status/headers, first body byte (None for HEAD/empty), and complete response body read separately.",
                "throughput_unit": "Completed benchmark operations per second; a two-stage operation performs lookup plus audio (two HTTP requests), all other operations perform one request.",
                "audio_parity": "SHA-256 and byte length versus live original; on-disk copy checked when legacy path is available.",
                "unique_lookup_isolation": "Timed unique-hit keys are disjoint across connection modes and from parity/oracle materialization.",
            },
        }

        print(f"[{utc_now()}] Probing endpoints", flush=True)
        available: list[Endpoint] = []
        for endpoint in endpoints:
            probe = endpoint_probe(endpoint, args.timeout)
            pid = listener_pid(endpoint.port) if probe.get("available") else None
            endpoint_result = {
                "base_url": endpoint.base_url,
                **probe,
                "pid": pid,
                "process": process_identity(pid),
                "memory_before": windows_process_memory(pid),
            }
            results["endpoints"][endpoint.name] = endpoint_result
            if probe.get("available"):
                available.append(endpoint)
        if reference_endpoint not in available:
            raise SystemExit(f"Reference endpoint {reference_endpoint.base_url} is not available")

        for endpoint in available:
            print(f"[{utc_now()}] First-observed touch and feature checks: {endpoint.name}", flush=True)
            results["first_touch"][endpoint.name] = first_touch_probe(
                endpoint, first_hit, args.timeout
            )
            results["features"][endpoint.name] = feature_checks(
                endpoint, first_hit, first_candidate, args.timeout
            )

        print(f"[{utc_now()}] Legacy returned-hostname control probe", flush=True)
        results["legacy_returned_hostname_probe"] = legacy_returned_hostname_probe(
            reference_endpoint,
            first_candidate["url"],
            args.timeout,
            min(5, max(3, profile["repeats"])),
        )

        for endpoint in available:
            print(f"[{utc_now()}] Sequential benchmarks: {endpoint.name}", flush=True)
            for mode_index, keep_alive in enumerate((False, True)):
                lookup_record = benchmark_lookup(
                    endpoint,
                    work_cases,
                    keep_alive,
                    profile["repeats"],
                    profile["lookup_iterations"],
                    profile["warmup"],
                    args.timeout,
                )
                lookup_record["category"] = "lookup_hotset"
                results["benchmarks"].append(lookup_record)

                unique_start = mode_index * unique_cases_per_mode
                unique_end = unique_start + unique_cases_per_mode
                unique_record = benchmark_lookup(
                    endpoint,
                    [asdict(case) for case in performance_cases[unique_start:unique_end]],
                    keep_alive,
                    profile["repeats"],
                    profile["lookup_iterations"],
                    0,
                    args.timeout,
                    unique_stream=True,
                    require_hit=True,
                )
                unique_record["category"] = "lookup_unique_real_hit"
                results["benchmarks"].append(unique_record)

                two_stage_record = benchmark_two_stage(
                    endpoint,
                    hit_cases,
                    keep_alive,
                    profile["repeats"],
                    profile["e2e_iterations"],
                    max(1, profile["warmup"] // 4),
                    args.timeout,
                )
                two_stage_record["category"] = "two_stage"
                results["benchmarks"].append(two_stage_record)

                if results["features"][endpoint.name].get("v1_play", {}).get("supported"):
                    play_record = benchmark_play(
                        endpoint,
                        hit_cases,
                        keep_alive,
                        profile["repeats"],
                        profile["play_iterations"],
                        profile["warmup"],
                        args.timeout,
                    )
                    play_record["category"] = "play"
                    results["benchmarks"].append(play_record)

            lookup_urls = [query_url(endpoint, case["params"]) for case in work_cases]
            audio_urls, audio_url_errors = prepare_audio_urls(endpoint, hit_cases, args.timeout)
            results["endpoints"][endpoint.name]["audio_workload_url_errors"] = audio_url_errors
            concurrency_repeats = min(3, profile["repeats"])
            print(f"[{utc_now()}] Concurrency benchmarks: {endpoint.name}", flush=True)
            for keep_alive in (False, True):
                for workers in profile["workers"]:
                    for workload, urls in (("lookup", lookup_urls), ("audio", audio_urls)):
                        record = benchmark_concurrency(
                            endpoint,
                            urls,
                            workload,
                            keep_alive,
                            workers,
                            profile["concurrency_requests"],
                            concurrency_repeats,
                            args.timeout,
                        )
                        record["category"] = "concurrency"
                        results["benchmarks"].append(record)

        add_speedups(results["benchmarks"])

        for endpoint in available:
            print(f"[{utc_now()}] Full parity validation: {endpoint.name}", flush=True)
            endpoint_correctness = validate_correctness(
                endpoint,
                reference,
                corpus.source_ids,
                corpus,
                args.timeout,
                args.max_audio_candidates,
            )
            endpoint_correctness["legacy_parity_pass"] = endpoint_correctness["pass"]
            if endpoint.name != "original":
                required = (
                    "healthz",
                    "info",
                    "v1_candidates",
                    "v1_play",
                    "head",
                    "range",
                    "range_head",
                    "v1_play_head",
                    "v1_play_range",
                    "stable_urls",
                    "cors",
                    "options",
                    "etag",
                )
                failures = [
                    feature
                    for feature in required
                    if not results["features"][endpoint.name].get(feature, {}).get("supported")
                ]
                if not results["features"][endpoint.name].get("v1_candidates", {}).get("parity"):
                    failures.append("v1_candidates_parity")
                endpoint_correctness["required_feature_failures"] = failures
                endpoint_correctness["required_features_pass"] = not failures
                endpoint_correctness["pass"] = bool(
                    endpoint_correctness["legacy_parity_pass"] and not failures
                )
            else:
                endpoint_correctness["required_features_pass"] = None
                endpoint_correctness["required_feature_failures"] = []
            results["correctness"][endpoint.name] = endpoint_correctness
            pid = listener_pid(endpoint.port)
            results["endpoints"][endpoint.name]["pid_after"] = pid
            results["endpoints"][endpoint.name]["process_after"] = process_identity(pid)
            results["endpoints"][endpoint.name]["memory_after"] = windows_process_memory(pid)

    if args.startup_spec:
        print(f"[{utc_now()}] Fresh-process startup trials", flush=True)
        results["startup"] = run_startup_specs(args.startup_spec, profile["repeats"])

    results["overall_pass"] = bool(
        not reference_errors
        and all(endpoint.get("available") for endpoint in results["endpoints"].values())
        and results["correctness"]
        and all(item.get("pass") for item in results["correctness"].values())
        and all(
            startup.get("supported")
            and startup.get("successful_trials") == len(startup.get("trials", []))
            for startup in results.get("startup", [])
        )
    )
    results["finished_at"] = utc_now()
    results = rounded(results)
    json_text = json.dumps(results, ensure_ascii=False, indent=2)
    markdown = make_markdown(results)
    metric_rows = benchmark_csv_rows(results["benchmarks"])
    parity_rows = correctness_csv_rows(results["correctness"])

    timestamp_stem = f"benchmark-{run_id}"
    paths = {
        "json": args.output_dir / f"{timestamp_stem}.json",
        "markdown": args.output_dir / f"{timestamp_stem}.md",
        "metrics_csv": args.output_dir / f"{timestamp_stem}-metrics.csv",
        "correctness_csv": args.output_dir / f"{timestamp_stem}-correctness.csv",
        "corpus_json": args.output_dir / f"{timestamp_stem}-corpus.json",
    }
    paths["json"].write_text(json_text, encoding="utf-8")
    paths["markdown"].write_text(markdown, encoding="utf-8")
    write_csv(paths["metrics_csv"], metric_rows)
    write_csv(paths["correctness_csv"], parity_rows)
    paths["corpus_json"].write_text(
        json.dumps(results["corpus"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (args.output_dir / "latest.json").write_text(json_text, encoding="utf-8")
    (args.output_dir / "latest.md").write_text(markdown, encoding="utf-8")
    write_csv(args.output_dir / "latest-metrics.csv", metric_rows)
    write_csv(args.output_dir / "latest-correctness.csv", parity_rows)
    (args.output_dir / "latest-corpus.json").write_text(
        json.dumps(results["corpus"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[{utc_now()}] Complete", flush=True)
    for label, path in paths.items():
        print(f"{label}: {path}")
    return 0 if results["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
