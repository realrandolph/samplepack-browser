from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import catalog_db
import mcp_protocol
from server import analyze_uploaded_wav


def sample_fixture(file_name: str) -> dict:
    return {
        "file": file_name,
        "name": "Fixture sample",
        "category": "kicks",
        "role": "kick",
        "tags": ["fixture", "punchy"],
        "use": ["kick", "oneshot"],
        "description": "A fixture for catalog tests",
        "transcript": None,
        "duration": 0.5,
        "sr": 48000,
        "channels": 2,
        "bit_depth": 24,
        "peak": 0.4,
        "rms": 0.1,
        "crest": 4,
        "centroid_hz": 120,
        "license": "test-only",
    }


def write_test_wav(path: Path, *, channels: int = 2, width: int = 3,
                   sample_rate: int = 48000, frames: int = 480) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(sample_rate)
        if width == 2:
            data = b"".join(struct.pack("<h", 2000 if index % 2 else -2000)
                            for index in range(frames * channels))
        elif width == 3:
            data = b"".join(
                ((2000 if index % 2 else -2000) & 0xFFFFFF).to_bytes(3, "little")
                for index in range(frames * channels)
            )
        else:
            raise ValueError("test helper supports 16-bit and 24-bit PCM")
        wav.writeframes(data)


def make_pcm24_stereo_wav() -> bytes:
    samples = [1000, -1000, 2000, -2000]
    data = b"".join((value & 0xFFFFFF).to_bytes(3, "little") for value in samples)
    fmt = struct.pack("<HHIIHH", 1, 2, 48000, 288000, 6, 24)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="samplepack-tests-")
        self.db = Path(self.tempdir.name) / "catalog.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_catalog_and_ui_state_round_trip(self) -> None:
        catalog = {
            "name": "test pack",
            "description": "tests",
            "format": "WAV PCM_24 stereo 48000 Hz",
            "updated": "2026-10-31",
            "agent": {"help": "retained metadata"},
            "samples": [sample_fixture("kicks/kick_fixture.wav")],
        }
        catalog_db.save_catalog(catalog, self.db, write_indexes=False)
        initial = catalog_db.load_catalog(self.db)
        self.assertEqual(initial["count"], 1)
        self.assertEqual(initial["agent"], {"help": "retained metadata"})
        self.assertFalse(initial["samples"][0]["reviewed"])

        catalog_db.set_state("kicks/kick_fixture.wav", favorite=True, path=self.db)
        catalog_db.set_state("kicks/kick_fixture.wav", flagged=True, reason="check tail", path=self.db)
        flagged = catalog_db.get_sample("kicks/kick_fixture.wav", self.db)
        self.assertTrue(flagged["favorite"])
        self.assertTrue(flagged["revisit"])
        self.assertEqual(flagged["revisit_reason"], "check tail")
        self.assertEqual(len(catalog_db.search_samples(role="kick", flagged=True, path=self.db)), 1)

        reviewed = catalog_db.set_state("kicks/kick_fixture.wav", reviewed=True, path=self.db)
        self.assertTrue(reviewed["reviewed"])
        self.assertFalse(reviewed["revisit"])

    def test_mcp_search_and_selection_returns_verified_path(self) -> None:
        available = next(catalog_db.SAMPLES_DIR.rglob("*.wav"))
        relative = available.relative_to(catalog_db.SAMPLES_DIR).as_posix()
        catalog_db.save_catalog({"samples": [sample_fixture(relative)]}, self.db, write_indexes=False)
        with patch.dict(os.environ, {"SAMPLEPACK_DB": str(self.db)}):
            result = mcp_protocol.call_tool("search_samples", {"query": "fixture", "limit": 5})
            search_result = result["structuredContent"]
            self.assertEqual(search_result["count"], 1)
            selected = mcp_protocol.call_tool("select_sample", {"file": relative})
            selected_result = selected["structuredContent"]
            self.assertEqual(Path(selected_result["absolute_path"]), available.resolve())
            self.assertTrue(Path(selected_result["absolute_path"]).is_file())

    def test_uploaded_wav_format_and_measurements(self) -> None:
        report = analyze_uploaded_wav(make_pcm24_stereo_wav())
        self.assertEqual(report["sr"], 48000)
        self.assertEqual(report["channels"], 2)
        self.assertEqual(report["bit_depth"], 24)
        self.assertAlmostEqual(report["duration"], 2 / 48000)
        self.assertGreater(report["peak"], 0)
        self.assertGreater(report["rms"], 0)

    def test_empty_library_initializes_without_legacy_files(self) -> None:
        base = Path(self.tempdir.name)
        samples = base / "samples"
        with patch.object(catalog_db, "SAMPLES_DIR", samples), \
                patch.object(catalog_db, "INDEX_PATH", base / "INDEX.md"), \
                patch.object(catalog_db, "MANIFEST_PATH", base / "manifest.txt"), \
                patch.object(catalog_db, "LEGACY_CATALOG_PATH", base / "catalog.json"), \
                patch.object(catalog_db, "LEGACY_REVISIT_PATH", base / "revisit_list.json"):
            self.assertEqual(catalog_db.migrate_legacy_files(self.db), 0)
            catalog = catalog_db.load_catalog(self.db)
            self.assertEqual(catalog["count"], 0)
            self.assertEqual(catalog["samples"], [])
            self.assertTrue(samples.is_dir())
            self.assertTrue((base / "INDEX.md").is_file())
            self.assertTrue((base / "manifest.txt").is_file())

    def test_import_directory_detects_native_wav_formats(self) -> None:
        base = Path(self.tempdir.name)
        source = base / "incoming" / "Kicks"
        write_test_wav(source / "mono16.wav", channels=1, width=2, sample_rate=44100)
        write_test_wav(source / "stereo24.wav", channels=2, width=3, sample_rate=48000)
        samples = base / "samples"
        with patch.object(catalog_db, "SAMPLES_DIR", samples), \
                patch.object(catalog_db, "INDEX_PATH", base / "INDEX.md"), \
                patch.object(catalog_db, "MANIFEST_PATH", base / "manifest.txt"):
            result = catalog_db.import_directory(source, "Kicks", self.db, write_indexes=False)
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["category"], "kicks")
            self.assertEqual(result["skipped"], [])
            self.assertTrue((samples / "kicks" / "mono16.wav").is_file())
            self.assertTrue((samples / "kicks" / "stereo24.wav").is_file())
            catalog = catalog_db.load_catalog(self.db)
            self.assertEqual(catalog["count"], 2)
            mono = catalog_db.get_sample("kicks/mono16.wav", self.db)
            stereo = catalog_db.get_sample("kicks/stereo24.wav", self.db)
            self.assertEqual((mono["channels"], mono["bit_depth"], mono["sr"]), (1, 16, 44100))
            self.assertEqual((stereo["channels"], stereo["bit_depth"], stereo["sr"]), (2, 24, 48000))

            again = catalog_db.import_directory(source, "Kicks", self.db, write_indexes=False)
            self.assertEqual(again["count"], 2)
            self.assertEqual(again["total"], 4)
            self.assertTrue((samples / "kicks" / "mono16_2.wav").is_file())

    def test_import_directory_rejects_missing_or_managed_source(self) -> None:
        base = Path(self.tempdir.name)
        samples = base / "samples"
        samples.mkdir()
        with patch.object(catalog_db, "SAMPLES_DIR", samples):
            with self.assertRaises(ValueError):
                catalog_db.import_directory(base / "does-not-exist", path=self.db)
            with self.assertRaises(ValueError):
                catalog_db.import_directory(samples, path=self.db)

    def test_data_and_config_directory_resolution(self) -> None:
        base = Path(self.tempdir.name)
        empty = {
            "SAMPLEPACK_DATA_DIR": "", "SAMPLEPACK_DB": "",
            "SAMPLEPACK_CONFIG_DIR": "", "XDG_DATA_HOME": str(base / "xdg-data"),
            "XDG_CONFIG_HOME": str(base / "xdg-config"),
        }
        with patch.dict(os.environ, empty):
            self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"),
                             base / "xdg-data" / "samplepack-browser")
            self.assertEqual(catalog_db.resolve_config_dir(),
                             base / "xdg-config" / "samplepack-browser")
            with patch.dict(os.environ, {"SAMPLEPACK_DATA_DIR": str(base / "explicit")}):
                self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"), base / "explicit")
            with patch.dict(os.environ, {"SAMPLEPACK_DB": str(base / "db" / "catalog.sqlite3")}):
                self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"), base / "db")

        checkout = base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "index.html").write_text("<!doctype html>", encoding="utf-8")
        with patch.dict(os.environ, empty):
            self.assertTrue(catalog_db.is_development_tree(checkout))
            self.assertEqual(catalog_db.resolve_data_dir(app_dir=checkout), checkout)

    def test_windows_directory_resolution_uses_appdata_locations(self) -> None:
        base = Path(self.tempdir.name)
        local = base / "LocalAppData"
        roaming = base / "RoamingAppData"
        env = {
            "SAMPLEPACK_DATA_DIR": "", "SAMPLEPACK_DB": "",
            "SAMPLEPACK_CONFIG_DIR": "",
            "LOCALAPPDATA": str(local), "APPDATA": str(roaming),
            "XDG_DATA_HOME": str(base / "xdg-data"),
            "XDG_CONFIG_HOME": str(base / "xdg-config"),
        }
        with patch.dict(os.environ, env), patch.object(sys, "platform", "win32"):
            self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"),
                             local / "samplepack-browser")
            self.assertEqual(catalog_db.resolve_config_dir(),
                             roaming / "samplepack-browser")
            self.assertEqual(catalog_db.config_path("state.json"),
                             roaming / "samplepack-browser" / "state.json")

            with patch.dict(os.environ, {"SAMPLEPACK_DATA_DIR": str(base / "explicit")}):
                self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"), base / "explicit")
            with patch.dict(os.environ, {"SAMPLEPACK_DB": str(base / "db" / "catalog.sqlite3")}):
                self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"), base / "db")
            with patch.dict(os.environ, {"SAMPLEPACK_CONFIG_DIR": str(base / "config")}):
                self.assertEqual(catalog_db.resolve_config_dir(), base / "config")

        fallback_env = dict(env, LOCALAPPDATA="", APPDATA="")
        with patch.dict(os.environ, fallback_env), \
                patch.object(sys, "platform", "win32"), \
                patch.object(catalog_db, "_home_dir", return_value=base / "home"):
            self.assertEqual(catalog_db.resolve_data_dir(app_dir=base / "app"),
                             base / "home" / "AppData" / "Local" / "samplepack-browser")
            self.assertEqual(catalog_db.resolve_config_dir(),
                             base / "home" / "AppData" / "Roaming" / "samplepack-browser")

    def test_windows_resolution_keeps_development_tree(self) -> None:
        base = Path(self.tempdir.name)
        checkout = base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "index.html").write_text("<!doctype html>", encoding="utf-8")
        env = {
            "SAMPLEPACK_DATA_DIR": "", "SAMPLEPACK_DB": "", "SAMPLEPACK_CONFIG_DIR": "",
            "LOCALAPPDATA": str(base / "LocalAppData"), "APPDATA": str(base / "RoamingAppData"),
        }
        with patch.dict(os.environ, env), patch.object(sys, "platform", "win32"):
            self.assertTrue(catalog_db.is_development_tree(checkout))
            self.assertEqual(catalog_db.resolve_data_dir(app_dir=checkout), checkout)

    def test_mcp_initialize_and_notifications(self) -> None:
        response = mcp_protocol.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": mcp_protocol.PROTOCOL_VERSION},
        })
        self.assertEqual(response["result"]["serverInfo"]["name"], "samplepack-browser")
        self.assertIsNone(mcp_protocol.handle_message({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))


if __name__ == "__main__":
    unittest.main()
