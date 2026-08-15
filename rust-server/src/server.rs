use std::io::{Read, Seek, SeekFrom};
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use axum::Router;
use axum::body::Body;
use axum::extract::{Path as AxumPath, RawQuery, State};
use axum::http::header::{
    ACCEPT_RANGES, ACCESS_CONTROL_ALLOW_HEADERS, ACCESS_CONTROL_ALLOW_METHODS,
    ACCESS_CONTROL_ALLOW_ORIGIN, ACCESS_CONTROL_MAX_AGE, ALLOW, CACHE_CONTROL, CONTENT_LENGTH,
    CONTENT_RANGE, CONTENT_TYPE, ETAG, IF_NONE_MATCH, RANGE, VARY,
};
use axum::http::{HeaderMap, HeaderValue, Method, Response, StatusCode};
use axum::routing::get;
use axum::serve::ListenerExt;
use bytes::Bytes;
use moka::sync::Cache;
use serde::Serialize;
use tokio::net::TcpListener;

use crate::bundle::{AudioEntry, Bundle};
use crate::query::{CandidateResponse, LookupMode, QueryEngine, QueryInput, parse_query};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AssetMode {
    Pack,
    Files,
}

#[derive(Debug, Clone)]
pub struct ServeOptions {
    pub bundle_root: PathBuf,
    pub host: IpAddr,
    pub port: u16,
    pub lookup_mode: LookupMode,
    pub asset_mode: AssetMode,
    pub legacy_root: Option<PathBuf>,
    pub response_cache_entries: u64,
    pub verify_index: bool,
}

#[derive(Clone)]
struct AppState {
    engine: Arc<QueryEngine>,
    base_url: Arc<str>,
    asset_mode: AssetMode,
    legacy_root: Option<Arc<PathBuf>>,
    compatibility_cache: Cache<QueryInput, Bytes>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AudioSourceList {
    #[serde(rename = "type")]
    response_type: &'static str,
    audio_sources: Vec<AudioSource>,
}

#[derive(Serialize)]
struct AudioSource {
    name: String,
    url: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CandidateList {
    version: String,
    candidates: Vec<CandidateResponse>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct InfoResponse {
    status: &'static str,
    server_version: &'static str,
    bundle_version: String,
    lookup_mode: &'static str,
    asset_mode: &'static str,
    term_count: u64,
    record_count: u64,
    audio_count: u64,
    unique_blob_count: u64,
    pack_bytes: u64,
    deduplicated_bytes: u64,
    sources: Vec<String>,
}

pub async fn serve(options: ServeOptions) -> Result<()> {
    ensure!(
        options.host.is_loopback(),
        "the audio server only listens on a loopback address (127.0.0.1 or ::1); \
         set --host to 127.0.0.1 and retry (requested {})",
        options.host
    );
    let bundle = Arc::new(Bundle::open(&options.bundle_root, options.verify_index)?);
    let load_started = std::time::Instant::now();
    let engine = Arc::new(QueryEngine::new(bundle, options.lookup_mode)?);
    let backend_load_ms = load_started.elapsed().as_secs_f64() * 1_000.0;
    let legacy_root = match (&options.asset_mode, options.legacy_root) {
        (AssetMode::Pack, _) => None,
        (AssetMode::Files, Some(root)) => {
            Some(Arc::new(root.canonicalize().with_context(|| {
                format!("cannot resolve legacy root {}", root.display())
            })?))
        }
        (AssetMode::Files, None) => {
            anyhow::bail!(
                "--asset-mode files needs --legacy-root pointing at the folder \
                 that holds the original audio files"
            )
        }
    };
    let bind_address = SocketAddr::new(options.host, options.port);
    let listener = TcpListener::bind(bind_address)
        .await
        .map_err(|error| anyhow::anyhow!(bind_failure_message(bind_address, error)))?;
    let address = listener.local_addr()?;
    let listener = listener.tap_io(|stream| {
        if let Err(error) = stream.set_nodelay(true) {
            eprintln!("warning: failed to set TCP_NODELAY: {error}");
        }
    });
    let host = match address.ip() {
        IpAddr::V4(value) => value.to_string(),
        IpAddr::V6(value) => format!("[{value}]"),
    };
    let base_url: Arc<str> = format!("http://{host}:{}", address.port()).into();
    let bundle_version = engine.bundle().manifest.bundle_version.clone();
    let lookup_name = engine.mode_name();
    let state = AppState {
        engine,
        base_url: base_url.clone(),
        asset_mode: options.asset_mode,
        legacy_root,
        compatibility_cache: Cache::builder()
            .max_capacity(options.response_cache_entries)
            .build(),
    };
    let app = Router::new()
        .route(
            "/",
            get(compatibility).head(compatibility).options(preflight),
        )
        .route("/healthz", get(health).head(health).options(preflight))
        .route("/v1/info", get(info).head(info).options(preflight))
        .route(
            "/v1/candidates",
            get(candidates).head(candidates).options(preflight),
        )
        .route("/v1/play", get(play).head(play).options(preflight))
        .route(
            "/v/{version}/audio/{audio_id}",
            get(audio_by_id).head(audio_by_id).options(preflight),
        )
        .fallback(not_found)
        .with_state(state);

    println!(
        "READY {base_url} bundle={} lookup={} assets={} backend_load_ms={backend_load_ms:.3}",
        bundle_version,
        lookup_name,
        match options.asset_mode {
            AssetMode::Pack => "mmap-pack",
            AssetMode::Files => "individual-files",
        }
    );
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("HTTP server failed")?;
    Ok(())
}

async fn compatibility(
    State(state): State<AppState>,
    method: Method,
    RawQuery(raw): RawQuery,
) -> Response<Body> {
    if raw.as_deref().is_none_or(str::is_empty) {
        let body = Bytes::from(format!("Yomitan Audio RS v{}", env!("CARGO_PKG_VERSION")));
        return data_response(
            StatusCode::OK,
            "text/plain; charset=utf-8",
            body,
            method == Method::HEAD,
            false,
        );
    }
    let query = match parse_query(raw.as_deref()) {
        Ok(query) => query,
        Err(error) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                &error.to_string(),
                method == Method::HEAD,
            );
        }
    };
    let body = if let Some(cached) = state.compatibility_cache.get(&query) {
        cached
    } else {
        let candidates = match state.engine.candidates(&query) {
            Ok(value) => value,
            Err(error) => {
                return internal_error_response("looking up audio", error, method == Method::HEAD);
            }
        };
        let audio_sources = candidates
            .into_iter()
            .map(|candidate| {
                let response = state.engine.candidate_response(candidate, &state.base_url);
                AudioSource {
                    name: response.name,
                    url: response.url,
                }
            })
            .collect();
        let response = AudioSourceList {
            response_type: "audioSourceList",
            audio_sources,
        };
        let serialized = match serde_json::to_vec(&response) {
            Ok(value) => Bytes::from(value),
            Err(error) => {
                return internal_error_response(
                    "preparing the audio list",
                    error,
                    method == Method::HEAD,
                );
            }
        };
        state.compatibility_cache.insert(query, serialized.clone());
        serialized
    };
    data_response(
        StatusCode::OK,
        "application/json; charset=utf-8",
        body,
        method == Method::HEAD,
        false,
    )
}

async fn health(State(_state): State<AppState>, method: Method) -> Response<Body> {
    data_response(
        StatusCode::OK,
        "application/json; charset=utf-8",
        Bytes::from_static(br#"{"status":"ok"}"#),
        method == Method::HEAD,
        false,
    )
}

async fn info(State(state): State<AppState>, method: Method) -> Response<Body> {
    let manifest = &state.engine.bundle().manifest;
    let response = InfoResponse {
        status: "ok",
        server_version: env!("CARGO_PKG_VERSION"),
        bundle_version: manifest.bundle_version.clone(),
        lookup_mode: state.engine.mode_name(),
        asset_mode: match state.asset_mode {
            AssetMode::Pack => "mmap-pack",
            AssetMode::Files => "individual-files",
        },
        term_count: manifest.term_count,
        record_count: manifest.record_count,
        audio_count: manifest.audio_count,
        unique_blob_count: manifest.unique_blob_count,
        pack_bytes: manifest.pack_bytes,
        deduplicated_bytes: manifest.deduplicated_bytes,
        sources: manifest
            .sources
            .iter()
            .map(|source| source.id.clone())
            .collect(),
    };
    json_response(&response, method == Method::HEAD)
}

async fn candidates(
    State(state): State<AppState>,
    method: Method,
    RawQuery(raw): RawQuery,
) -> Response<Body> {
    let query = match parse_query(raw.as_deref()) {
        Ok(query) => query,
        Err(error) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                &error.to_string(),
                method == Method::HEAD,
            );
        }
    };
    let candidates = match state.engine.candidates(&query) {
        Ok(candidates) => candidates
            .into_iter()
            .map(|candidate| state.engine.candidate_response(candidate, &state.base_url))
            .collect(),
        Err(error) => {
            return internal_error_response(
                "listing audio candidates",
                error,
                method == Method::HEAD,
            );
        }
    };
    json_response(
        &CandidateList {
            version: state.engine.bundle().manifest.bundle_version.clone(),
            candidates,
        },
        method == Method::HEAD,
    )
}

async fn play(
    State(state): State<AppState>,
    method: Method,
    RawQuery(raw): RawQuery,
    headers: HeaderMap,
) -> Response<Body> {
    let query = match parse_query(raw.as_deref()) {
        Ok(query) => query,
        Err(error) => {
            return error_response(
                StatusCode::BAD_REQUEST,
                &error.to_string(),
                method == Method::HEAD,
            );
        }
    };
    let candidate = match state.engine.best_candidate(&query) {
        Ok(Some(candidate)) => candidate,
        Ok(None) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "no audio was found for that term and reading; \
                 try a different reading or check your configured sources",
                method == Method::HEAD,
            );
        }
        Err(error) => {
            return internal_error_response("finding audio to play", error, method == Method::HEAD);
        }
    };
    audio_response(
        &state,
        candidate.audio_id,
        candidate.audio,
        &headers,
        method == Method::HEAD,
        false,
    )
    .await
}

async fn audio_by_id(
    State(state): State<AppState>,
    method: Method,
    AxumPath((version, audio_id)): AxumPath<(String, u32)>,
    headers: HeaderMap,
) -> Response<Body> {
    if version != state.engine.bundle().manifest.bundle_version {
        return error_response(
            StatusCode::NOT_FOUND,
            "that audio link is from an older bundle version; \
             request a fresh audio URL from a lookup and retry",
            method == Method::HEAD,
        );
    }
    let audio = match state.engine.bundle().audio(audio_id as usize) {
        Ok(audio) => audio,
        Err(_) => {
            return error_response(
                StatusCode::NOT_FOUND,
                "that audio ID is not in this bundle; \
                 request a fresh audio URL from a lookup and retry",
                method == Method::HEAD,
            );
        }
    };
    audio_response(
        &state,
        audio_id,
        audio,
        &headers,
        method == Method::HEAD,
        true,
    )
    .await
}

async fn audio_response(
    state: &AppState,
    audio_id: u32,
    audio: AudioEntry,
    request_headers: &HeaderMap,
    head: bool,
    immutable: bool,
) -> Response<Body> {
    let etag = format!(
        "\"{}-{}-{:016x}\"",
        state.engine.bundle().manifest.bundle_version,
        audio_id,
        audio.content_hash_prefix
    );
    if request_headers
        .get(IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        == Some(etag.as_str())
        && request_headers.get(RANGE).is_none()
    {
        let mut response = Response::new(Body::empty());
        *response.status_mut() = StatusCode::NOT_MODIFIED;
        add_common_headers(response.headers_mut());
        response
            .headers_mut()
            .insert(ETAG, HeaderValue::from_str(&etag).unwrap());
        return response;
    }
    let range = match parse_range(request_headers.get(RANGE), audio.length) {
        Ok(range) => range,
        Err(message) => {
            let mut response = error_response(StatusCode::RANGE_NOT_SATISFIABLE, message, head);
            response.headers_mut().insert(
                CONTENT_RANGE,
                HeaderValue::from_str(&format!("bytes */{}", audio.length)).unwrap(),
            );
            return response;
        }
    };
    let (start, end) = range.unwrap_or((0, audio.length.saturating_sub(1)));
    let length = if audio.length == 0 {
        0
    } else {
        end - start + 1
    };
    let status = if range.is_some() {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    let bytes = if head {
        Bytes::new()
    } else {
        match state.asset_mode {
            AssetMode::Pack => match state.engine.bundle().audio_bytes(audio, start, length) {
                Ok(bytes) => bytes,
                Err(error) => {
                    return internal_error_response("serving audio", error, false);
                }
            },
            AssetMode::Files => {
                let root = state
                    .legacy_root
                    .as_ref()
                    .expect("validated at startup")
                    .clone();
                let bundle = state.engine.bundle().clone();
                match tokio::task::spawn_blocking(move || {
                    read_legacy_range(&bundle, &root, audio, start, length)
                })
                .await
                {
                    Ok(Ok(bytes)) => Bytes::from(bytes),
                    Ok(Err(error)) => {
                        return internal_error_response("reading audio from disk", error, false);
                    }
                    Err(error) => {
                        return internal_error_response("reading audio from disk", error, false);
                    }
                }
            }
        }
    };
    let mut response = Response::new(if head {
        Body::empty()
    } else {
        Body::from(bytes)
    });
    *response.status_mut() = status;
    let headers = response.headers_mut();
    add_common_headers(headers);
    headers.insert(CONTENT_TYPE, HeaderValue::from_static(audio.mime.as_str()));
    headers.insert(
        CONTENT_LENGTH,
        HeaderValue::from_str(&length.to_string()).unwrap(),
    );
    headers.insert(ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    headers.insert(ETAG, HeaderValue::from_str(&etag).unwrap());
    headers.insert(
        CACHE_CONTROL,
        HeaderValue::from_static(if immutable {
            "public, max-age=31536000, immutable"
        } else {
            "private, max-age=0, must-revalidate"
        }),
    );
    if range.is_some() {
        headers.insert(
            CONTENT_RANGE,
            HeaderValue::from_str(&format!("bytes {start}-{end}/{}", audio.length)).unwrap(),
        );
    }
    response
}

fn read_legacy_range(
    bundle: &Bundle,
    root: &Path,
    audio: AudioEntry,
    start: u64,
    length: u64,
) -> Result<Vec<u8>> {
    let path = bundle.legacy_audio_path(root, audio)?;
    let canonical = path
        .canonicalize()
        .with_context(|| format!("cannot resolve legacy audio {}", path.display()))?;
    ensure!(canonical.starts_with(root), "legacy audio escapes its root");
    let mut file = std::fs::File::open(&canonical)?;
    ensure!(
        file.metadata()?.len() == audio.length,
        "legacy audio length differs from bundle"
    );
    file.seek(SeekFrom::Start(start))?;
    let mut bytes =
        vec![0u8; usize::try_from(length).context("audio range exceeds address space")?];
    file.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn parse_range(
    header: Option<&HeaderValue>,
    total: u64,
) -> std::result::Result<Option<(u64, u64)>, &'static str> {
    let Some(header) = header else {
        return Ok(None);
    };
    if total == 0 {
        return Err("range cannot be applied to an empty asset");
    }
    let value = header.to_str().map_err(|_| "invalid Range header")?;
    let spec = value
        .strip_prefix("bytes=")
        .ok_or("only byte ranges are supported")?;
    if spec.contains(',') {
        return Err("multiple ranges are not supported");
    }
    let (left, right) = spec.split_once('-').ok_or("invalid byte range")?;
    let (start, end) = if left.is_empty() {
        let suffix: u64 = right.parse().map_err(|_| "invalid suffix range")?;
        if suffix == 0 {
            return Err("zero suffix range is invalid");
        }
        (total.saturating_sub(suffix), total - 1)
    } else {
        let start: u64 = left.parse().map_err(|_| "invalid range start")?;
        if start >= total {
            return Err("range starts beyond the asset");
        }
        let end = if right.is_empty() {
            total - 1
        } else {
            right
                .parse::<u64>()
                .map_err(|_| "invalid range end")?
                .min(total - 1)
        };
        if end < start {
            return Err("range end precedes start");
        }
        (start, end)
    };
    Ok(Some((start, end)))
}

async fn preflight() -> Response<Body> {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::NO_CONTENT;
    add_common_headers(response.headers_mut());
    response
        .headers_mut()
        .insert(ALLOW, HeaderValue::from_static("GET, HEAD, OPTIONS"));
    response.headers_mut().insert(
        ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static("GET, HEAD, OPTIONS"),
    );
    response.headers_mut().insert(
        ACCESS_CONTROL_ALLOW_HEADERS,
        HeaderValue::from_static("Range, If-None-Match, Content-Type"),
    );
    response
        .headers_mut()
        .insert(ACCESS_CONTROL_MAX_AGE, HeaderValue::from_static("86400"));
    response
}

async fn not_found(method: Method) -> Response<Body> {
    error_response(
        StatusCode::NOT_FOUND,
        "unknown request path; use / with a 'term' query, or \
         /v1/play, /v1/candidates, /v1/info, or /healthz",
        method == Method::HEAD,
    )
}

fn json_response<T: Serialize>(value: &T, head: bool) -> Response<Body> {
    match serde_json::to_vec(value) {
        Ok(bytes) => data_response(
            StatusCode::OK,
            "application/json; charset=utf-8",
            Bytes::from(bytes),
            head,
            false,
        ),
        Err(error) => error_response(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string(), head),
    }
}

fn error_response(status: StatusCode, message: &str, head: bool) -> Response<Body> {
    let bytes = Bytes::from(
        serde_json::to_vec(&serde_json::json!({"error": message}))
            .unwrap_or_else(|_| b"{\"error\":\"internal error\"}".to_vec()),
    );
    data_response(
        status,
        "application/json; charset=utf-8",
        bytes,
        head,
        false,
    )
}

/// Stable, task-oriented copy returned for any HTTP 500.
///
/// The precise engine/serialization/pack invariant is logged server-side by
/// [`internal_error_response`] and never reaches the client, so corrupt or
/// incompatible runtime data can no longer leak internal table/range/layout
/// detail as user copy. `{task}` names the user activity ("looking up audio",
/// "serving audio") so the message stays specific without exposing internals.
fn internal_error_message(task: &str) -> String {
    format!(
        "The audio server ran into an internal problem while {task}. Your audio \
         bundle may be incomplete or from an incompatible version. Rebuild the \
         audio bundle or restart the server, then try again."
    )
}

/// Log the raw error server-side and return a stable friendly 500 to the client.
///
/// Preserves the HTTP 500 status, JSON `{"error": ...}` body shape, JSON content
/// type, and HEAD (empty body) semantics of the previous `error_response(...,
/// &error.to_string(), ...)` call sites — only the *content* of the message
/// changes from a raw invariant to actionable copy. The full diagnostic is kept
/// on stderr for support.
fn internal_error_response(
    task: &str,
    error: impl std::fmt::Display,
    head: bool,
) -> Response<Body> {
    eprintln!("error: internal server error while {task}: {error}");
    error_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        &internal_error_message(task),
        head,
    )
}

/// Friendly, actionable copy for a failed loopback bind, keeping the raw cause.
///
/// The overwhelmingly common cause is the port already being in use by another
/// local server. Names the exact address/port and a concrete recovery, then
/// appends the raw OS error so logs and diagnostics keep the precise failure.
fn bind_failure_message(address: SocketAddr, error: impl std::fmt::Display) -> String {
    format!(
        "Could not start the audio server: the address {address} is not available. \
         Another program may already be using port {}. Stop the other local audio \
         server, or choose a free port with --port, then start it again.\n\
         Technical detail: {error}",
        address.port()
    )
}

fn data_response(
    status: StatusCode,
    content_type: &'static str,
    body: Bytes,
    head: bool,
    immutable: bool,
) -> Response<Body> {
    let length = body.len();
    let mut response = Response::new(if head {
        Body::empty()
    } else {
        Body::from(body)
    });
    *response.status_mut() = status;
    let headers = response.headers_mut();
    add_common_headers(headers);
    headers.insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    headers.insert(
        CONTENT_LENGTH,
        HeaderValue::from_str(&length.to_string()).unwrap(),
    );
    headers.insert(
        CACHE_CONTROL,
        HeaderValue::from_static(if immutable {
            "public, max-age=31536000, immutable"
        } else {
            "no-store"
        }),
    );
    response
}

fn add_common_headers(headers: &mut HeaderMap) {
    headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
    headers.insert(VARY, HeaderValue::from_static("Origin"));
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod http_error_tests {
    use super::*;

    async fn body_string(response: Response<Body>) -> (StatusCode, String, Option<String>) {
        let status = response.status();
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            String::from_utf8(bytes.to_vec()).unwrap(),
            content_type,
        )
    }

    fn raw_invariant() -> anyhow::Error {
        // A representative internal invariant that must NEVER reach the client:
        // the kind of low-level table/range/layout detail engine errors carry.
        anyhow::anyhow!("record 42 offset 9187 exceeds mmap pack length 4096")
    }

    #[tokio::test]
    async fn internal_error_response_hides_raw_invariant_and_stays_stable() {
        let response = internal_error_response("looking up audio", raw_invariant(), false);
        let (status, body, content_type) = body_string(response).await;
        // Protocol shape is preserved: 500 + JSON {error} + JSON content type.
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(
            content_type.as_deref(),
            Some("application/json; charset=utf-8")
        );
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        let message = parsed
            .get("error")
            .and_then(|value| value.as_str())
            .expect("error field is a string");
        // The raw internal invariant is not leaked to the user.
        assert!(
            !message.contains("mmap") && !message.contains("offset") && !message.contains("4096"),
            "raw invariant leaked into user copy: {message}"
        );
        // Stable, task-oriented recovery copy: names the task and a next action.
        let lowered = message.to_lowercase();
        assert!(lowered.contains("audio"), "{message}");
        assert!(
            lowered.contains("rebuild") || lowered.contains("restart"),
            "must suggest a recovery action: {message}"
        );
    }

    #[tokio::test]
    async fn internal_error_response_head_omits_body_but_keeps_status() {
        let response = internal_error_response("serving audio", raw_invariant(), true);
        let (status, body, _content_type) = body_string(response).await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
        // HEAD semantics preserved: no body bytes.
        assert!(body.is_empty(), "HEAD response must have an empty body");
    }

    #[test]
    fn bind_failure_message_is_actionable_and_keeps_raw_detail() {
        let cause = std::io::Error::new(
            std::io::ErrorKind::AddrInUse,
            "Address already in use (os error 98)",
        );
        let message =
            bind_failure_message(std::net::SocketAddr::from(([127, 0, 0, 1], 5050)), &cause);
        let lowered = message.to_lowercase();
        // Names the port and a concrete recovery, not a bare "cannot bind".
        assert!(lowered.contains("5050"), "{message}");
        assert!(lowered.contains("port"), "{message}");
        // Keeps the raw OS detail for diagnostics.
        assert!(message.contains("Address already in use"), "{message}");
    }
}
