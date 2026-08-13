use std::cmp::Ordering;
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, ensure};
use hashbrown::HashMap;
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;
use serde_json::Value;
use xxhash_rust::xxh3::xxh3_64;

use crate::bundle::{
    AUDIO_LEN, BundleManifest, FANOUT_LEN, FORMAT_VERSION, HEADER_LEN, MAGIC, Mime, NONE_REF,
    RECORD_LEN, SourceManifest, TERM_LEN, align8, ensure_safe_relative,
};

#[derive(Debug, Clone)]
pub struct CompileOptions {
    pub addon_root: PathBuf,
    pub output: PathBuf,
    pub deduplicate: bool,
    pub pack_workers: usize,
}

#[derive(Debug, Deserialize)]
struct Config {
    sources: Vec<ConfigSource>,
}

#[derive(Debug, Clone, Deserialize)]
struct ConfigSource {
    id: String,
    path: String,
    display: String,
}

#[derive(Debug, Clone)]
struct AssetBuild {
    source_index: u16,
    relative_path: String,
    absolute_path: PathBuf,
    pack_offset: u64,
    length: u64,
    content_hash_prefix: u64,
    mime: Mime,
}

#[derive(Debug)]
struct EntryBuild {
    row_id: i64,
    reading: Option<String>,
    source_index: u16,
    speaker: Option<String>,
    name: String,
    audio_id: u32,
}

#[derive(Debug, Clone, Copy)]
struct StringRef {
    offset: u32,
    len: u32,
}

#[derive(Debug, Clone, Copy)]
struct TermBuild {
    hash: u64,
    term: StringRef,
    first_record: u32,
    record_count: u32,
}

struct MphBuild {
    displacements: Vec<i32>,
    slots: Vec<TermBuild>,
}

#[derive(Default)]
struct StringTable {
    bytes: Vec<u8>,
    refs: HashMap<String, StringRef>,
}

impl StringTable {
    fn intern(&mut self, value: &str) -> Result<StringRef> {
        if let Some(existing) = self.refs.get(value) {
            return Ok(*existing);
        }
        let offset = u32::try_from(self.bytes.len()).context("string table exceeds 4 GiB")?;
        let len = u32::try_from(value.len()).context("one string exceeds 4 GiB")?;
        self.bytes.extend_from_slice(value.as_bytes());
        let reference = StringRef { offset, len };
        self.refs.insert(value.to_owned(), reference);
        Ok(reference)
    }

    fn optional(&mut self, value: Option<&str>) -> Result<StringRef> {
        match value {
            Some(value) => self.intern(value),
            None => Ok(StringRef {
                offset: NONE_REF,
                len: 0,
            }),
        }
    }
}

pub fn compile(options: &CompileOptions) -> Result<BundleManifest> {
    let started = Instant::now();
    let addon_root = options.addon_root.canonicalize().with_context(|| {
        format!(
            "cannot resolve add-on root {}",
            options.addon_root.display()
        )
    })?;
    let output = if options.output.is_absolute() {
        options.output.clone()
    } else {
        std::env::current_dir()?.join(&options.output)
    };
    fs::create_dir_all(&output)
        .with_context(|| format!("cannot create bundle output {}", output.display()))?;
    fs::create_dir_all(output.join("versions"))?;

    let sources = load_config(&addon_root)?;
    ensure!(!sources.is_empty(), "configuration has no sources");
    ensure!(sources.len() <= u16::MAX as usize, "too many sources");
    let source_lookup: HashMap<&str, u16> = sources
        .iter()
        .enumerate()
        .map(|(index, source)| (source.id.as_str(), index as u16))
        .collect();
    let db_path = addon_root.join("user_files").join("entries.db");
    let connection = Connection::open_with_flags(
        &db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("cannot open copied legacy database {}", db_path.display()))?;
    connection.pragma_update(None, "query_only", true)?;
    connection.busy_timeout(std::time::Duration::from_secs(5))?;
    let integrity: String = connection.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
    ensure!(
        integrity == "ok",
        "legacy entries.db quick_check failed: {integrity}"
    );

    let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let temp_dir = output.join(format!(".build-{}-{stamp}", std::process::id()));
    ensure!(
        !temp_dir.exists(),
        "temporary build directory already exists"
    );
    fs::create_dir(&temp_dir)?;
    let build_result = compile_inner(
        &connection,
        &addon_root,
        &sources,
        &source_lookup,
        &temp_dir,
        options.deduplicate,
        options.pack_workers,
        started,
    );
    let mut manifest = match build_result {
        Ok(manifest) => manifest,
        Err(error) => {
            let _ = fs::remove_dir_all(&temp_dir);
            return Err(error);
        }
    };

    let version_dir = output.join("versions").join(&manifest.bundle_version);
    if version_dir.exists() {
        let existing_lookup = version_dir.join("lookup.bin");
        let existing_pack = version_dir.join("audio.pack");
        let same = existing_lookup.is_file()
            && existing_pack.is_file()
            && blake3_file(&existing_lookup)? == manifest.lookup_blake3
            && blake3_file(&existing_pack)? == manifest.pack_blake3;
        ensure!(
            same,
            "bundle version directory exists but its content differs"
        );
        fs::remove_dir_all(&temp_dir)?;
    } else {
        fs::rename(&temp_dir, &version_dir).with_context(|| {
            format!(
                "cannot publish bundle version {} -> {}",
                temp_dir.display(),
                version_dir.display()
            )
        })?;
    }

    manifest.lookup_file = slash_path(
        Path::new("versions")
            .join(&manifest.bundle_version)
            .join("lookup.bin"),
    );
    manifest.pack_file = slash_path(
        Path::new("versions")
            .join(&manifest.bundle_version)
            .join("audio.pack"),
    );
    manifest.compile_milliseconds = started.elapsed().as_millis() as u64;
    publish_manifest(&output, &manifest)?;
    Ok(manifest)
}

#[allow(clippy::too_many_arguments)]
fn compile_inner(
    connection: &Connection,
    addon_root: &Path,
    sources: &[ConfigSource],
    source_lookup: &HashMap<&str, u16>,
    temp_dir: &Path,
    deduplicate: bool,
    pack_workers: usize,
    started: Instant,
) -> Result<BundleManifest> {
    eprintln!("[compile] reading distinct audio references from entries.db");
    let mut assets = collect_assets(connection, addon_root, sources, source_lookup)?;
    let mut asset_ids = HashMap::with_capacity(assets.len());
    for (index, asset) in assets.iter().enumerate() {
        let key = asset_key(asset.source_index, &asset.relative_path);
        ensure!(
            asset_ids.insert(key, index as u32).is_none(),
            "duplicate logical asset"
        );
    }

    eprintln!("[compile] packing {} logical audio assets", assets.len());
    let pack_path = temp_dir.join("audio.pack");
    let pack_started = Instant::now();
    let pack_metrics = write_pack(&pack_path, &mut assets, deduplicate, pack_workers)?;
    let pack_build_milliseconds = pack_started.elapsed().as_millis() as u64;
    eprintln!(
        "[compile] pack {:.2} GiB, deduplicated {:.2} MiB, {} unique blobs in {:.1}s",
        pack_metrics.pack_bytes as f64 / 1_073_741_824.0,
        pack_metrics.deduplicated_bytes as f64 / 1_048_576.0,
        pack_metrics.unique_blob_count,
        started.elapsed().as_secs_f64()
    );

    eprintln!("[compile] compiling lookup records");
    let lookup_started = Instant::now();
    let mut strings = StringTable::default();
    let mut terms = Vec::<TermBuild>::new();
    let mut record_bytes = Vec::<u8>::new();
    collect_records(
        connection,
        sources,
        source_lookup,
        &asset_ids,
        &mut strings,
        &mut terms,
        &mut record_bytes,
    )?;
    terms.sort_unstable_by(|left, right| {
        left.hash
            .cmp(&right.hash)
            .then(left.term.offset.cmp(&right.term.offset))
    });
    let fanout = make_fanout(&terms);
    eprintln!("[compile] constructing collision-safe CHD candidate");
    let mph_started = Instant::now();
    let mph = build_mph(&terms, &strings.bytes)?;
    let mph_build_milliseconds = mph_started.elapsed().as_millis() as u64;
    let audio_bytes = serialize_audio_table(&assets, &mut strings)?;
    ensure!(
        record_bytes.len() % RECORD_LEN == 0,
        "record serialization width mismatch"
    );
    ensure!(
        audio_bytes.len() % AUDIO_LEN == 0,
        "audio serialization width mismatch"
    );

    let lookup_path = temp_dir.join("lookup.bin");
    write_lookup(
        &lookup_path,
        &fanout,
        &terms,
        &mph,
        &record_bytes,
        &audio_bytes,
        &strings.bytes,
    )?;
    let lookup_build_milliseconds = lookup_started.elapsed().as_millis() as u64;
    let lookup_blake3 = blake3_file(&lookup_path)?;
    let pack_blake3 = blake3_file(&pack_path)?;
    let version_digest = blake3::hash(format!("{lookup_blake3}:{pack_blake3}").as_bytes());
    let bundle_version = version_digest.to_hex()[..16].to_string();
    let record_count = (record_bytes.len() / RECORD_LEN) as u64;
    let distinct_speaker_count: u64 = connection.query_row(
        "SELECT COUNT(DISTINCT speaker) FROM entries WHERE speaker IS NOT NULL",
        [],
        |row| row.get(0),
    )?;

    let created = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
    Ok(BundleManifest {
        format_version: FORMAT_VERSION,
        bundle_version,
        lookup_file: "lookup.bin".to_owned(),
        pack_file: "audio.pack".to_owned(),
        lookup_blake3,
        pack_blake3,
        created_utc: format!("unix:{created}"),
        compiler_version: env!("CARGO_PKG_VERSION").to_owned(),
        term_count: terms.len() as u64,
        record_count,
        audio_count: assets.len() as u64,
        unique_blob_count: pack_metrics.unique_blob_count,
        pack_bytes: pack_metrics.pack_bytes,
        source_audio_bytes: pack_metrics.source_audio_bytes,
        deduplicated_bytes: pack_metrics.deduplicated_bytes,
        repeated_path_references: record_count.saturating_sub(assets.len() as u64),
        identical_content_assets: assets.len() as u64 - pack_metrics.unique_blob_count,
        distinct_speaker_count,
        pack_build_milliseconds,
        mph_build_milliseconds,
        lookup_build_milliseconds,
        compile_milliseconds: 0,
        sources: sources
            .iter()
            .map(|source| SourceManifest {
                id: source.id.clone(),
                display: source.display.clone(),
                legacy_media_dir: source.path.clone(),
            })
            .collect(),
    })
}

/// Mirrors `config.merge_sources` in the add-on: user entries win by `id`, but defaults
/// missing from a pinned user list are appended, so both runtimes see the same sources.
fn merge_sources(default_sources: &Value, user_sources: Option<&Value>) -> Value {
    let Some(Value::Array(user)) = user_sources else {
        return default_sources.clone();
    };
    let Value::Array(defaults) = default_sources else {
        return Value::Array(user.clone());
    };
    let mut merged = user.clone();
    for item in defaults {
        let id = item.get("id").and_then(Value::as_str);
        if !merged
            .iter()
            .any(|existing| existing.get("id").and_then(Value::as_str) == id)
        {
            merged.push(item.clone());
        }
    }
    Value::Array(merged)
}

fn load_config(addon_root: &Path) -> Result<Vec<ConfigSource>> {
    let default_path = addon_root.join("default_config.json");
    let mut value: Value = serde_json::from_reader(
        File::open(&default_path)
            .with_context(|| format!("cannot open {}", default_path.display()))?,
    )?;
    let override_path = addon_root.join("user_files").join("config.json");
    if override_path.is_file() {
        let override_value: Value = serde_json::from_reader(File::open(&override_path)?)?;
        let default_sources = value.get("sources").cloned();
        let base = value
            .as_object_mut()
            .ok_or_else(|| anyhow!("default config must be an object"))?;
        let replacement = override_value
            .as_object()
            .ok_or_else(|| anyhow!("user config must be an object"))?;
        for (key, item) in replacement {
            base.insert(key.clone(), item.clone());
        }
        if let Some(defaults) = default_sources {
            let merged = merge_sources(&defaults, base.get("sources"));
            base.insert("sources".to_string(), merged);
        }
    }
    let config: Config = serde_json::from_value(value)?;
    let mut seen = HashMap::<String, ()>::new();
    for source in &config.sources {
        ensure!(!source.id.is_empty(), "source ID cannot be empty");
        ensure!(
            source
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
            "source ID {:?} contains unsafe characters",
            source.id
        );
        ensure!(
            seen.insert(source.id.clone(), ()).is_none(),
            "duplicate source ID {}",
            source.id
        );
        ensure_safe_relative(Path::new(&source.path))?;
    }
    Ok(config.sources)
}

fn collect_assets(
    connection: &Connection,
    addon_root: &Path,
    sources: &[ConfigSource],
    source_lookup: &HashMap<&str, u16>,
) -> Result<Vec<AssetBuild>> {
    let mut statement = connection
        .prepare("SELECT source, file FROM entries GROUP BY source, file ORDER BY source, file")?;
    let mut rows = statement.query([])?;
    let mut assets = Vec::new();
    while let Some(row) = rows.next()? {
        let source_id: String = row.get(0)?;
        let relative_path: String = row.get(1)?;
        let source_index = *source_lookup
            .get(source_id.as_str())
            .ok_or_else(|| anyhow!("entries.db references unconfigured source {source_id:?}"))?;
        ensure_safe_relative(Path::new(&relative_path))?;
        let media = addon_root.join(&sources[source_index as usize].path);
        let absolute_path = media.join(&relative_path);
        let mime = Mime::from_extension(&absolute_path)
            .ok_or_else(|| anyhow!("unsupported audio extension: {}", absolute_path.display()))?;
        assets.push(AssetBuild {
            source_index,
            relative_path,
            absolute_path,
            pack_offset: 0,
            length: 0,
            content_hash_prefix: 0,
            mime,
        });
    }
    ensure!(
        assets.len() <= u32::MAX as usize,
        "more than 2^32 audio assets"
    );
    Ok(assets)
}

#[derive(Debug)]
struct PackMetrics {
    pack_bytes: u64,
    source_audio_bytes: u64,
    deduplicated_bytes: u64,
    unique_blob_count: u64,
}

fn write_pack(
    path: &Path,
    assets: &mut [AssetBuild],
    deduplicate: bool,
    pack_workers: usize,
) -> Result<PackMetrics> {
    ensure!(
        pack_workers > 0 && pack_workers <= 64,
        "pack workers must be between 1 and 64"
    );
    let file = File::create(path)?;
    let mut writer = BufWriter::with_capacity(4 * 1024 * 1024, file);
    let mut pack_hasher = blake3::Hasher::new();
    let mut blobs = HashMap::<[u8; 32], (u64, u64, PathBuf)>::with_capacity(assets.len());
    let mut offset = 0u64;
    let mut source_audio_bytes = 0u64;
    let mut deduplicated_bytes = 0u64;
    let mut unique_blob_count = 0u64;
    let asset_count = assets.len();
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(pack_workers)
        .thread_name(|index| format!("audio-pack-{index}"))
        .build()?;
    let mut processed = 0usize;
    for chunk in assets.chunks_mut(1024) {
        let loaded = pool.install(|| {
            chunk
                .par_iter()
                .map(|asset| {
                    let data = fs::read(&asset.absolute_path).with_context(|| {
                        format!("cannot read {}", asset.absolute_path.display())
                    })?;
                    let digest = *blake3::hash(&data).as_bytes();
                    Ok::<_, anyhow::Error>((data, digest))
                })
                .collect::<Vec<_>>()
        });
        for (asset, loaded) in chunk.iter_mut().zip(loaded) {
            let (data, digest_bytes) = loaded?;
            let length = data.len() as u64;
            asset.length = length;
            source_audio_bytes = source_audio_bytes
                .checked_add(length)
                .ok_or_else(|| anyhow!("source audio byte count overflow"))?;
            asset.content_hash_prefix = u64::from_le_bytes(digest_bytes[0..8].try_into().unwrap());
            if deduplicate {
                if let Some((existing_offset, existing_length, existing_path)) =
                    blobs.get(&digest_bytes)
                {
                    if *existing_length == length {
                        // BLAKE3 chooses the candidate; an exact byte comparison makes
                        // coalescing correct even under a hypothetical digest collision.
                        let existing = fs::read(existing_path).with_context(|| {
                            format!(
                                "cannot re-read dedup representative {}",
                                existing_path.display()
                            )
                        })?;
                        if existing == data {
                            asset.pack_offset = *existing_offset;
                            deduplicated_bytes += length;
                            processed += 1;
                            continue;
                        }
                    }
                }
            }
            asset.pack_offset = offset;
            writer.write_all(&data)?;
            pack_hasher.update(&data);
            blobs
                .entry(digest_bytes)
                .or_insert_with(|| (offset, length, asset.absolute_path.clone()));
            offset = offset
                .checked_add(length)
                .ok_or_else(|| anyhow!("audio pack exceeds u64"))?;
            unique_blob_count += 1;
            processed += 1;
        }
        if processed > 0 && processed % 50_000 < 1024 {
            eprintln!(
                "[compile] packed {processed}/{asset_count} assets ({:.2} GiB)",
                offset as f64 / 1_073_741_824.0
            );
        }
    }
    writer.flush()?;
    writer.get_ref().sync_all()?;
    // `blake3_file` is the authoritative published digest. The incremental hasher
    // deliberately remains here as a cheap guard against future pack-writer changes.
    let incremental = pack_hasher.finalize().to_hex().to_string();
    let on_disk = blake3_file(path)?;
    ensure!(incremental == on_disk, "pack digest mismatch after write");
    Ok(PackMetrics {
        pack_bytes: offset,
        source_audio_bytes,
        deduplicated_bytes,
        unique_blob_count,
    })
}

#[allow(clippy::too_many_arguments)]
fn collect_records(
    connection: &Connection,
    sources: &[ConfigSource],
    source_lookup: &HashMap<&str, u16>,
    asset_ids: &HashMap<String, u32>,
    strings: &mut StringTable,
    terms: &mut Vec<TermBuild>,
    record_bytes: &mut Vec<u8>,
) -> Result<()> {
    let mut statement = connection.prepare(
        "SELECT id, expression, reading, source, speaker, display, file FROM entries ORDER BY expression, id",
    )?;
    let mut rows = statement.query([])?;
    let mut active_term: Option<String> = None;
    let mut active_rows = Vec::<EntryBuild>::new();
    while let Some(row) = rows.next()? {
        let expression: String = row.get(1)?;
        if active_term.as_deref() != Some(expression.as_str()) {
            if let Some(previous) = active_term.take() {
                flush_term(&previous, &mut active_rows, strings, terms, record_bytes)?;
            }
            active_term = Some(expression.clone());
        }
        let source_id: String = row.get(3)?;
        let source_index = *source_lookup
            .get(source_id.as_str())
            .ok_or_else(|| anyhow!("entries.db references unconfigured source {source_id:?}"))?;
        let file: String = row.get(6)?;
        let audio_id = *asset_ids
            .get(&asset_key(source_index, &file))
            .ok_or_else(|| anyhow!("entry references unknown audio {source_id}/{file}"))?;
        let display: Option<String> = row.get(5)?;
        let template = &sources[source_index as usize].display;
        let name = match display {
            Some(display) => template.replace("%s", &display),
            None => template.clone(),
        };
        active_rows.push(EntryBuild {
            row_id: row.get(0)?,
            reading: row.get(2)?,
            source_index,
            speaker: row.get(4)?,
            name,
            audio_id,
        });
    }
    if let Some(previous) = active_term {
        flush_term(&previous, &mut active_rows, strings, terms, record_bytes)?;
    }
    ensure!(terms.len() <= u32::MAX as usize, "more than 2^32 terms");
    Ok(())
}

fn flush_term(
    term: &str,
    rows: &mut Vec<EntryBuild>,
    strings: &mut StringTable,
    terms: &mut Vec<TermBuild>,
    record_bytes: &mut Vec<u8>,
) -> Result<()> {
    rows.sort_by(|left, right| {
        left.source_index
            .cmp(&right.source_index)
            .then_with(|| option_sqlite_cmp(left.reading.as_deref(), right.reading.as_deref()))
            .then(left.row_id.cmp(&right.row_id))
    });
    let first_record =
        u32::try_from(record_bytes.len() / RECORD_LEN).context("more than 2^32 lookup records")?;
    for row in rows.iter() {
        let reading = strings.optional(row.reading.as_deref())?;
        let speaker = strings.optional(row.speaker.as_deref())?;
        let name = strings.intern(&row.name)?;
        put_u32(record_bytes, reading.offset);
        put_u32(record_bytes, reading.len);
        put_u32(record_bytes, speaker.offset);
        put_u32(record_bytes, speaker.len);
        put_u32(record_bytes, name.offset);
        put_u32(record_bytes, name.len);
        put_u32(record_bytes, row.audio_id);
        put_u16(record_bytes, row.source_index);
        put_u16(record_bytes, 0);
    }
    let term_ref = strings.intern(term)?;
    terms.push(TermBuild {
        hash: xxh3_64(term.as_bytes()),
        term: term_ref,
        first_record,
        record_count: rows.len() as u32,
    });
    rows.clear();
    Ok(())
}

fn option_sqlite_cmp(left: Option<&str>, right: Option<&str>) -> Ordering {
    match (left, right) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Less,
        (Some(_), None) => Ordering::Greater,
        (Some(left), Some(right)) => left.cmp(right),
    }
}

fn make_fanout(terms: &[TermBuild]) -> Vec<u32> {
    let mut fanout = vec![0u32; FANOUT_LEN];
    let mut term_index = 0usize;
    for (bucket, item) in fanout.iter_mut().enumerate() {
        while term_index < terms.len() && ((terms[term_index].hash >> 48) as usize) < bucket {
            term_index += 1;
        }
        *item = term_index as u32;
    }
    fanout
}

fn build_mph(terms: &[TermBuild], strings: &[u8]) -> Result<MphBuild> {
    if terms.is_empty() {
        return Ok(MphBuild {
            displacements: Vec::new(),
            slots: Vec::new(),
        });
    }
    ensure!(
        terms.len() <= i32::MAX as usize,
        "CHD candidate supports at most i32::MAX terms"
    );
    let bucket_count = terms.len().div_ceil(4).max(1);
    let mut buckets = vec![Vec::<usize>::new(); bucket_count];
    for (index, term) in terms.iter().enumerate() {
        buckets[(term.hash % bucket_count as u64) as usize].push(index);
    }
    let mut order: Vec<usize> = (0..bucket_count).collect();
    order.sort_unstable_by_key(|&index| std::cmp::Reverse(buckets[index].len()));
    let mut used = vec![false; terms.len()];
    let mut slots = vec![None::<TermBuild>; terms.len()];
    let mut displacements = vec![i32::MIN; bucket_count];

    for &bucket_index in &order {
        let bucket = &buckets[bucket_index];
        if bucket.len() <= 1 {
            continue;
        }
        let mut displacement = 0u32;
        let chosen = loop {
            let mut candidate_slots = Vec::with_capacity(bucket.len());
            let mut valid = true;
            for &term_index in bucket {
                let term = terms[term_index];
                let key = string_bytes(strings, term.term)?;
                let slot = (xxhash_rust::xxh3::xxh3_64_with_seed(key, displacement as u64)
                    % terms.len() as u64) as usize;
                if used[slot] || candidate_slots.contains(&slot) {
                    valid = false;
                    break;
                }
                candidate_slots.push(slot);
            }
            if valid {
                break candidate_slots;
            }
            displacement = displacement
                .checked_add(1)
                .ok_or_else(|| anyhow!("CHD displacement overflow"))?;
            ensure!(
                displacement <= i32::MAX as u32,
                "CHD displacement exceeds i32"
            );
        };
        displacements[bucket_index] = displacement as i32;
        for (&term_index, slot) in bucket.iter().zip(chosen) {
            used[slot] = true;
            slots[slot] = Some(terms[term_index]);
        }
    }

    let free_slots: Vec<usize> = used
        .iter()
        .enumerate()
        .filter_map(|(index, occupied)| (!occupied).then_some(index))
        .collect();
    let mut free_slots = free_slots.into_iter();
    for &bucket_index in &order {
        let bucket = &buckets[bucket_index];
        if bucket.len() != 1 {
            continue;
        }
        let slot = free_slots
            .next()
            .ok_or_else(|| anyhow!("CHD ran out of singleton slots"))?;
        used[slot] = true;
        slots[slot] = Some(terms[bucket[0]]);
        displacements[bucket_index] = -((slot as i32) + 1);
    }
    ensure!(
        used.iter().all(|item| *item),
        "CHD did not fill every term slot"
    );
    let slots = slots
        .into_iter()
        .map(|item| item.ok_or_else(|| anyhow!("CHD slot is empty")))
        .collect::<Result<Vec<_>>>()?;

    // Validate the generated function and collision-safe key verification before publish.
    for term in terms {
        let key = string_bytes(strings, term.term)?;
        let bucket = (term.hash % bucket_count as u64) as usize;
        let displacement = displacements[bucket];
        let slot = if displacement < 0 {
            (-i64::from(displacement) - 1) as usize
        } else {
            (xxhash_rust::xxh3::xxh3_64_with_seed(key, displacement as u64) % terms.len() as u64)
                as usize
        };
        let resolved = slots[slot];
        ensure!(
            resolved.hash == term.hash && string_bytes(strings, resolved.term)? == key,
            "CHD validation failed"
        );
    }
    Ok(MphBuild {
        displacements,
        slots,
    })
}

fn string_bytes(strings: &[u8], reference: StringRef) -> Result<&[u8]> {
    let start = reference.offset as usize;
    let end = start
        .checked_add(reference.len as usize)
        .ok_or_else(|| anyhow!("string reference overflow"))?;
    strings
        .get(start..end)
        .ok_or_else(|| anyhow!("string reference outside compiler table"))
}

fn serialize_audio_table(assets: &[AssetBuild], strings: &mut StringTable) -> Result<Vec<u8>> {
    let capacity = assets
        .len()
        .checked_mul(AUDIO_LEN)
        .ok_or_else(|| anyhow!("audio table size overflow"))?;
    let mut bytes = Vec::with_capacity(capacity);
    for asset in assets {
        let path = strings.intern(&asset.relative_path)?;
        put_u64(&mut bytes, asset.pack_offset);
        put_u64(&mut bytes, asset.length);
        put_u32(&mut bytes, path.offset);
        put_u32(&mut bytes, path.len);
        put_u64(&mut bytes, asset.content_hash_prefix);
        put_u16(&mut bytes, asset.source_index);
        bytes.push(asset.mime as u8);
        bytes.extend_from_slice(&[0u8; 5]);
    }
    Ok(bytes)
}

fn write_lookup(
    path: &Path,
    fanout: &[u32],
    terms: &[TermBuild],
    mph: &MphBuild,
    records: &[u8],
    audio: &[u8],
    strings: &[u8],
) -> Result<()> {
    ensure!(fanout.len() == FANOUT_LEN, "fanout width mismatch");
    let fanout_offset = HEADER_LEN;
    let terms_offset = align8(fanout_offset + FANOUT_LEN * 4);
    let mph_buckets_offset = align8(terms_offset + terms.len() * TERM_LEN);
    let mph_terms_offset = align8(mph_buckets_offset + mph.displacements.len() * 4);
    let records_offset = align8(mph_terms_offset + mph.slots.len() * TERM_LEN);
    let audio_offset = align8(records_offset + records.len());
    let strings_offset = align8(audio_offset + audio.len());
    let total = strings_offset
        .checked_add(strings.len())
        .ok_or_else(|| anyhow!("lookup file size overflow"))?;
    let mut output = vec![0u8; total];
    output[0..8].copy_from_slice(MAGIC);
    write_u32(&mut output, 8, FORMAT_VERSION)?;
    write_u32(&mut output, 12, HEADER_LEN as u32)?;
    write_u64(&mut output, 16, terms.len() as u64)?;
    write_u64(&mut output, 24, (records.len() / RECORD_LEN) as u64)?;
    write_u64(&mut output, 32, (audio.len() / AUDIO_LEN) as u64)?;
    write_u64(&mut output, 40, fanout_offset as u64)?;
    write_u64(&mut output, 48, terms_offset as u64)?;
    write_u64(&mut output, 56, records_offset as u64)?;
    write_u64(&mut output, 64, audio_offset as u64)?;
    write_u64(&mut output, 72, strings_offset as u64)?;
    write_u64(&mut output, 80, strings.len() as u64)?;
    write_u64(&mut output, 88, mph.displacements.len() as u64)?;
    write_u64(&mut output, 96, mph_buckets_offset as u64)?;
    write_u64(&mut output, 104, mph_terms_offset as u64)?;

    let mut cursor = fanout_offset;
    for value in fanout {
        output[cursor..cursor + 4].copy_from_slice(&value.to_le_bytes());
        cursor += 4;
    }
    cursor = terms_offset;
    for term in terms {
        output[cursor..cursor + 8].copy_from_slice(&term.hash.to_le_bytes());
        output[cursor + 8..cursor + 12].copy_from_slice(&term.term.offset.to_le_bytes());
        output[cursor + 12..cursor + 16].copy_from_slice(&term.term.len.to_le_bytes());
        output[cursor + 16..cursor + 20].copy_from_slice(&term.first_record.to_le_bytes());
        output[cursor + 20..cursor + 24].copy_from_slice(&term.record_count.to_le_bytes());
        cursor += TERM_LEN;
    }
    cursor = mph_buckets_offset;
    for displacement in &mph.displacements {
        output[cursor..cursor + 4].copy_from_slice(&displacement.to_le_bytes());
        cursor += 4;
    }
    cursor = mph_terms_offset;
    for term in &mph.slots {
        output[cursor..cursor + 8].copy_from_slice(&term.hash.to_le_bytes());
        output[cursor + 8..cursor + 12].copy_from_slice(&term.term.offset.to_le_bytes());
        output[cursor + 12..cursor + 16].copy_from_slice(&term.term.len.to_le_bytes());
        output[cursor + 16..cursor + 20].copy_from_slice(&term.first_record.to_le_bytes());
        output[cursor + 20..cursor + 24].copy_from_slice(&term.record_count.to_le_bytes());
        cursor += TERM_LEN;
    }
    output[records_offset..records_offset + records.len()].copy_from_slice(records);
    output[audio_offset..audio_offset + audio.len()].copy_from_slice(audio);
    output[strings_offset..strings_offset + strings.len()].copy_from_slice(strings);
    let checksum = blake3::hash(&output[HEADER_LEN..]);
    output[120..152].copy_from_slice(checksum.as_bytes());

    let mut writer = BufWriter::with_capacity(4 * 1024 * 1024, File::create(path)?);
    writer.write_all(&output)?;
    writer.flush()?;
    writer.get_ref().sync_all()?;
    Ok(())
}

fn publish_manifest(output: &Path, manifest: &BundleManifest) -> Result<()> {
    let temp = output.join(format!("manifest.json.tmp.{}", std::process::id()));
    let mut file = File::create(&temp)?;
    serde_json::to_writer_pretty(&mut file, manifest)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    let destination = output.join("manifest.json");
    if destination.exists() {
        // Versioned data files remain immutable. This tiny replacement window only
        // affects repeated compilation; a running server already owns its old mappings.
        fs::remove_file(&destination)?;
    }
    fs::rename(&temp, &destination)?;
    Ok(())
}

fn asset_key(source_index: u16, path: &str) -> String {
    let mut key = String::with_capacity(path.len() + 8);
    key.push_str(&source_index.to_string());
    key.push('\0');
    key.push_str(path);
    key
}

fn slash_path(path: PathBuf) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn blake3_file(path: &Path) -> Result<String> {
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

fn put_u16(output: &mut Vec<u8>, value: u16) {
    output.extend_from_slice(&value.to_le_bytes());
}

fn put_u32(output: &mut Vec<u8>, value: u32) {
    output.extend_from_slice(&value.to_le_bytes());
}

fn put_u64(output: &mut Vec<u8>, value: u64) {
    output.extend_from_slice(&value.to_le_bytes());
}

fn write_u32(output: &mut [u8], offset: usize, value: u32) -> Result<()> {
    let target = output
        .get_mut(offset..offset + 4)
        .ok_or_else(|| anyhow!("header write overflow"))?;
    target.copy_from_slice(&value.to_le_bytes());
    Ok(())
}

fn write_u64(output: &mut [u8], offset: usize, value: u64) -> Result<()> {
    let target = output
        .get_mut(offset..offset + 8)
        .ok_or_else(|| anyhow!("header write overflow"))?;
    target.copy_from_slice(&value.to_le_bytes());
    Ok(())
}
