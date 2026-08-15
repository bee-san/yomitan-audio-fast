from __future__ import annotations

import json
import socket
import threading
import atexit

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .config import ALL_SOURCES, get_server_config
from .consts import HOSTNAME, PORT
from .fast_store import FirstAudioRequest, LookupRequest, LookupStore
from .util import get_db_path, get_program_root_path, get_version_file_path


MIME_TYPE_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}
MAX_TERM_LENGTH = 512
MAX_FILTER_VALUES = 128
STREAM_BUFFER_SIZE = 64 * 1024


class ServerStartupError(RuntimeError):
    """A fatal server-start failure, carrying friendly, actionable guidance.

    The ``str()`` of this error is safe to show an ordinary user: it explains
    that the server could not start, gives concrete recovery steps, and appends
    the raw cause as technical detail. The original exception is chained as
    ``__cause__`` so logs and diagnostics keep the precise failure.
    """


def startup_failure_message(error: BaseException) -> str:
    """Build friendly, actionable copy for a fatal server-start failure.

    Leads with what happened and what to do (the most common cause by far is the
    configured port already being in use by another local server), then appends
    the raw error as technical detail without hiding it.
    """

    return (
        "Local Audio Server could not start.\n\n"
        "Another program may already be using its port on your computer. Close "
        "the other local audio server if you have one running, or change the "
        "port in the add-on's configuration to a free one, then restart Anki.\n\n"
        f"Technical detail: {error}"
    )


class OptimizedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address) -> None:
        self.runtime: Optional[ServerRuntime] = None
        super().__init__(address, LocalAudioHandler)

    def handle_error(self, _request, _client_address) -> None:
        return


class LocalAudioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalAudioFast"
    sys_version = ""
    disable_nagle_algorithm = True

    def setup(self) -> None:
        super().setup()
        self._stream_buffer = bytearray(STREAM_BUFFER_SIZE)
        try:
            self.connection.settimeout(30.0)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    @property
    def runtime(self) -> "ServerRuntime":
        runtime = self.server.runtime
        if runtime is None:
            raise RuntimeError("server runtime is not initialized")
        return runtime

    def log_message(self, *_args) -> None:
        return

    def log_error(self, *_args) -> None:
        return

    def _headers(
        self,
        status: int,
        content_type: Optional[str],
        content_length: Optional[int],
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response_only(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def _payload(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        head_only: bool = False,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._headers(status, content_type, len(payload), extra)
        if not head_only and payload:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

    def _error(self, status: int, message: str, head_only: bool = False) -> None:
        payload = json.dumps(
            {"error": message}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._payload(
            status,
            payload,
            "application/json; charset=utf-8",
            head_only=head_only,
            extra=(("Cache-Control", "no-store"),),
        )

    def _parse_lookup_terms(
        self, query: str
    ) -> tuple[dict[str, list[str]], str, Optional[str]]:
        values = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=32,
        )
        if "term" in values:
            expression = values["term"][0]
        elif "expression" in values:
            expression = values["expression"][0]
        else:
            raise ValueError(
                "add a 'term' (or 'expression') query parameter naming the word to look up"
            )
        if not expression:
            raise ValueError("the 'term' is empty; provide the word to look up")
        if len(expression) > MAX_TERM_LENGTH:
            raise ValueError(
                f"the 'term' is too long; use at most {MAX_TERM_LENGTH} characters"
            )
        reading = values.get("reading", [None])[0]
        if reading == "":
            reading = None
        if reading is not None and len(reading) > MAX_TERM_LENGTH:
            raise ValueError(
                f"the 'reading' is too long; use at most {MAX_TERM_LENGTH} characters"
            )
        return values, expression, reading

    def _parse_lookup(self, query: str) -> LookupRequest:
        values, expression, reading = self._parse_lookup_terms(query)
        if "sources" in values:
            sources = tuple(
                value.strip() for value in values["sources"][0].split(",") if value.strip()
            )
            if not sources:
                sources = self.runtime.store.source_ids
        else:
            sources = self.runtime.store.source_ids
        users = tuple(
            value.strip()
            for value in values.get("user", [""])[0].split(",")
            if value.strip()
        )
        if len(sources) > MAX_FILTER_VALUES or len(users) > MAX_FILTER_VALUES:
            raise ValueError(
                f"too many 'sources' or 'user' filters; list at most {MAX_FILTER_VALUES} of each"
            )
        return LookupRequest(expression, reading, sources, users)

    def _parse_first_lookup(self, query: str) -> FirstAudioRequest:
        _values, expression, reading = self._parse_lookup_terms(query)
        return FirstAudioRequest(expression, reading)

    @staticmethod
    def _range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
        if header is None:
            return 0, size
        if size <= 0:
            return None
        if not header.startswith("bytes=") or "," in header:
            return None
        spec = header[6:].strip()
        if "-" not in spec:
            return None
        start_text, end_text = spec.split("-", 1)
        try:
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
                if start < 0 or end < start or start >= size:
                    return None
                end = min(end, size - 1)
            else:
                suffix = int(end_text)
                if suffix <= 0:
                    return None
                start = max(0, size - suffix)
                end = size - 1
        except ValueError:
            return None
        return start, end - start + 1

    def _audio_headers(
        self,
        mime_type: str,
        size: int,
        selected: tuple[int, int],
        etag: str,
        immutable: bool,
        range_requested: bool,
        cache_control: Optional[str] = None,
    ) -> None:
        start, length = selected
        extras = [
            ("Accept-Ranges", "bytes"),
            ("ETag", etag),
            (
                "Cache-Control",
                cache_control
                if cache_control is not None
                else (
                    "public, max-age=31536000, immutable"
                    if immutable
                    else "public, max-age=3600"
                ),
            ),
        ]
        if range_requested:
            extras.append(("Content-Range", f"bytes {start}-{start + length - 1}/{size}"))
        self._headers(
            HTTPStatus.PARTIAL_CONTENT if range_requested else HTTPStatus.OK,
            mime_type,
            length,
            tuple(extras),
        )

    def _not_modified(
        self,
        etag: str,
        immutable: bool,
        cache_control: Optional[str] = None,
    ) -> bool:
        if self.headers.get("Range") is not None:
            return False
        candidates = {
            value.strip() for value in self.headers.get("If-None-Match", "").split(",")
        }
        if etag not in candidates and "*" not in candidates:
            return False
        self._headers(
            HTTPStatus.NOT_MODIFIED,
            None,
            None,
            (
                ("ETag", etag),
                (
                    "Cache-Control",
                    cache_control
                    if cache_control is not None
                    else (
                        "public, max-age=31536000, immutable"
                        if immutable
                        else "public, max-age=3600"
                    ),
                ),
            ),
        )
        return True

    def _packed_audio(
        self,
        version: str,
        audio_id_text: str,
        head_only: bool,
        cache_control: Optional[str] = None,
    ) -> bool:
        try:
            audio_id = int(audio_id_text)
        except ValueError:
            return False
        with self.runtime.store.leased_packed_audio(version, audio_id) as (pack, audio):
            if pack is None or audio is None:
                return False
            etag = f'"{version}-{audio.audio_id}"'
            if self._not_modified(etag, immutable=True, cache_control=cache_control):
                return True
            range_header = self.headers.get("Range")
            selected = self._range(range_header, audio.length)
            if selected is None:
                self._headers(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "text/plain; charset=utf-8",
                    0,
                    (("Content-Range", f"bytes */{audio.length}"),),
                )
                return True
            self._audio_headers(
                audio.mime_type,
                audio.length,
                selected,
                etag,
                immutable=True,
                range_requested=range_header is not None,
                cache_control=cache_control,
            )
            if not head_only:
                view = pack.view(audio, *selected)
                try:
                    self.wfile.write(view)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                finally:
                    view.release()
            return True

    def _legacy_audio(
        self,
        source_id: str,
        encoded_filename: str,
        head_only: bool,
        cache_control: Optional[str] = None,
    ) -> bool:
        source = self.runtime.store.sources.get(unquote(source_id))
        if source is None:
            return False
        try:
            filename = unquote(encoded_filename)
            if "\x00" in filename:
                return False
            relative = Path(filename.replace("\\", "/"))
            if relative.is_absolute() or any(part == ".." for part in relative.parts):
                return False
        except (ValueError, UnicodeError):
            return False
        try:
            media_root = source.get_media_dir_path().resolve(strict=True)
            audio_path = (media_root / relative).resolve(strict=True)
            audio_path.relative_to(media_root)
            mime_type = MIME_TYPE_BY_SUFFIX.get(audio_path.suffix.lower())
            if mime_type is None or not audio_path.is_file():
                raise FileNotFoundError(audio_path)
            stat = audio_path.stat()
        except (OSError, ValueError):
            packed = self.runtime.store.packed_target_for_legacy_path(
                unquote(source_id), filename
            )
            if packed is None:
                return False
            version, audio_id = packed
            return self._packed_audio(
                version,
                str(audio_id),
                head_only,
                cache_control=(
                    cache_control
                    if cache_control is not None
                    else "public, max-age=3600"
                ),
            )
        range_header = self.headers.get("Range")
        selected = self._range(range_header, stat.st_size)
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self._not_modified(etag, immutable=False, cache_control=cache_control):
            return True
        if selected is None:
            self._headers(
                HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                "text/plain; charset=utf-8",
                0,
                (("Content-Range", f"bytes */{stat.st_size}"),),
            )
            return True
        self._audio_headers(
            mime_type,
            stat.st_size,
            selected,
            etag,
            immutable=False,
            range_requested=range_header is not None,
            cache_control=cache_control,
        )
        if head_only:
            return True
        start, remaining = selected
        view = memoryview(self._stream_buffer)
        try:
            with audio_path.open("rb") as audio_file:
                audio_file.seek(start)
                while remaining:
                    count = audio_file.readinto(view[: min(len(view), remaining)])
                    if not count:
                        self.close_connection = True
                        break
                    self.wfile.write(view[:count])
                    remaining -= count
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            view.release()
        return True

    def _serve_audio_url(
        self,
        url: str,
        head_only: bool,
        cache_control: Optional[str] = None,
    ) -> bool:
        path = urlsplit(url).path
        path_parts = path.lstrip("/").split("/")
        if (
            len(path_parts) == 4
            and path_parts[0] == "v"
            and path_parts[2] == "audio"
        ):
            return self._packed_audio(
                path_parts[1],
                path_parts[3],
                head_only,
                cache_control=cache_control,
            )
        legacy_parts = path.lstrip("/").split("/", 1)
        return len(legacy_parts) == 2 and self._legacy_audio(
            legacy_parts[0],
            legacy_parts[1],
            head_only,
            cache_control=cache_control,
        )

    def _dispatch(self, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/" and not parsed.query:
            payload = f"Local Audio Server v{self.runtime.version}".encode("utf-8")
            self._payload(
                HTTPStatus.OK,
                payload,
                "text/plain; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        if path == "/healthz":
            self._payload(
                HTTPStatus.OK,
                b'{"status":"ok"}',
                "application/json; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        if path == "/v1/info":
            payload = json.dumps(
                self.runtime.store.info(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self._payload(
                HTTPStatus.OK,
                payload,
                "application/json; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        if path == "/v1/first":
            try:
                request = self._parse_first_lookup(parsed.query)
                payload = self.runtime.store.lookup_first(request)
            except (ValueError, UnicodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error), head_only)
                return
            self._payload(
                HTTPStatus.OK,
                payload,
                "application/json; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        if path in ("/v1/play", "/v1/candidates"):
            try:
                request = self._parse_lookup(parsed.query)
            except (ValueError, UnicodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error), head_only)
                return
            if path == "/v1/play":
                audio_url = self.runtime.store.best_audio_url(request)
                if audio_url is None or not self._serve_audio_url(
                    audio_url,
                    head_only,
                    cache_control="no-store",
                ):
                    self._error(
                        HTTPStatus.NOT_FOUND,
                        "no audio was found for that term and reading; "
                        "try a different reading or check your configured sources",
                        head_only,
                    )
                return
            payload = self.runtime.store.candidates_payload(request)
            self._payload(
                HTTPStatus.OK,
                payload,
                "application/json; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        path_parts = path.lstrip("/").split("/")
        if (
            len(path_parts) == 4
            and path_parts[0] == "v"
            and path_parts[2] == "audio"
            and self._packed_audio(path_parts[1], path_parts[3], head_only)
        ):
            return
        legacy_parts = path.lstrip("/").split("/", 1)
        if len(legacy_parts) == 2 and self._legacy_audio(
            legacy_parts[0], legacy_parts[1], head_only
        ):
            return
        if path == "/" and parsed.query:
            try:
                request = self._parse_lookup(parsed.query)
                payload = self.runtime.store.lookup(request)
            except (ValueError, UnicodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error), head_only)
                return
            self._payload(
                HTTPStatus.OK,
                payload,
                "application/json; charset=utf-8",
                head_only,
                (("Cache-Control", "no-store"),),
            )
            return
        self._error(
            HTTPStatus.NOT_FOUND,
            "unknown request path; use / with a 'term' query, or "
            "/v1/play, /v1/candidates, /v1/first, /v1/info, or /healthz",
            head_only,
        )

    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_OPTIONS(self) -> None:
        self._headers(
            HTTPStatus.NO_CONTENT,
            "text/plain; charset=utf-8",
            0,
            (
                ("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"),
                (
                    "Access-Control-Allow-Headers",
                    "Range, Content-Type, If-None-Match",
                ),
                ("Access-Control-Max-Age", "86400"),
            ),
        )


class ServerRuntime:
    def __init__(
        self,
        host: str = HOSTNAME,
        port: int = PORT,
        db_path: Optional[Path] = None,
        pack_root: Optional[Path] = None,
        lookup_mode: Optional[str] = None,
        response_cache_size: Optional[int] = None,
        row_cache_size: Optional[int] = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError(
                "the desktop audio server only listens on 127.0.0.1 (your own "
                "computer); set the host back to 127.0.0.1 and restart it"
            )
        config = get_server_config()
        try:
            self.server = OptimizedHTTPServer((host, port))
        except OSError as error:
            # The overwhelmingly common cause is the port already being held by
            # another local server. Turn the raw OSError into friendly, actionable
            # guidance while chaining the original for logs/diagnostics.
            raise ServerStartupError(startup_failure_message(error)) from error
        actual_port = self.server.server_address[1]
        root = get_program_root_path()
        try:
            self.version = get_version_file_path().read_text(encoding="utf-8").strip()
            self.store = LookupStore(
                db_path or get_db_path(),
                ALL_SOURCES,
                f"http://127.0.0.1:{actual_port}",
                pack_root or root / "user_files" / "fast_audio",
                response_cache_size=(
                    response_cache_size
                    if response_cache_size is not None
                    else config["response_cache_entries"]
                ),
                row_cache_size=(
                    row_cache_size
                    if row_cache_size is not None
                    else config["row_cache_entries"]
                ),
                lookup_mode=lookup_mode or config["lookup_mode"],
            )
        except Exception:
            self.server.server_close()
            raise
        self.server.runtime = self
        self.thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self.server.server_address

    @property
    def base_url(self) -> str:
        return f"http://{self.address[0]}:{self.address[1]}"

    def start_background(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="local-audio-fast-http",
            daemon=True,
        )
        self.thread.start()

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.1)

    def stop(self) -> None:
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.thread = None
        self.server.server_close()
        self.store.close()


_runtime_lock = threading.Lock()
_runtime: Optional[ServerRuntime] = None


def get_runtime() -> Optional[ServerRuntime]:
    return _runtime


def run_server() -> ServerRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            return _runtime
        config = get_server_config()
        runtime = ServerRuntime(
            host="127.0.0.1",
            port=config["port"],
            lookup_mode=config["lookup_mode"],
            response_cache_size=config["response_cache_entries"],
            row_cache_size=config["row_cache_entries"],
        )
        runtime.start_background()
        _runtime = runtime
        return runtime


def stop_server() -> None:
    """Idempotently release the add-on server during interpreter shutdown."""

    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        runtime.stop()


atexit.register(stop_server)
