from __future__ import annotations

import os
import struct
import tempfile
import unittest
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


def make_pcm24_stereo_wav() -> bytes:
    samples = [1000, -1000, 2000, -2000]
    data = b"".join((value & 0xFFFFFF).to_bytes(3, "little") for value in samples)
    fmt = struct.pack("<HHIIHH", 1, 2, 48000, 288000, 6, 24)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="samplepack-tests-", dir="/tmp/opencode")
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
