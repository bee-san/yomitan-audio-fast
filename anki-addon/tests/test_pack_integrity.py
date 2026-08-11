from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest

from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_integrity_addon"
os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
specification = importlib.util.spec_from_file_location(
    PACKAGE,
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
assert specification is not None and specification.loader is not None
module = importlib.util.module_from_spec(specification)
sys.modules[PACKAGE] = module
specification.loader.exec_module(module)
fast_pack = importlib.import_module(f"{PACKAGE}.fast_pack")


class RustBundleIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "entries.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                CREATE TABLE entries (
                    id INTEGER PRIMARY KEY NOT NULL,
                    expression TEXT NOT NULL,
                    reading TEXT,
                    source TEXT NOT NULL,
                    speaker TEXT,
                    display TEXT,
                    file TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO entries VALUES (1,'cat','reading','s1',NULL,NULL,'tone.opus')"
            )
            connection.commit()
        finally:
            connection.close()
        self.bundle_root = self.root / "bundle"
        self.pack_root = self.root / "addon-pack"
        self.version = "0123456789abcdef"
        self.payload = b"OggS-integrity-fixture"
        self.lookup_path, self.source_pack_path = self._write_bundle()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bundle(self) -> tuple[Path, Path]:
        version_root = self.bundle_root / "versions" / self.version
        version_root.mkdir(parents=True)
        source_pack_path = version_root / "audio.pack"
        source_pack_path.write_bytes(self.payload)
        filename = b"tone.opus"
        strings_offset = fast_pack.RUST_HEADER_SIZE + fast_pack.RUST_AUDIO_RECORD.size
        lookup = bytearray(strings_offset + len(filename))
        lookup[:8] = fast_pack.RUST_INDEX_MAGIC
        struct.pack_into("<II", lookup, 8, 1, fast_pack.RUST_HEADER_SIZE)
        struct.pack_into("<Q", lookup, 24, 1)
        struct.pack_into("<Q", lookup, 32, 1)
        struct.pack_into("<Q", lookup, 64, fast_pack.RUST_HEADER_SIZE)
        struct.pack_into("<Q", lookup, 72, strings_offset)
        struct.pack_into("<Q", lookup, 80, len(filename))
        fast_pack.RUST_AUDIO_RECORD.pack_into(
            lookup,
            fast_pack.RUST_HEADER_SIZE,
            0,
            len(self.payload),
            0,
            len(filename),
            0,
            0,
            4,
        )
        lookup[strings_offset:] = filename
        lookup_path = version_root / "lookup.bin"
        lookup_path.write_bytes(lookup)
        lookup_relative = f"versions/{self.version}/lookup.bin"
        pack_relative = f"versions/{self.version}/audio.pack"
        manifest = {
            "formatVersion": 1,
            "bundleVersion": self.version,
            "lookupFile": lookup_relative,
            "packFile": pack_relative,
            "lookupBlake3": "0" * 64,
            "packBlake3": "1" * 64,
            "recordCount": 1,
            "audioCount": 1,
            "uniqueBlobCount": 1,
            "identicalContentAssets": 0,
            "deduplicatedBytes": 0,
            "packBytes": len(self.payload),
            "sources": [{"id": "s1"}],
        }
        (self.bundle_root / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        integrity = {
            "format": fast_pack.INTEGRITY_FORMAT,
            "bundleVersion": self.version,
            "files": {
                lookup_relative: {
                    "bytes": len(lookup),
                    # Uppercase is deliberately accepted and normalized.
                    "sha256": hashlib.sha256(lookup).hexdigest().upper(),
                },
                pack_relative: {
                    "bytes": len(self.payload),
                    "sha256": hashlib.sha256(self.payload).hexdigest(),
                },
            },
        }
        (self.bundle_root / fast_pack.INTEGRITY_FILE_NAME).write_text(
            json.dumps(integrity), encoding="utf-8"
        )
        return lookup_path, source_pack_path

    def _import(self, suffix: str = "") -> dict:
        pack_root = self.pack_root.with_name(self.pack_root.name + suffix)
        return fast_pack.import_rust_bundle(
            self.db_path, pack_root, self.bundle_root
        )

    def test_valid_sidecar_is_verified_and_recorded(self) -> None:
        result = self._import()
        self.assertEqual(
            result["integrity"]["rust_lookup_sha256"],
            hashlib.sha256(self.lookup_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            result["integrity"]["pack_sha256"],
            hashlib.sha256(self.payload).hexdigest(),
        )
        version_manifest = json.loads(
            (
                self.pack_root
                / "versions"
                / result["version"]
                / fast_pack.VERSION_MANIFEST_NAME
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(version_manifest["pack_sha256"], result["integrity"]["pack_sha256"])

    def test_missing_sidecar_is_rejected(self) -> None:
        (self.bundle_root / fast_pack.INTEGRITY_FILE_NAME).unlink()
        with self.assertRaises(FileNotFoundError):
            self._import("-missing")

    def test_same_size_lookup_tampering_is_rejected_before_parse(self) -> None:
        data = bytearray(self.lookup_path.read_bytes())
        data[-1] ^= 1
        self.lookup_path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self._import("-lookup-tampered")

    def test_same_size_pack_tampering_is_rejected(self) -> None:
        data = bytearray(self.source_pack_path.read_bytes())
        data[-1] ^= 1
        self.source_pack_path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self._import("-pack-tampered")

    def test_corrupted_same_size_published_index_is_never_reused(self) -> None:
        result = self._import()
        index_path = self.pack_root / result["index"]
        original_size = index_path.stat().st_size
        with index_path.open("r+b") as index_file:
            index_file.seek(-1, os.SEEK_END)
            byte = index_file.read(1)
            index_file.seek(-1, os.SEEK_END)
            index_file.write(bytes((byte[0] ^ 1,)))
        self.assertEqual(index_path.stat().st_size, original_size)
        with self.assertRaisesRegex(FileExistsError, "incompatible published pack"):
            self._import()

    def test_sidecar_version_and_exact_file_set_are_enforced(self) -> None:
        integrity_path = self.bundle_root / fast_pack.INTEGRITY_FILE_NAME
        integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
        integrity["bundleVersion"] = "fedcba9876543210"
        integrity_path.write_text(json.dumps(integrity), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "format/version mismatch"):
            self._import("-wrong-version")


if __name__ == "__main__":
    unittest.main()
