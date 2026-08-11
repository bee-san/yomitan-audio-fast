use std::cmp::Ordering;
use std::fs::{self, File};
use std::hint::black_box;
use std::io::{BufReader, Read};
use std::path::Path;
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Result, ensure};
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags, params};
use serde::Serialize;
use xxhash_rust::xxh3::xxh3_64;

use crate::bundle::Bundle;
use crate::query::{LookupMode, QueryEngine, QueryInput};

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BenchmarkReport {
    generated_utc: String,
    server_version: &'static str,
    bundle_version: String,
    dataset: DatasetMetrics,
    index_open_milliseconds: f64,
    architectures: Vec<ArchitectureMetrics>,
    audio_storage: Vec<AudioMetrics>,
    pack_compiler_prefetch: Vec<PrefetchMetrics>,
    notes: Vec<&'static str>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DatasetMetrics {
    terms: u64,
    mapping_rows: u64,
    logical_audio_assets: u64,
    unique_audio_blobs: u64,
    repeated_path_references: u64,
    identical_content_assets: u64,
    distinct_speakers: u64,
    source_audio_bytes: u64,
    pack_bytes: u64,
    deduplicated_bytes: u64,
    compiler_milliseconds: u64,
    pack_build_milliseconds: u64,
    mph_build_milliseconds: u64,
    lookup_build_milliseconds: u64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ArchitectureMetrics {
    name: String,
    setup_milliseconds: f64,
    iterations: usize,
    total_milliseconds: f64,
    median_microseconds: f64,
    p95_microseconds: f64,
    p99_microseconds: f64,
    operations_per_second: f64,
    output_checksum: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AudioMetrics {
    name: &'static str,
    iterations: usize,
    total_milliseconds: f64,
    median_microseconds: f64,
    p95_microseconds: f64,
    operations_per_second: f64,
    touched_bytes: u64,
    output_checksum: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct PrefetchMetrics {
    workers: usize,
    files: usize,
    bytes: u64,
    total_milliseconds: f64,
    files_per_second: f64,
    mebibytes_per_second: f64,
    output_checksum: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CompatResponse {
    #[serde(rename = "type")]
    response_type: &'static str,
    audio_sources: Vec<CompatAudio>,
}

#[derive(Serialize)]
struct CompatAudio {
    name: String,
    url: String,
}

pub fn run(
    bundle_root: &Path,
    addon_root: &Path,
    output: &Path,
    iterations: usize,
    audio_iterations: usize,
) -> Result<()> {
    ensure!(iterations >= 100, "use at least 100 lookup iterations");
    ensure!(audio_iterations >= 10, "use at least 10 audio iterations");
    let open_started = Instant::now();
    let bundle = Arc::new(Bundle::open(bundle_root, true)?);
    let index_open_milliseconds = open_started.elapsed().as_secs_f64() * 1_000.0;
    let queries = sample_queries(&bundle, 2_048)?;
    ensure!(!queries.is_empty(), "bundle contains no query samples");
    let mut architectures = Vec::new();

    for (name, mode) in [
        ("mmap-sorted-xxh3-fanout", LookupMode::Sorted),
        ("mmap-chd-perfect-hash", LookupMode::Mph),
        ("preloaded-hashmap", LookupMode::Preload),
    ] {
        let setup = Instant::now();
        let engine = QueryEngine::new(bundle.clone(), mode)?;
        let setup_milliseconds = setup.elapsed().as_secs_f64() * 1_000.0;
        architectures.push(benchmark_engine(
            name,
            &engine,
            &queries,
            iterations,
            setup_milliseconds,
        )?);
    }

    let sqlite_path = addon_root.join("user_files").join("entries.db");
    let setup = Instant::now();
    let mut sqlite = Connection::open_with_flags(
        &sqlite_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    sqlite.pragma_update(None, "query_only", true)?;
    let sqlite_setup = setup.elapsed().as_secs_f64() * 1_000.0;
    architectures.push(benchmark_sqlite(
        &mut sqlite,
        &bundle,
        &queries,
        iterations,
        sqlite_setup,
    )?);

    // Differentially verify native candidates against the retained database before
    // publishing performance numbers. URLs differ by design; names/order/counts do not.
    verify_sqlite_parity(&mut sqlite, &bundle, &queries)?;
    let audio_storage = benchmark_audio(&bundle, addon_root, audio_iterations)?;
    let pack_compiler_prefetch = benchmark_pack_prefetch(
        &bundle,
        addon_root,
        audio_iterations.max(10_000).min(25_000),
    )?;
    let manifest = &bundle.manifest;
    let generated = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    let report = BenchmarkReport {
        generated_utc: format!("unix:{generated}"),
        server_version: env!("CARGO_PKG_VERSION"),
        bundle_version: manifest.bundle_version.clone(),
        dataset: DatasetMetrics {
            terms: manifest.term_count,
            mapping_rows: manifest.record_count,
            logical_audio_assets: manifest.audio_count,
            unique_audio_blobs: manifest.unique_blob_count,
            repeated_path_references: manifest.repeated_path_references,
            identical_content_assets: manifest.identical_content_assets,
            distinct_speakers: manifest.distinct_speaker_count,
            source_audio_bytes: manifest.source_audio_bytes,
            pack_bytes: manifest.pack_bytes,
            deduplicated_bytes: manifest.deduplicated_bytes,
            compiler_milliseconds: manifest.compile_milliseconds,
            pack_build_milliseconds: manifest.pack_build_milliseconds,
            mph_build_milliseconds: manifest.mph_build_milliseconds,
            lookup_build_milliseconds: manifest.lookup_build_milliseconds,
        },
        index_open_milliseconds,
        architectures,
        audio_storage,
        pack_compiler_prefetch,
        notes: vec![
            "Lookup timings include reading/source/user filtering, dynamic absolute URL construction, and JSON serialization.",
            "Lookup component cache is bypassed so index architecture remains measurable.",
            "Audio component timings are warm real-data operations; HTTP and concurrent results are recorded by scripts/benchmark-http.ps1.",
            "mmap-pack-view creates the exact zero-copy Bytes owner used by HTTP; individual-files opens and reads a separate file.",
            "Pack compiler prefetch compares the same warm sampled files with one and eight read+hash workers; full compile time is taken from the manifest.",
        ],
    };
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec_pretty(&report)?;
    fs::write(output, &bytes)?;
    println!("{}", String::from_utf8(bytes).expect("JSON is UTF-8"));
    Ok(())
}

fn benchmark_pack_prefetch(
    bundle: &Bundle,
    addon_root: &Path,
    count: usize,
) -> Result<Vec<PrefetchMetrics>> {
    let count = count.min(bundle.audio_count());
    let mut paths = Vec::with_capacity(count);
    for sample in 0..count {
        let id = sample * bundle.audio_count() / count;
        let audio = bundle.audio(id)?;
        paths.push(bundle.legacy_audio_path(addon_root, audio)?);
    }
    // Warm the exact sample once before comparing worker counts.
    for path in &paths {
        black_box(fs::read(path)?);
    }
    [1usize, 8usize]
        .into_iter()
        .map(|workers| {
            let pool = rayon::ThreadPoolBuilder::new()
                .num_threads(workers)
                .build()?;
            let started = Instant::now();
            let loaded = pool.install(|| {
                paths
                    .par_iter()
                    .map(|path| {
                        let bytes = fs::read(path)?;
                        let digest = blake3::hash(&bytes);
                        Ok::<_, std::io::Error>((bytes.len() as u64, *digest.as_bytes()))
                    })
                    .collect::<Vec<_>>()
            });
            let elapsed = started.elapsed();
            let mut bytes = 0u64;
            let mut checksum = 0u64;
            for (index, item) in loaded.into_iter().enumerate() {
                let (length, digest) = item?;
                bytes += length;
                checksum ^= u64::from_le_bytes(digest[0..8].try_into().unwrap())
                    .rotate_left((index % 63) as u32);
            }
            Ok(PrefetchMetrics {
                workers,
                files: count,
                bytes,
                total_milliseconds: elapsed.as_secs_f64() * 1_000.0,
                files_per_second: count as f64 / elapsed.as_secs_f64(),
                mebibytes_per_second: bytes as f64 / 1_048_576.0 / elapsed.as_secs_f64(),
                output_checksum: format!("{checksum:016x}"),
            })
        })
        .collect()
}

pub fn verify(bundle_root: &Path, full_pack_hash: bool) -> Result<()> {
    let bundle = Bundle::open(bundle_root, true)?;
    let lookup = bundle.root.join(&bundle.manifest.lookup_file);
    let lookup_hash = hash_file(&lookup)?;
    ensure!(
        lookup_hash == bundle.manifest.lookup_blake3,
        "lookup hash differs from manifest"
    );
    if full_pack_hash {
        let pack = bundle.root.join(&bundle.manifest.pack_file);
        let pack_hash = hash_file(&pack)?;
        ensure!(
            pack_hash == bundle.manifest.pack_blake3,
            "pack hash differs from manifest"
        );
    }
    println!(
        "verified bundle {}: {} terms, {} records, {} audio assets{}",
        bundle.manifest.bundle_version,
        bundle.manifest.term_count,
        bundle.manifest.record_count,
        bundle.manifest.audio_count,
        if full_pack_hash {
            ", lookup + full pack BLAKE3"
        } else {
            ", lookup BLAKE3 + pack bounds"
        }
    );
    Ok(())
}

pub fn export_corpus(bundle_root: &Path, output: &Path, count: usize) -> Result<()> {
    ensure!(count >= 32, "corpus should contain at least 32 queries");
    let bundle = Bundle::open(bundle_root, true)?;
    let queries = sample_queries(&bundle, count)?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let bytes = serde_json::to_vec_pretty(&queries)?;
    fs::write(output, &bytes)?;
    println!(
        "exported {} deterministic mixed queries to {}",
        queries.len(),
        output.display()
    );
    Ok(())
}

fn sample_queries(bundle: &Bundle, count: usize) -> Result<Vec<QueryInput>> {
    let count = count.min(bundle.term_count());
    let mut known_speaker = None;
    for index in 0..bundle.record_count() {
        let record = bundle.record(index)?;
        if let Some(speaker) = bundle.optional_string(record.speaker_offset, record.speaker_len)? {
            known_speaker = Some(speaker.to_owned());
            break;
        }
    }
    let mut output = Vec::with_capacity(count);
    for sample in 0..count {
        let index = sample * bundle.term_count() / count;
        let term = bundle.term(index)?;
        let mut expression = bundle.string(term.term_offset, term.term_len)?.to_owned();
        let first = bundle.record(term.first_record as usize)?;
        let reading = if sample % 2 == 0 {
            bundle
                .optional_string(first.reading_offset, first.reading_len)?
                .map(str::to_owned)
        } else {
            None
        };
        let sources = if sample % 5 == 0 {
            let first_source = &bundle.manifest.sources[first.source_index as usize].id;
            let mut requested = vec![first_source.clone()];
            if bundle.manifest.sources.len() > 1 {
                requested.push(
                    bundle.manifest.sources
                        [(first.source_index as usize + 1) % bundle.manifest.sources.len()]
                    .id
                    .clone(),
                );
            }
            Some(requested)
        } else {
            None
        };
        let users = if sample % 11 == 0 {
            known_speaker.iter().cloned().collect()
        } else {
            Vec::new()
        };
        if sample % 31 == 0 {
            expression.push('\u{10ffff}');
        }
        output.push(QueryInput {
            term: expression,
            reading,
            sources,
            users,
        });
    }
    Ok(output)
}

fn benchmark_engine(
    name: &str,
    engine: &QueryEngine,
    queries: &[QueryInput],
    iterations: usize,
    setup_milliseconds: f64,
) -> Result<ArchitectureMetrics> {
    for query in queries.iter().take(256) {
        black_box(engine_json(engine, query)?);
    }
    let mut latency = Vec::with_capacity(iterations);
    let mut checksum = 0u64;
    let total_started = Instant::now();
    for index in 0..iterations {
        let started = Instant::now();
        let body = engine_json(engine, &queries[index % queries.len()])?;
        latency.push(started.elapsed().as_nanos() as u64);
        checksum ^= xxh3_64(&body).rotate_left((index % 63) as u32);
        black_box(&body);
    }
    let elapsed = total_started.elapsed();
    Ok(metrics_from_latencies(
        name,
        setup_milliseconds,
        iterations,
        elapsed,
        latency,
        checksum,
    ))
}

fn engine_json(engine: &QueryEngine, query: &QueryInput) -> Result<Vec<u8>> {
    let audio_sources = engine
        .candidates(query)?
        .into_iter()
        .map(|candidate| {
            let response = engine.candidate_response(candidate, "http://127.0.0.1:5052");
            CompatAudio {
                name: response.name,
                url: response.url,
            }
        })
        .collect();
    Ok(serde_json::to_vec(&CompatResponse {
        response_type: "audioSourceList",
        audio_sources,
    })?)
}

#[derive(Clone)]
struct SqlCandidate {
    row_id: i64,
    reading: Option<String>,
    source_index: u16,
    speaker: Option<String>,
    name: String,
}

fn sqlite_candidates(
    connection: &mut Connection,
    bundle: &Bundle,
    query: &QueryInput,
) -> Result<Vec<SqlCandidate>> {
    let mut statement = connection.prepare_cached(
        "SELECT id, reading, source, speaker, display FROM entries \
         WHERE expression = ?1 AND (?2 = 0 OR reading IS NULL OR reading = ?3)",
    )?;
    let mut rows = statement.query(params![
        query.term,
        i64::from(query.reading.is_some()),
        query.reading.as_deref().unwrap_or("")
    ])?;
    let source_rank = requested_source_rank(bundle, query);
    let mut output = Vec::new();
    while let Some(row) = rows.next()? {
        let source_id: String = row.get(2)?;
        let Some(source_index) = bundle
            .manifest
            .sources
            .iter()
            .position(|source| source.id == source_id)
            .map(|value| value as u16)
        else {
            continue;
        };
        if !source_rank.iter().any(|(index, _)| *index == source_index) {
            continue;
        }
        let speaker: Option<String> = row.get(3)?;
        if !query.users.is_empty()
            && speaker
                .as_ref()
                .is_some_and(|value| !query.users.iter().any(|user| user == value))
        {
            continue;
        }
        let display: Option<String> = row.get(4)?;
        let template = &bundle.manifest.sources[source_index as usize].display;
        output.push(SqlCandidate {
            row_id: row.get(0)?,
            reading: row.get(1)?,
            source_index,
            speaker,
            name: display.map_or_else(|| template.clone(), |value| template.replace("%s", &value)),
        });
    }
    drop(rows);
    drop(statement);
    output.sort_by(|left, right| {
        rank_of(&source_rank, left.source_index)
            .cmp(&rank_of(&source_rank, right.source_index))
            .then_with(|| {
                sql_speaker_rank(left.speaker.as_deref(), &query.users)
                    .cmp(&sql_speaker_rank(right.speaker.as_deref(), &query.users))
            })
            .then_with(|| optional_cmp(left.reading.as_deref(), right.reading.as_deref()))
            .then(left.row_id.cmp(&right.row_id))
    });
    Ok(output)
}

fn benchmark_sqlite(
    connection: &mut Connection,
    bundle: &Bundle,
    queries: &[QueryInput],
    iterations: usize,
    setup_milliseconds: f64,
) -> Result<ArchitectureMetrics> {
    for query in queries.iter().take(256) {
        black_box(sqlite_json(connection, bundle, query)?);
    }
    let mut latency = Vec::with_capacity(iterations);
    let mut checksum = 0u64;
    let total_started = Instant::now();
    for index in 0..iterations {
        let started = Instant::now();
        let body = sqlite_json(connection, bundle, &queries[index % queries.len()])?;
        latency.push(started.elapsed().as_nanos() as u64);
        checksum ^= xxh3_64(&body).rotate_left((index % 63) as u32);
        black_box(&body);
    }
    let elapsed = total_started.elapsed();
    Ok(metrics_from_latencies(
        "retained-sqlite-hot",
        setup_milliseconds,
        iterations,
        elapsed,
        latency,
        checksum,
    ))
}

fn sqlite_json(
    connection: &mut Connection,
    bundle: &Bundle,
    query: &QueryInput,
) -> Result<Vec<u8>> {
    let audio_sources = sqlite_candidates(connection, bundle, query)?
        .into_iter()
        .map(|candidate| CompatAudio {
            name: candidate.name,
            url: format!("http://127.0.0.1:5052/v/legacy/audio/{}", candidate.row_id),
        })
        .collect();
    Ok(serde_json::to_vec(&CompatResponse {
        response_type: "audioSourceList",
        audio_sources,
    })?)
}

fn verify_sqlite_parity(
    connection: &mut Connection,
    bundle: &Arc<Bundle>,
    queries: &[QueryInput],
) -> Result<()> {
    let engine = QueryEngine::new(bundle.clone(), LookupMode::Mph)?;
    for query in queries {
        let native = engine
            .candidates(query)?
            .into_iter()
            .map(|candidate| {
                (
                    candidate.name.to_owned(),
                    candidate.source.to_owned(),
                    candidate.speaker.map(str::to_owned),
                    candidate.reading.map(str::to_owned),
                )
            })
            .collect::<Vec<_>>();
        let sql = sqlite_candidates(connection, bundle, query)?
            .into_iter()
            .map(|candidate| {
                (
                    candidate.name,
                    bundle.manifest.sources[candidate.source_index as usize]
                        .id
                        .clone(),
                    candidate.speaker,
                    candidate.reading,
                )
            })
            .collect::<Vec<_>>();
        ensure!(
            native == sql,
            "compiled/SQLite candidate mismatch for {:?}",
            query.term
        );
    }
    Ok(())
}

fn requested_source_rank(bundle: &Bundle, query: &QueryInput) -> Vec<(u16, usize)> {
    match &query.sources {
        None => (0..bundle.manifest.sources.len())
            .map(|index| (index as u16, index))
            .collect(),
        Some(ids) => ids
            .iter()
            .enumerate()
            .filter_map(|(rank, id)| {
                bundle
                    .manifest
                    .sources
                    .iter()
                    .position(|source| source.id == *id)
                    .map(|index| (index as u16, rank))
            })
            .collect(),
    }
}

fn rank_of(ranks: &[(u16, usize)], source: u16) -> usize {
    ranks
        .iter()
        .find_map(|(index, rank)| (*index == source).then_some(*rank))
        .unwrap_or(usize::MAX)
}

fn sql_speaker_rank(speaker: Option<&str>, users: &[String]) -> (u8, usize) {
    match speaker {
        None => (0, 0),
        Some(_) if users.is_empty() => (1, 0),
        Some(value) => (
            1,
            users
                .iter()
                .position(|user| user == value)
                .unwrap_or(usize::MAX),
        ),
    }
}

fn optional_cmp(left: Option<&str>, right: Option<&str>) -> Ordering {
    match (left, right) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Less,
        (Some(_), None) => Ordering::Greater,
        (Some(left), Some(right)) => left.cmp(right),
    }
}

fn benchmark_audio(
    bundle: &Bundle,
    addon_root: &Path,
    iterations: usize,
) -> Result<Vec<AudioMetrics>> {
    let ids = (0..iterations)
        .map(|index| index * bundle.audio_count() / iterations)
        .collect::<Vec<_>>();
    // Warm both representations once. This is explicitly a warm component benchmark.
    for &id in &ids {
        let audio = bundle.audio(id)?;
        let bytes = bundle.audio_bytes(audio, 0, audio.length)?;
        black_box(bytes.first().copied());
        let path = bundle.legacy_audio_path(addon_root, audio)?;
        black_box(fs::read(path)?);
    }

    let mut mmap_latency = Vec::with_capacity(iterations);
    let mut mmap_checksum = 0u64;
    let mut touched_bytes = 0u64;
    let total = Instant::now();
    for (iteration, &id) in ids.iter().enumerate() {
        let started = Instant::now();
        let audio = bundle.audio(id)?;
        let bytes = bundle.audio_bytes(audio, 0, audio.length)?;
        mmap_checksum ^= u64::from(bytes.first().copied().unwrap_or(0))
            | (u64::from(bytes.last().copied().unwrap_or(0)) << 8)
            | ((bytes.len() as u64).rotate_left((iteration % 63) as u32));
        touched_bytes += audio.length;
        black_box(&bytes);
        mmap_latency.push(started.elapsed().as_nanos() as u64);
    }
    let mmap_elapsed = total.elapsed();

    let mut file_latency = Vec::with_capacity(iterations);
    let mut file_checksum = 0u64;
    let total = Instant::now();
    for (iteration, &id) in ids.iter().enumerate() {
        let started = Instant::now();
        let audio = bundle.audio(id)?;
        let path = bundle.legacy_audio_path(addon_root, audio)?;
        let bytes = fs::read(path)?;
        file_checksum ^= u64::from(bytes.first().copied().unwrap_or(0))
            | (u64::from(bytes.last().copied().unwrap_or(0)) << 8)
            | ((bytes.len() as u64).rotate_left((iteration % 63) as u32));
        black_box(&bytes);
        file_latency.push(started.elapsed().as_nanos() as u64);
    }
    let file_elapsed = total.elapsed();

    Ok(vec![
        audio_metrics(
            "mmap-pack-zero-copy-view",
            iterations,
            mmap_elapsed,
            mmap_latency,
            touched_bytes,
            mmap_checksum,
        ),
        audio_metrics(
            "individual-file-open-read",
            iterations,
            file_elapsed,
            file_latency,
            touched_bytes,
            file_checksum,
        ),
    ])
}

fn metrics_from_latencies(
    name: &str,
    setup_milliseconds: f64,
    iterations: usize,
    elapsed: std::time::Duration,
    mut latency: Vec<u64>,
    checksum: u64,
) -> ArchitectureMetrics {
    latency.sort_unstable();
    ArchitectureMetrics {
        name: name.to_owned(),
        setup_milliseconds,
        iterations,
        total_milliseconds: elapsed.as_secs_f64() * 1_000.0,
        median_microseconds: percentile(&latency, 0.50) as f64 / 1_000.0,
        p95_microseconds: percentile(&latency, 0.95) as f64 / 1_000.0,
        p99_microseconds: percentile(&latency, 0.99) as f64 / 1_000.0,
        operations_per_second: iterations as f64 / elapsed.as_secs_f64(),
        output_checksum: format!("{checksum:016x}"),
    }
}

fn audio_metrics(
    name: &'static str,
    iterations: usize,
    elapsed: std::time::Duration,
    mut latency: Vec<u64>,
    touched_bytes: u64,
    checksum: u64,
) -> AudioMetrics {
    latency.sort_unstable();
    AudioMetrics {
        name,
        iterations,
        total_milliseconds: elapsed.as_secs_f64() * 1_000.0,
        median_microseconds: percentile(&latency, 0.50) as f64 / 1_000.0,
        p95_microseconds: percentile(&latency, 0.95) as f64 / 1_000.0,
        operations_per_second: iterations as f64 / elapsed.as_secs_f64(),
        touched_bytes,
        output_checksum: format!("{checksum:016x}"),
    }
}

fn percentile(values: &[u64], quantile: f64) -> u64 {
    let index = ((values.len() - 1) as f64 * quantile).round() as usize;
    values[index]
}

fn hash_file(path: &Path) -> Result<String> {
    let mut reader = BufReader::with_capacity(4 * 1024 * 1024, File::open(path)?);
    let mut hasher = blake3::Hasher::new();
    let mut buffer = vec![0u8; 4 * 1024 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hasher.finalize().to_hex().to_string())
}
