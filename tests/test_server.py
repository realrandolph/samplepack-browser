from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import catalog_db
import server


def sample_fixture(file_name: str) -> dict:
    return {
        "file": file_name,
        "name": "Fixture sample",
        "category": "kicks",
        "role": "kick",
        "tags": [],
        "use": ["kick"],
        "description": "A fixture for server tests",
    }


class QuietHandler(server.SamplepackHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class ServerHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = server.SamplepackServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, name="test-http", daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def request(self, method: str, path: str, *, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_shutdown_accepts_bodyless_post_and_uses_callback(self) -> None:
        fired = threading.Event()
        previous = getattr(self.httpd, "shutdown_callback", None)
        self.httpd.shutdown_callback = fired.set
        try:
            status, _headers, raw = self.request("POST", "/api/shutdown")
        finally:
            self.httpd.shutdown_callback = previous
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertTrue(fired.wait(2))

    def test_static_allowlist_serves_shell_icon_and_samples(self) -> None:
        for path in ("/", "/index.html", "/icon.png"):
            status, headers, raw = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(raw), 0, path)
            self.assertTrue(headers["Content-Type"], path)

        sample = min(catalog_db.SAMPLES_DIR.rglob("*.wav"), key=lambda path: path.stat().st_size)
        relative = sample.relative_to(catalog_db.SAMPLES_DIR).as_posix()
        status, headers, raw = self.request("GET", "/samples/" + quote(relative))
        self.assertEqual(status, 200)
        self.assertEqual(raw, sample.read_bytes())
        self.assertIn("audio", headers["Content-Type"])

    def test_static_allowlist_rejects_non_static_paths_and_traversal(self) -> None:
        blocked = (
            "/catalog.sqlite3",
            "/catalog.sqlite3-wal",
            "/server.py",
            "/catalog_db.py",
            "/tests/test_server.py",
            "/source/archival/provenance.json",
            "/.git/config",
            "/.venv/bin/python",
            "/work",
            "/samples",
            "/samples/",
            "/samples/../catalog.sqlite3",
            "/samples/%2e%2e%2fcatalog.sqlite3",
            "/samples/%2e%2e%2fserver.py",
            "/%2e%2e/etc/passwd",
        )
        for path in blocked:
            status, _headers, raw = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"Index of", raw, path)

        status, headers, raw = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(raw)["count"], catalog_db.load_catalog()["count"])

        status, headers, _raw = self.request("GET", "/mcp")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")

    def test_json_mutations_require_json_content_type(self) -> None:
        body = json.dumps({"file": "kicks/none.wav"}).encode()
        status, _headers, raw = self.request(
            "POST", "/api/favorite", body=body, headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(raw)["error"])

    def test_cross_origin_mutation_is_rejected(self) -> None:
        body = json.dumps({"file": "kicks/none.wav"}).encode()
        headers = {"Content-Type": "application/json", "Origin": "http://evil.example"}
        status, _headers, raw = self.request("POST", "/api/favorite", body=body, headers=headers)
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", json.loads(raw)["error"])

        same_origin = f"http://127.0.0.1:{self.port}"
        status, _headers, raw = self.request(
            "POST", "/api/revisit", body=b'{"file": ""}',
            headers={"Content-Type": "application/json", "Origin": same_origin},
        )
        self.assertEqual(status, 400)
        self.assertNotIn("cross-origin", json.loads(raw)["error"])

    def test_multipart_upload_is_not_forced_to_json(self) -> None:
        status, _headers, raw = self.request(
            "POST", "/api/add", body=b"--x--\r\n",
            headers={"Content-Type": "multipart/form-data; boundary=x"},
        )
        self.assertEqual(status, 400)
        self.assertNotIn("Content-Type must be application/json", json.loads(raw)["error"])

    def test_delete_removes_stale_catalog_row_without_wav(self) -> None:
        with tempfile.TemporaryDirectory(prefix="samplepack-server-", dir="/tmp/opencode") as temp:
            base = Path(temp)
            db = base / "catalog.sqlite3"
            samples = base / "samples"
            samples.mkdir()
            catalog_db.save_catalog(
                {"samples": [sample_fixture("kicks/missing.wav")]}, db, write_indexes=False,
            )
            body = json.dumps({"file": "kicks/missing.wav"}).encode()
            with patch.dict(os.environ, {"SAMPLEPACK_DB": str(db)}), \
                    patch.object(server, "SAMPLES_DIR", samples), \
                    patch.object(server, "PROVENANCE_PATH", base / "provenance.json"):
                status, _headers, raw = self.request(
                    "POST", "/api/delete", body=body,
                    headers={"Content-Type": "application/json"},
                )
            self.assertEqual(status, 200)
            result = json.loads(raw)
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 0)
            self.assertIsNone(catalog_db.get_sample("kicks/missing.wav", db))


class StartupMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="samplepack-startup-", dir="/tmp/opencode")
        self.base = Path(self.tempdir.name)
        self.db = self.base / "catalog.sqlite3"
        self.patches = [
            patch.object(catalog_db, "LEGACY_CATALOG_PATH", self.base / "catalog.json"),
            patch.object(catalog_db, "LEGACY_REVISIT_PATH", self.base / "revisit_list.json"),
            patch.object(catalog_db, "INDEX_PATH", self.base / "INDEX.md"),
            patch.object(catalog_db, "MANIFEST_PATH", self.base / "manifest.txt"),
            patch.object(catalog_db, "SAMPLES_DIR", self.base / "samples"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_normal_startup_accepts_stale_catalog_row_without_wav(self) -> None:
        catalog_db.save_catalog(
            {"samples": [sample_fixture("kicks/missing.wav")]}, self.db, write_indexes=False,
        )
        self.assertEqual(catalog_db.migrate_legacy_files(self.db), 1)
        self.assertFalse((self.base / "catalog.json").exists())

    def test_legacy_migration_still_validates_missing_wav(self) -> None:
        legacy = self.base / "catalog.json"
        legacy.write_text(json.dumps({"samples": [sample_fixture("kicks/missing.wav")]}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            catalog_db.migrate_legacy_files(self.db)
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
