use std::cmp::Ordering;
use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Result, anyhow, ensure};
use serde::{Deserialize, Serialize};

use crate::bundle::{AudioEntry, Bundle, RecordEntry, TermEntry};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LookupMode {
    Sorted,
    Mph,
    Preload,
}

#[derive(Clone)]
pub struct QueryEngine {
    bundle: Arc<Bundle>,
    backend: Backend,
}

#[derive(Clone)]
enum Backend {
    Sorted,
    Mph,
    Preload(Arc<HashMap<Box<str>, TermEntry>>),
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct QueryInput {
    pub term: String,
    pub reading: Option<String>,
    pub sources: Option<Vec<String>>,
    pub users: Vec<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CandidateResponse {
    pub audio_id: u32,
    pub source: String,
    pub speaker: Option<String>,
    pub reading: Option<String>,
    pub name: String,
    pub mime: &'static str,
    pub url: String,
}

#[derive(Debug, Clone, Copy)]
pub struct CandidateRef<'a> {
    pub audio_id: u32,
    pub audio: AudioEntry,
    pub source: &'a str,
    pub name: &'a str,
    pub speaker: Option<&'a str>,
    pub reading: Option<&'a str>,
    ordinal: usize,
}

impl QueryEngine {
    pub fn new(bundle: Arc<Bundle>, mode: LookupMode) -> Result<Self> {
        let backend = match mode {
            LookupMode::Sorted => Backend::Sorted,
            LookupMode::Mph => Backend::Mph,
            LookupMode::Preload => Backend::Preload(Arc::new(bundle.preload_hashmap()?)),
        };
        Ok(Self { bundle, backend })
    }

    pub fn bundle(&self) -> &Arc<Bundle> {
        &self.bundle
    }

    pub fn mode_name(&self) -> &'static str {
        match self.backend {
            Backend::Sorted => "mmap-sorted-hash",
            Backend::Mph => "mmap-chd",
            Backend::Preload(_) => "preload-hashmap",
        }
    }

    pub fn term_entry(&self, term: &str) -> Result<Option<TermEntry>> {
        match &self.backend {
            Backend::Sorted => self.bundle.lookup_term(term),
            Backend::Mph => self.bundle.lookup_term_mph(term),
            Backend::Preload(map) => Ok(map.get(term).copied()),
        }
    }

    pub fn candidates<'a>(&'a self, query: &QueryInput) -> Result<Vec<CandidateRef<'a>>> {
        let Some(term) = self.term_entry(&query.term)? else {
            return Ok(Vec::new());
        };
        let source_rank = self.source_rank(query);
        let user_rank = user_rank(&query.users);
        let mut output = Vec::with_capacity(term.record_count as usize);
        for relative in 0..term.record_count as usize {
            let ordinal = term.first_record as usize + relative;
            let record = self.bundle.record(ordinal)?;
            if let Some(candidate) =
                self.resolve_record(record, ordinal, query, &source_rank, &user_rank)?
            {
                output.push(candidate);
            }
        }
        output.sort_by(|left, right| compare_candidates(left, right, &source_rank, &user_rank));
        Ok(output)
    }

    pub fn best_candidate<'a>(&'a self, query: &QueryInput) -> Result<Option<CandidateRef<'a>>> {
        let Some(term) = self.term_entry(&query.term)? else {
            return Ok(None);
        };
        let source_rank = self.source_rank(query);
        let user_rank = user_rank(&query.users);
        let default_fast_path = query.sources.is_none() && query.users.is_empty();
        let mut best = None;
        for relative in 0..term.record_count as usize {
            let ordinal = term.first_record as usize + relative;
            let record = self.bundle.record(ordinal)?;
            let Some(candidate) =
                self.resolve_record(record, ordinal, query, &source_rank, &user_rank)?
            else {
                continue;
            };
            if default_fast_path {
                // Compiler record groups use the legacy default source/reading order.
                return Ok(Some(candidate));
            }
            if best.as_ref().is_none_or(|current| {
                compare_candidates(&candidate, current, &source_rank, &user_rank) == Ordering::Less
            }) {
                best = Some(candidate);
            }
        }
        Ok(best)
    }

    pub fn candidate_response(
        &self,
        candidate: CandidateRef<'_>,
        base_url: &str,
    ) -> CandidateResponse {
        CandidateResponse {
            audio_id: candidate.audio_id,
            source: candidate.source.to_owned(),
            speaker: candidate.speaker.map(str::to_owned),
            reading: candidate.reading.map(str::to_owned),
            name: candidate.name.to_owned(),
            mime: candidate.audio.mime.as_str(),
            url: format!(
                "{base_url}/v/{}/audio/{}",
                self.bundle.manifest.bundle_version, candidate.audio_id
            ),
        }
    }

    fn source_rank(&self, query: &QueryInput) -> HashMap<u16, usize> {
        let mut output = HashMap::new();
        match &query.sources {
            None => {
                for index in 0..self.bundle.manifest.sources.len() {
                    output.insert(index as u16, index);
                }
            }
            Some(requested) => {
                for (rank, id) in requested.iter().enumerate() {
                    if let Some(index) = self
                        .bundle
                        .manifest
                        .sources
                        .iter()
                        .position(|source| source.id == *id)
                    {
                        output.entry(index as u16).or_insert(rank);
                    }
                }
            }
        }
        output
    }

    fn resolve_record<'a>(
        &'a self,
        record: RecordEntry,
        ordinal: usize,
        query: &QueryInput,
        source_rank: &HashMap<u16, usize>,
        user_rank: &HashMap<&str, usize>,
    ) -> Result<Option<CandidateRef<'a>>> {
        if !source_rank.contains_key(&record.source_index) {
            return Ok(None);
        }
        let reading = self
            .bundle
            .optional_string(record.reading_offset, record.reading_len)?;
        if let Some(requested) = query.reading.as_deref() {
            if reading.is_some_and(|value| value != requested) {
                return Ok(None);
            }
        }
        let speaker = self
            .bundle
            .optional_string(record.speaker_offset, record.speaker_len)?;
        if !query.users.is_empty() && speaker.is_some_and(|value| !user_rank.contains_key(value)) {
            return Ok(None);
        }
        let source = self
            .bundle
            .manifest
            .sources
            .get(record.source_index as usize)
            .ok_or_else(|| anyhow!("record source is outside manifest"))?;
        let name = self.bundle.string(record.name_offset, record.name_len)?;
        let audio = self.bundle.audio(record.audio_id as usize)?;
        ensure!(
            audio.source_index == record.source_index,
            "record/audio source mismatch"
        );
        Ok(Some(CandidateRef {
            audio_id: record.audio_id,
            audio,
            source: &source.id,
            name,
            speaker,
            reading,
            ordinal,
        }))
    }
}

fn user_rank(users: &[String]) -> HashMap<&str, usize> {
    let mut output = HashMap::new();
    for (rank, user) in users.iter().enumerate() {
        output.entry(user.as_str()).or_insert(rank);
    }
    output
}

fn compare_candidates(
    left: &CandidateRef<'_>,
    right: &CandidateRef<'_>,
    source_rank: &HashMap<u16, usize>,
    user_rank: &HashMap<&str, usize>,
) -> Ordering {
    source_rank[&left.audio.source_index]
        .cmp(&source_rank[&right.audio.source_index])
        .then_with(|| {
            speaker_sort(left.speaker, user_rank).cmp(&speaker_sort(right.speaker, user_rank))
        })
        .then_with(|| optional_text_cmp(left.reading, right.reading))
        .then(left.ordinal.cmp(&right.ordinal))
}

fn speaker_sort(speaker: Option<&str>, user_rank: &HashMap<&str, usize>) -> (u8, usize) {
    match speaker {
        None => (0, 0),
        Some(_value) if user_rank.is_empty() => (1, 0),
        Some(value) => (1, user_rank.get(value).copied().unwrap_or(usize::MAX)),
    }
}

fn optional_text_cmp(left: Option<&str>, right: Option<&str>) -> Ordering {
    match (left, right) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Less,
        (Some(_), None) => Ordering::Greater,
        (Some(left), Some(right)) => left.cmp(right),
    }
}

pub fn parse_query(raw: Option<&str>) -> Result<QueryInput> {
    let raw = raw.unwrap_or_default();
    ensure!(raw.len() <= 8_192, "query string exceeds 8192 bytes");
    let mut term = None;
    let mut expression = None;
    let mut reading = None;
    let mut sources: Option<Vec<String>> = None;
    let mut users = Vec::new();
    for (key, value) in url::form_urlencoded::parse(raw.as_bytes()) {
        match key.as_ref() {
            "term" if term.is_none() => term = Some(value.into_owned()),
            "expression" if expression.is_none() => expression = Some(value.into_owned()),
            "reading" if reading.is_none() => reading = Some(value.into_owned()),
            "sources" if sources.is_none() => {
                sources = Some(value.split(',').map(str::to_owned).collect())
            }
            "user" if users.is_empty() => {
                users = value
                    .split(',')
                    .map(|value| value.trim().to_owned())
                    .collect()
            }
            _ => {}
        }
    }
    let term = term
        .or(expression)
        .ok_or_else(|| anyhow!("missing term or expression query parameter"))?;
    ensure!(term.len() <= 1_024, "term exceeds 1024 UTF-8 bytes");
    if let Some(value) = &reading {
        ensure!(value.len() <= 1_024, "reading exceeds 1024 UTF-8 bytes");
    }
    if let Some(values) = &sources {
        ensure!(values.len() <= 32, "more than 32 sources requested");
        ensure!(
            values.iter().all(|value| value.len() <= 128),
            "source ID exceeds 128 bytes"
        );
    }
    ensure!(users.len() <= 128, "more than 128 users requested");
    ensure!(
        users.iter().all(|value| value.len() <= 256),
        "user exceeds 256 bytes"
    );
    Ok(QueryInput {
        term,
        reading,
        sources,
        users,
    })
}
