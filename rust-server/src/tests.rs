use std::fs;
use std::sync::Arc;

use anyhow::Result;
use rusqlite::Connection;
use tempfile::TempDir;

use crate::bundle::Bundle;
use crate::compiler::{CompileOptions, compile};
use crate::query::{LookupMode, QueryEngine, QueryInput};

fn fixture() -> Result<(TempDir, std::path::PathBuf, std::path::PathBuf)> {
    let temp = TempDir::new()?;
    let addon = temp.path().join("addon");
    let media_a = addon.join("user_files").join("alpha");
    let media_b = addon.join("user_files").join("beta");
    fs::create_dir_all(&media_a)?;
    fs::create_dir_all(&media_b)?;
    fs::write(
        addon.join("default_config.json"),
        r#"{
          "sources": [
            {"type":"jpod","id":"alpha","path":"user_files/alpha","display":"Alpha %s"},
            {"type":"forvo","id":"beta","path":"user_files/beta","display":"Beta (%s)"}
          ]
        }"#,
    )?;
    let duplicate = b"ID3\x04\x00\x00fixture-identical-audio";
    fs::write(media_a.join("a.mp3"), duplicate)?;
    fs::write(media_b.join("b.mp3"), duplicate)?;
    fs::write(media_a.join("c.mp3"), b"ID3\x04\x00\x00different-audio")?;
    let db = Connection::open(addon.join("user_files").join("entries.db"))?;
    db.execute_batch(
        "CREATE TABLE entries (
            id INTEGER PRIMARY KEY NOT NULL,
            expression TEXT NOT NULL,
            reading TEXT,
            source TEXT NOT NULL,
            speaker TEXT,
            display TEXT,
            file TEXT NOT NULL
         );
         CREATE INDEX idx_expression ON entries(expression);",
    )?;
    let rows = [
        (
            "読む",
            Some("よむ"),
            "alpha",
            None,
            Some("ヨム [1]"),
            "a.mp3",
        ),
        (
            "読む",
            Some("よむ"),
            "beta",
            Some("alice"),
            Some("alice"),
            "b.mp3",
        ),
        ("読む", None, "alpha", None, None, "c.mp3"),
        (
            "詠む",
            Some("よむ"),
            "alpha",
            None,
            Some("ヨム [1]"),
            "a.mp3",
        ),
    ];
    for row in rows {
        db.execute(
            "INSERT INTO entries(expression,reading,source,speaker,display,file) VALUES (?1,?2,?3,?4,?5,?6)",
            row,
        )?;
    }
    drop(db);
    let bundle = temp.path().join("bundle");
    compile(&CompileOptions {
        addon_root: addon.clone(),
        output: bundle.clone(),
        deduplicate: true,
        pack_workers: 2,
    })?;
    Ok((temp, addon, bundle))
}

#[test]
fn compiled_backends_are_equivalent_and_deduplicate_exact_bytes() -> Result<()> {
    let (_temp, _addon, root) = fixture()?;
    let bundle = Arc::new(Bundle::open(&root, true)?);
    assert_eq!(bundle.manifest.record_count, 4);
    assert_eq!(bundle.manifest.audio_count, 3);
    assert_eq!(bundle.manifest.unique_blob_count, 2);
    assert_eq!(bundle.manifest.identical_content_assets, 1);
    assert!(bundle.manifest.deduplicated_bytes > 0);
    let query = QueryInput {
        term: "読む".to_owned(),
        reading: Some("よむ".to_owned()),
        sources: None,
        users: Vec::new(),
    };
    let sorted = QueryEngine::new(bundle.clone(), LookupMode::Sorted)?;
    let fixture_candidates = sorted.candidates(&query)?;
    let response = sorted.candidate_response(
        *fixture_candidates
            .iter()
            .find(|candidate| candidate.reading.is_some())
            .expect("fixture has an exact-reading candidate"),
        "http://127.0.0.1:5052",
    );
    assert_eq!(response.reading.as_deref(), query.reading.as_deref());
    assert!(serde_json::to_value(&response)?.get("reading").is_some());
    let expected = fixture_candidates
        .into_iter()
        .map(|item| (item.name.to_owned(), item.audio_id))
        .collect::<Vec<_>>();
    assert_eq!(expected.len(), 3); // exact reading plus reading=NULL wildcard
    for mode in [LookupMode::Mph, LookupMode::Preload] {
        let actual = QueryEngine::new(bundle.clone(), mode)?
            .candidates(&query)?
            .into_iter()
            .map(|item| (item.name.to_owned(), item.audio_id))
            .collect::<Vec<_>>();
        assert_eq!(actual, expected);
    }
    assert!(bundle.lookup_term_mph("not-present")?.is_none());
    let audio = bundle.audio(expected[0].1 as usize)?;
    let all = bundle.audio_bytes(audio, 0, audio.length)?;
    let tail = bundle.audio_bytes(audio, audio.length - 5, 5)?;
    assert_eq!(tail.as_ref(), &all[all.len() - 5..]);
    Ok(())
}

#[test]
fn filtering_order_and_expression_alias_semantics_are_retained() -> Result<()> {
    let (_temp, _addon, root) = fixture()?;
    let bundle = Arc::new(Bundle::open(&root, true)?);
    let engine = QueryEngine::new(bundle, LookupMode::Mph)?;
    let query = QueryInput {
        term: "読む".to_owned(),
        reading: Some("よむ".to_owned()),
        sources: Some(vec!["beta".to_owned(), "alpha".to_owned()]),
        users: vec!["alice".to_owned()],
    };
    let candidates = engine.candidates(&query)?;
    assert_eq!(candidates.len(), 3);
    assert_eq!(candidates[0].source, "beta");
    assert_eq!(candidates[0].speaker, Some("alice"));
    assert_eq!(candidates[1].source, "alpha");
    Ok(())
}

#[test]
fn pinned_user_sources_keep_their_paths_and_gain_new_defaults() -> Result<()> {
    let temp = TempDir::new()?;
    let addon = temp.path().join("addon");
    let moved = addon.join("user_files").join("alpha_moved");
    let media_b = addon.join("user_files").join("beta");
    fs::create_dir_all(&moved)?;
    fs::create_dir_all(&media_b)?;
    fs::write(
        addon.join("default_config.json"),
        r#"{
          "sources": [
            {"type":"jpod","id":"alpha","path":"user_files/alpha","display":"Alpha %s"},
            {"type":"forvo","id":"beta","path":"user_files/beta","display":"Beta (%s)"}
          ]
        }"#,
    )?;
    // a config written before "beta" existed: the pinned path wins, the default is appended
    fs::write(
        addon.join("user_files").join("config.json"),
        r#"{
          "sources": [
            {"type":"jpod","id":"alpha","path":"user_files/alpha_moved","display":"Alpha %s"}
          ]
        }"#,
    )?;
    fs::write(moved.join("a.mp3"), b"ID3\x04\x00\x00moved-alpha-audio")?;
    fs::write(media_b.join("b.mp3"), b"ID3\x04\x00\x00default-beta-audio")?;
    let db = Connection::open(addon.join("user_files").join("entries.db"))?;
    db.execute_batch(
        "CREATE TABLE entries (
            id INTEGER PRIMARY KEY NOT NULL,
            expression TEXT NOT NULL,
            reading TEXT,
            source TEXT NOT NULL,
            speaker TEXT,
            display TEXT,
            file TEXT NOT NULL
         );
         CREATE INDEX idx_expression ON entries(expression);",
    )?;
    for row in [("alpha", "a.mp3"), ("beta", "b.mp3")] {
        db.execute(
            "INSERT INTO entries(expression,reading,source,speaker,display,file)
             VALUES ('読む',NULL,?1,NULL,NULL,?2)",
            row,
        )?;
    }
    drop(db);
    let root = temp.path().join("bundle");
    compile(&CompileOptions {
        addon_root: addon.clone(),
        output: root.clone(),
        deduplicate: true,
        pack_workers: 2,
    })?;
    let bundle = Arc::new(Bundle::open(&root, true)?);
    let engine = QueryEngine::new(bundle.clone(), LookupMode::Mph)?;
    let candidates = engine.candidates(&QueryInput {
        term: "読む".to_owned(),
        reading: None,
        sources: None,
        users: Vec::new(),
    })?;
    assert_eq!(
        candidates
            .iter()
            .map(|item| item.source)
            .collect::<Vec<_>>(),
        vec!["alpha", "beta"]
    );
    let audio = bundle.audio(candidates[0].audio_id as usize)?;
    assert_eq!(
        bundle.audio_bytes(audio, 0, audio.length)?.as_ref(),
        b"ID3\x04\x00\x00moved-alpha-audio"
    );
    Ok(())
}

#[test]
fn truncated_lookup_is_rejected() -> Result<()> {
    let (_temp, _addon, root) = fixture()?;
    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(root.join("manifest.json"))?)?;
    let lookup = root.join(manifest["lookupFile"].as_str().unwrap());
    let file = fs::OpenOptions::new().write(true).open(&lookup)?;
    file.set_len(64)?;
    drop(file);
    assert!(Bundle::open(&root, true).is_err());
    Ok(())
}

#[test]
fn unsafe_manifest_paths_are_rejected() -> Result<()> {
    let (_temp, _addon, root) = fixture()?;
    let manifest_path = root.join("manifest.json");
    let mut manifest: serde_json::Value = serde_json::from_slice(&fs::read(&manifest_path)?)?;
    manifest["lookupFile"] = serde_json::Value::String("../outside.bin".to_owned());
    fs::write(&manifest_path, serde_json::to_vec(&manifest)?)?;
    assert!(Bundle::open(&root, true).is_err());
    Ok(())
}
