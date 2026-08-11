use std::collections::HashMap;
use std::fs::File;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, anyhow, bail, ensure};
use bytes::Bytes;
use memmap2::{Mmap, MmapOptions};
use serde::{Deserialize, Serialize};
use xxhash_rust::xxh3::xxh3_64;

pub const FORMAT_VERSION: u32 = 1;
pub const HEADER_LEN: usize = 160;
pub const FANOUT_LEN: usize = 65_537;
pub const TERM_LEN: usize = 24;
pub const RECORD_LEN: usize = 32;
pub const AUDIO_LEN: usize = 40;
pub const MAGIC: &[u8; 8] = b"YAFIDX1\0";
pub const NONE_REF: u32 = u32::MAX;

const HEADER_CHECKSUM_OFFSET: usize = 120;
const HEADER_CHECKSUM_END: usize = 152;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceManifest {
    pub id: String,
    pub display: String,
    pub legacy_media_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BundleManifest {
    pub format_version: u32,
    pub bundle_version: String,
    pub lookup_file: String,
    pub pack_file: String,
    pub lookup_blake3: String,
    pub pack_blake3: String,
    pub created_utc: String,
    pub compiler_version: String,
    pub term_count: u64,
    pub record_count: u64,
    pub audio_count: u64,
    pub unique_blob_count: u64,
    pub pack_bytes: u64,
    pub source_audio_bytes: u64,
    pub deduplicated_bytes: u64,
    pub repeated_path_references: u64,
    pub identical_content_assets: u64,
    pub distinct_speaker_count: u64,
    #[serde(default)]
    pub pack_build_milliseconds: u64,
    #[serde(default)]
    pub mph_build_milliseconds: u64,
    #[serde(default)]
    pub lookup_build_milliseconds: u64,
    pub compile_milliseconds: u64,
    pub sources: Vec<SourceManifest>,
}

#[derive(Debug, Clone, Copy)]
pub struct TermEntry {
    pub hash: u64,
    pub term_offset: u32,
    pub term_len: u32,
    pub first_record: u32,
    pub record_count: u32,
}

#[derive(Debug, Clone, Copy)]
pub struct RecordEntry {
    pub reading_offset: u32,
    pub reading_len: u32,
    pub speaker_offset: u32,
    pub speaker_len: u32,
    pub name_offset: u32,
    pub name_len: u32,
    pub audio_id: u32,
    pub source_index: u16,
}

#[derive(Debug, Clone, Copy)]
pub struct AudioEntry {
    pub pack_offset: u64,
    pub length: u64,
    pub path_offset: u32,
    pub path_len: u32,
    pub content_hash_prefix: u64,
    pub source_index: u16,
    pub mime: Mime,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Mime {
    Mpeg = 1,
    Aac = 2,
    Mp4 = 3,
    Ogg = 4,
    Flac = 5,
    Wav = 6,
}

impl Mime {
    pub fn from_extension(path: &Path) -> Option<Self> {
        match path.extension()?.to_str()?.to_ascii_lowercase().as_str() {
            "mp3" => Some(Self::Mpeg),
            "aac" => Some(Self::Aac),
            "m4a" => Some(Self::Mp4),
            "ogg" | "oga" | "opus" => Some(Self::Ogg),
            "flac" => Some(Self::Flac),
            "wav" => Some(Self::Wav),
            _ => None,
        }
    }

    pub fn from_byte(value: u8) -> Result<Self> {
        match value {
            1 => Ok(Self::Mpeg),
            2 => Ok(Self::Aac),
            3 => Ok(Self::Mp4),
            4 => Ok(Self::Ogg),
            5 => Ok(Self::Flac),
            6 => Ok(Self::Wav),
            _ => bail!("invalid MIME tag {value}"),
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Mpeg => "audio/mpeg",
            Self::Aac => "audio/aac",
            Self::Mp4 => "audio/mp4",
            Self::Ogg => "audio/ogg",
            Self::Flac => "audio/flac",
            Self::Wav => "audio/wav",
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Header {
    term_count: u64,
    record_count: u64,
    audio_count: u64,
    fanout_offset: u64,
    terms_offset: u64,
    records_offset: u64,
    audio_offset: u64,
    strings_offset: u64,
    strings_len: u64,
    mph_bucket_count: u64,
    mph_buckets_offset: u64,
    mph_terms_offset: u64,
}

#[derive(Clone)]
pub struct Bundle {
    pub root: PathBuf,
    pub manifest: Arc<BundleManifest>,
    index: Arc<Mmap>,
    pack: Arc<Mmap>,
    header: Header,
}

#[derive(Clone)]
pub struct MappedBytes {
    map: Arc<Mmap>,
    start: usize,
    end: usize,
}

impl AsRef<[u8]> for MappedBytes {
    fn as_ref(&self) -> &[u8] {
        &self.map[self.start..self.end]
    }
}

impl Bundle {
    pub fn open(root: &Path, verify_index: bool) -> Result<Self> {
        let root = root
            .canonicalize()
            .with_context(|| format!("cannot resolve bundle root {}", root.display()))?;
        let manifest_path = root.join("manifest.json");
        let manifest: BundleManifest = serde_json::from_reader(
            File::open(&manifest_path)
                .with_context(|| format!("cannot open {}", manifest_path.display()))?,
        )
        .with_context(|| format!("cannot parse {}", manifest_path.display()))?;
        ensure!(
            manifest.format_version == FORMAT_VERSION,
            "bundle format {} is unsupported (expected {})",
            manifest.format_version,
            FORMAT_VERSION
        );
        ensure!(
            !manifest.bundle_version.is_empty()
                && manifest
                    .bundle_version
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit()),
            "bundleVersion must be non-empty hexadecimal text"
        );

        let lookup_path = resolve_manifest_path(&root, &manifest.lookup_file)?;
        let pack_path = resolve_manifest_path(&root, &manifest.pack_file)?;
        let index = map_file(&lookup_path)?;
        let pack = map_file(&pack_path)?;
        let header = parse_header(&index, verify_index)?;

        ensure!(
            header.term_count == manifest.term_count,
            "manifest/index term count differs"
        );
        ensure!(
            header.record_count == manifest.record_count,
            "manifest/index record count differs"
        );
        ensure!(
            header.audio_count == manifest.audio_count,
            "manifest/index audio count differs"
        );
        ensure!(
            pack.len() as u64 == manifest.pack_bytes,
            "pack length differs from manifest"
        );

        let bundle = Self {
            root,
            manifest: Arc::new(manifest),
            index,
            pack,
            header,
        };
        bundle.validate_structure()?;
        Ok(bundle)
    }

    pub fn term_count(&self) -> usize {
        self.header.term_count as usize
    }

    pub fn record_count(&self) -> usize {
        self.header.record_count as usize
    }

    pub fn audio_count(&self) -> usize {
        self.header.audio_count as usize
    }

    pub fn lookup_term(&self, term: &str) -> Result<Option<TermEntry>> {
        let hash = xxh3_64(term.as_bytes());
        let bucket = (hash >> 48) as usize;
        let start = self.fanout(bucket)? as usize;
        let end = self.fanout(bucket + 1)? as usize;
        ensure!(
            start <= end && end <= self.term_count(),
            "invalid fanout range"
        );

        let mut low = start;
        let mut high = end;
        while low < high {
            let mid = low + (high - low) / 2;
            if self.term(mid)?.hash < hash {
                low = mid + 1;
            } else {
                high = mid;
            }
        }

        let mut index = low;
        while index < end {
            let candidate = self.term(index)?;
            if candidate.hash != hash {
                break;
            }
            // Hashes are only an accelerator. Exact UTF-8 key bytes are always checked,
            // making malicious or accidental XXH3 collisions harmless.
            if self.string(candidate.term_offset, candidate.term_len)? == term {
                return Ok(Some(candidate));
            }
            index += 1;
        }
        Ok(None)
    }

    /// Minimal-perfect-hash-style CHD lookup. Unknown keys can map to an occupied
    /// slot, so exact UTF-8 bytes are still verified before a result is accepted.
    pub fn lookup_term_mph(&self, term: &str) -> Result<Option<TermEntry>> {
        if self.term_count() == 0 || self.header.mph_bucket_count == 0 {
            return Ok(None);
        }
        let hash = xxh3_64(term.as_bytes());
        let bucket = (hash % self.header.mph_bucket_count) as usize;
        let displacement = self.mph_displacement(bucket)?;
        if displacement == i32::MIN {
            return Ok(None);
        }
        let slot = if displacement < 0 {
            (-i64::from(displacement) - 1) as usize
        } else {
            (xxhash_rust::xxh3::xxh3_64_with_seed(term.as_bytes(), displacement as u64)
                % self.header.term_count) as usize
        };
        ensure!(slot < self.term_count(), "MPH slot is out of range");
        let candidate = self.mph_term(slot)?;
        if candidate.hash == hash && self.string(candidate.term_offset, candidate.term_len)? == term
        {
            Ok(Some(candidate))
        } else {
            Ok(None)
        }
    }

    pub fn term(&self, index: usize) -> Result<TermEntry> {
        ensure!(index < self.term_count(), "term index out of range");
        let pos = checked_table_pos(self.header.terms_offset, index, TERM_LEN, self.index.len())?;
        let bytes = &self.index[pos..pos + TERM_LEN];
        Ok(TermEntry {
            hash: le_u64(bytes, 0)?,
            term_offset: le_u32(bytes, 8)?,
            term_len: le_u32(bytes, 12)?,
            first_record: le_u32(bytes, 16)?,
            record_count: le_u32(bytes, 20)?,
        })
    }

    pub fn mph_term(&self, index: usize) -> Result<TermEntry> {
        ensure!(index < self.term_count(), "MPH term index out of range");
        let pos = checked_table_pos(
            self.header.mph_terms_offset,
            index,
            TERM_LEN,
            self.index.len(),
        )?;
        parse_term(&self.index[pos..pos + TERM_LEN])
    }

    pub fn record(&self, index: usize) -> Result<RecordEntry> {
        ensure!(index < self.record_count(), "record index out of range");
        let pos = checked_table_pos(
            self.header.records_offset,
            index,
            RECORD_LEN,
            self.index.len(),
        )?;
        let bytes = &self.index[pos..pos + RECORD_LEN];
        Ok(RecordEntry {
            reading_offset: le_u32(bytes, 0)?,
            reading_len: le_u32(bytes, 4)?,
            speaker_offset: le_u32(bytes, 8)?,
            speaker_len: le_u32(bytes, 12)?,
            name_offset: le_u32(bytes, 16)?,
            name_len: le_u32(bytes, 20)?,
            audio_id: le_u32(bytes, 24)?,
            source_index: le_u16(bytes, 28)?,
        })
    }

    pub fn audio(&self, index: usize) -> Result<AudioEntry> {
        ensure!(index < self.audio_count(), "audio index out of range");
        let pos = checked_table_pos(self.header.audio_offset, index, AUDIO_LEN, self.index.len())?;
        let bytes = &self.index[pos..pos + AUDIO_LEN];
        Ok(AudioEntry {
            pack_offset: le_u64(bytes, 0)?,
            length: le_u64(bytes, 8)?,
            path_offset: le_u32(bytes, 16)?,
            path_len: le_u32(bytes, 20)?,
            content_hash_prefix: le_u64(bytes, 24)?,
            source_index: le_u16(bytes, 32)?,
            mime: Mime::from_byte(bytes[34])?,
        })
    }

    pub fn string(&self, offset: u32, len: u32) -> Result<&str> {
        ensure!(offset != NONE_REF, "null string reference");
        let start = self
            .header
            .strings_offset
            .checked_add(offset as u64)
            .ok_or_else(|| anyhow!("string offset overflow"))?;
        let end = start
            .checked_add(len as u64)
            .ok_or_else(|| anyhow!("string length overflow"))?;
        ensure!(
            end <= self.header.strings_offset + self.header.strings_len,
            "string reference is outside string table"
        );
        std::str::from_utf8(&self.index[start as usize..end as usize])
            .context("invalid UTF-8 in string table")
    }

    pub fn optional_string(&self, offset: u32, len: u32) -> Result<Option<&str>> {
        if offset == NONE_REF {
            Ok(None)
        } else {
            self.string(offset, len).map(Some)
        }
    }

    pub fn audio_bytes(&self, audio: AudioEntry, start: u64, length: u64) -> Result<Bytes> {
        ensure!(start <= audio.length, "audio range starts beyond asset");
        ensure!(length <= audio.length - start, "audio range exceeds asset");
        let absolute_start = audio
            .pack_offset
            .checked_add(start)
            .ok_or_else(|| anyhow!("pack offset overflow"))?;
        let absolute_end = absolute_start
            .checked_add(length)
            .ok_or_else(|| anyhow!("pack length overflow"))?;
        ensure!(
            absolute_end <= self.pack.len() as u64,
            "audio range exceeds pack"
        );
        let owner = MappedBytes {
            map: self.pack.clone(),
            start: absolute_start as usize,
            end: absolute_end as usize,
        };
        Ok(Bytes::from_owner(owner))
    }

    pub fn preload_hashmap(&self) -> Result<HashMap<Box<str>, TermEntry>> {
        let mut map = HashMap::with_capacity(self.term_count());
        for index in 0..self.term_count() {
            let entry = self.term(index)?;
            let term: Box<str> = self.string(entry.term_offset, entry.term_len)?.into();
            ensure!(
                map.insert(term, entry).is_none(),
                "duplicate term in compiled index"
            );
        }
        Ok(map)
    }

    pub fn legacy_audio_path(&self, legacy_root: &Path, audio: AudioEntry) -> Result<PathBuf> {
        let source = self
            .manifest
            .sources
            .get(audio.source_index as usize)
            .ok_or_else(|| anyhow!("audio source index is invalid"))?;
        let relative = self.string(audio.path_offset, audio.path_len)?;
        ensure_safe_relative(Path::new(&source.legacy_media_dir))?;
        ensure_safe_relative(Path::new(relative))?;
        Ok(legacy_root.join(&source.legacy_media_dir).join(relative))
    }

    fn fanout(&self, index: usize) -> Result<u32> {
        ensure!(index < FANOUT_LEN, "fanout index out of range");
        let pos = checked_table_pos(self.header.fanout_offset, index, 4, self.index.len())?;
        le_u32(&self.index[pos..pos + 4], 0)
    }

    fn mph_displacement(&self, index: usize) -> Result<i32> {
        ensure!(
            index < self.header.mph_bucket_count as usize,
            "MPH bucket out of range"
        );
        let pos = checked_table_pos(self.header.mph_buckets_offset, index, 4, self.index.len())?;
        let bytes: [u8; 4] = self.index[pos..pos + 4]
            .try_into()
            .expect("slice width checked");
        Ok(i32::from_le_bytes(bytes))
    }

    fn validate_structure(&self) -> Result<()> {
        let term_count = self.term_count();
        let record_count = self.record_count();
        let audio_count = self.audio_count();
        ensure!(self.fanout(0)? == 0, "fanout must begin at zero");
        ensure!(
            self.fanout(FANOUT_LEN - 1)? as usize == term_count,
            "fanout must end at term count"
        );
        ensure!(
            self.header.mph_bucket_count > 0 || term_count == 0,
            "MPH bucket table is empty"
        );

        let mut prior_fanout = 0u32;
        for index in 0..FANOUT_LEN {
            let value = self.fanout(index)?;
            ensure!(
                value >= prior_fanout && value as usize <= term_count,
                "fanout is not monotonic"
            );
            prior_fanout = value;
        }

        let mut prior_hash = 0u64;
        for index in 0..term_count {
            let term = self.term(index)?;
            if index > 0 {
                ensure!(term.hash >= prior_hash, "term hashes are not sorted");
            }
            prior_hash = term.hash;
            let _ = self.string(term.term_offset, term.term_len)?;
            ensure!(
                term.first_record as usize <= record_count
                    && term.record_count as usize <= record_count - term.first_record as usize,
                "term record range is invalid"
            );
            let key = self.string(term.term_offset, term.term_len)?;
            let mph = self
                .lookup_term_mph(key)?
                .ok_or_else(|| anyhow!("MPH does not resolve compiled key {key:?}"))?;
            ensure!(
                mph.first_record == term.first_record
                    && mph.record_count == term.record_count
                    && self.string(mph.term_offset, mph.term_len)? == key,
                "MPH and sorted index disagree for {key:?}"
            );
        }

        for index in 0..term_count {
            let term = self.mph_term(index)?;
            let _ = self.string(term.term_offset, term.term_len)?;
            ensure!(
                term.first_record as usize <= record_count
                    && term.record_count as usize <= record_count - term.first_record as usize,
                "MPH term record range is invalid"
            );
        }

        for index in 0..record_count {
            let record = self.record(index)?;
            ensure!(
                (record.audio_id as usize) < audio_count,
                "record audio ID is invalid"
            );
            ensure!(
                (record.source_index as usize) < self.manifest.sources.len(),
                "record source is invalid"
            );
            if record.reading_offset != NONE_REF {
                let _ = self.string(record.reading_offset, record.reading_len)?;
            }
            if record.speaker_offset != NONE_REF {
                let _ = self.string(record.speaker_offset, record.speaker_len)?;
            }
            let _ = self.string(record.name_offset, record.name_len)?;
        }

        for index in 0..audio_count {
            let audio = self.audio(index)?;
            ensure!(
                (audio.source_index as usize) < self.manifest.sources.len(),
                "audio source is invalid"
            );
            let _ = self.string(audio.path_offset, audio.path_len)?;
            let end = audio
                .pack_offset
                .checked_add(audio.length)
                .ok_or_else(|| anyhow!("audio pack range overflow"))?;
            ensure!(end <= self.pack.len() as u64, "audio pack range is invalid");
        }
        Ok(())
    }
}

fn map_file(path: &Path) -> Result<Arc<Mmap>> {
    let file = File::open(path).with_context(|| format!("cannot open {}", path.display()))?;
    ensure!(file.metadata()?.len() > 0, "{} is empty", path.display());
    // SAFETY: the compiler publishes immutable versioned files. The server opens them
    // read-only and never offers mutation/deletion endpoints. The Arc keeps each mapping
    // alive until all response Bytes owners have been dropped.
    let mmap = unsafe { MmapOptions::new().map(&file) }
        .with_context(|| format!("cannot memory-map {}", path.display()))?;
    Ok(Arc::new(mmap))
}

fn parse_header(index: &[u8], verify_checksum: bool) -> Result<Header> {
    ensure!(
        index.len() >= HEADER_LEN,
        "lookup file is truncated before its header"
    );
    ensure!(&index[0..8] == MAGIC, "lookup magic is invalid");
    ensure!(
        le_u32(index, 8)? == FORMAT_VERSION,
        "lookup format version is unsupported"
    );
    ensure!(
        le_u32(index, 12)? as usize == HEADER_LEN,
        "lookup header size is invalid"
    );
    if verify_checksum {
        let expected = &index[HEADER_CHECKSUM_OFFSET..HEADER_CHECKSUM_END];
        let actual = blake3::hash(&index[HEADER_LEN..]);
        ensure!(
            expected == actual.as_bytes(),
            "lookup payload checksum failed"
        );
    }
    let header = Header {
        term_count: le_u64(index, 16)?,
        record_count: le_u64(index, 24)?,
        audio_count: le_u64(index, 32)?,
        fanout_offset: le_u64(index, 40)?,
        terms_offset: le_u64(index, 48)?,
        records_offset: le_u64(index, 56)?,
        audio_offset: le_u64(index, 64)?,
        strings_offset: le_u64(index, 72)?,
        strings_len: le_u64(index, 80)?,
        mph_bucket_count: le_u64(index, 88)?,
        mph_buckets_offset: le_u64(index, 96)?,
        mph_terms_offset: le_u64(index, 104)?,
    };

    ensure!(
        header.fanout_offset as usize >= HEADER_LEN,
        "fanout overlaps header"
    );
    checked_section(header.fanout_offset, FANOUT_LEN as u64 * 4, index.len())?;
    checked_section(
        header.terms_offset,
        header
            .term_count
            .checked_mul(TERM_LEN as u64)
            .ok_or_else(|| anyhow!("term table length overflow"))?,
        index.len(),
    )?;
    checked_section(
        header.mph_buckets_offset,
        header
            .mph_bucket_count
            .checked_mul(4)
            .ok_or_else(|| anyhow!("MPH bucket table length overflow"))?,
        index.len(),
    )?;
    checked_section(
        header.mph_terms_offset,
        header
            .term_count
            .checked_mul(TERM_LEN as u64)
            .ok_or_else(|| anyhow!("MPH term table length overflow"))?,
        index.len(),
    )?;
    checked_section(
        header.records_offset,
        header
            .record_count
            .checked_mul(RECORD_LEN as u64)
            .ok_or_else(|| anyhow!("record table length overflow"))?,
        index.len(),
    )?;
    checked_section(
        header.audio_offset,
        header
            .audio_count
            .checked_mul(AUDIO_LEN as u64)
            .ok_or_else(|| anyhow!("audio table length overflow"))?,
        index.len(),
    )?;
    checked_section(header.strings_offset, header.strings_len, index.len())?;
    ensure!(
        header.fanout_offset <= header.terms_offset,
        "lookup sections are out of order"
    );
    ensure!(
        header.terms_offset <= header.mph_buckets_offset,
        "lookup sections are out of order"
    );
    ensure!(
        header.mph_buckets_offset <= header.mph_terms_offset,
        "lookup sections are out of order"
    );
    ensure!(
        header.mph_terms_offset <= header.records_offset,
        "lookup sections are out of order"
    );
    ensure!(
        header.records_offset <= header.audio_offset,
        "lookup sections are out of order"
    );
    ensure!(
        header.audio_offset <= header.strings_offset,
        "lookup sections are out of order"
    );
    Ok(header)
}

fn parse_term(bytes: &[u8]) -> Result<TermEntry> {
    Ok(TermEntry {
        hash: le_u64(bytes, 0)?,
        term_offset: le_u32(bytes, 8)?,
        term_len: le_u32(bytes, 12)?,
        first_record: le_u32(bytes, 16)?,
        record_count: le_u32(bytes, 20)?,
    })
}

fn checked_section(offset: u64, length: u64, total: usize) -> Result<()> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| anyhow!("lookup section length overflow"))?;
    ensure!(end <= total as u64, "lookup section is truncated");
    Ok(())
}

fn checked_table_pos(offset: u64, index: usize, width: usize, total: usize) -> Result<usize> {
    let item = (index as u64)
        .checked_mul(width as u64)
        .ok_or_else(|| anyhow!("table index overflow"))?;
    let pos = offset
        .checked_add(item)
        .ok_or_else(|| anyhow!("table offset overflow"))?;
    checked_section(pos, width as u64, total)?;
    Ok(pos as usize)
}

fn resolve_manifest_path(root: &Path, value: &str) -> Result<PathBuf> {
    let relative = Path::new(value);
    ensure_safe_relative(relative)?;
    let path = root.join(relative);
    let canonical = path
        .canonicalize()
        .with_context(|| format!("cannot resolve bundle file {}", path.display()))?;
    ensure!(
        canonical.starts_with(root),
        "bundle file escapes the bundle root"
    );
    Ok(canonical)
}

pub fn ensure_safe_relative(path: &Path) -> Result<()> {
    ensure!(!path.as_os_str().is_empty(), "empty relative path");
    ensure!(!path.is_absolute(), "absolute paths are forbidden");
    for component in path.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                bail!("unsafe relative path {}", path.display())
            }
        }
    }
    Ok(())
}

fn le_u16(bytes: &[u8], offset: usize) -> Result<u16> {
    let value = bytes
        .get(offset..offset + 2)
        .ok_or_else(|| anyhow!("truncated u16"))?;
    Ok(u16::from_le_bytes(
        value.try_into().expect("slice width checked"),
    ))
}

fn le_u32(bytes: &[u8], offset: usize) -> Result<u32> {
    let value = bytes
        .get(offset..offset + 4)
        .ok_or_else(|| anyhow!("truncated u32"))?;
    Ok(u32::from_le_bytes(
        value.try_into().expect("slice width checked"),
    ))
}

fn le_u64(bytes: &[u8], offset: usize) -> Result<u64> {
    let value = bytes
        .get(offset..offset + 8)
        .ok_or_else(|| anyhow!("truncated u64"))?;
    Ok(u64::from_le_bytes(
        value.try_into().expect("slice width checked"),
    ))
}

pub fn align8(value: usize) -> usize {
    (value + 7) & !7
}
