from __future__ import annotations

import http.client
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import catalog_db
import server


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

        sample_candidates = list(catalog_db.SAMPLES_DIR.rglob("*.wav")) if catalog_db.SAMPLES_DIR.exists() else []
        created_temp = False
        if sample_candidates:
            sample = min(sample_candidates, key=lambda path: path.stat().st_size)
        else:
            sample = catalog_db.SAMPLES_DIR / "kicks" / "test_fixture.wav"
            sample.parent.mkdir(parents=True, exist_ok=True)
            from test_catalog_db import make_pcm24_stereo_wav
            sample.write_bytes(make_pcm24_stereo_wav())
            created_temp = True
        try:
            relative = sample.relative_to(catalog_db.SAMPLES_DIR).as_posix()
            status, headers, raw = self.request("GET", "/samples/" + quote(relative))
            self.assertEqual(status, 200)
            self.assertEqual(raw, sample.read_bytes())
            self.assertIn("audio", headers["Content-Type"])
        finally:
            if created_temp:
                sample.unlink(missing_ok=True)

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
        with tempfile.TemporaryDirectory(prefix="samplepack-server-") as temp:
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

    def test_import_dir_endpoint_copies_and_catalogs_wavs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="samplepack-import-") as temp:
            base = Path(temp)
            source = base / "incoming"
            write_test_wav(source / "mono_hit.wav", channels=1, width=2, sample_rate=44100)
            db = base / "catalog.sqlite3"
            samples = base / "samples"
            body = json.dumps({"directory": str(source), "category": "hits"}).encode()
            with patch.dict(os.environ, {"SAMPLEPACK_DB": str(db)}), \
                    patch.object(catalog_db, "SAMPLES_DIR", samples), \
                    patch.object(catalog_db, "INDEX_PATH", base / "INDEX.md"), \
                    patch.object(catalog_db, "MANIFEST_PATH", base / "manifest.txt"), \
                    patch.object(server, "SAMPLES_DIR", samples):
                status, _headers, raw = self.request(
                    "POST", "/api/import-dir", body=body,
                    headers={"Content-Type": "application/json"},
                )
            self.assertEqual(status, 200, raw)
            result = json.loads(raw)
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["category"], "hits")
            self.assertTrue((samples / "hits" / "mono_hit.wav").is_file())
            imported = catalog_db.get_sample("hits/mono_hit.wav", db)
            self.assertIsNotNone(imported)
            self.assertEqual(imported["channels"], 1)
            self.assertEqual(imported["bit_depth"], 16)
            self.assertEqual(imported["sr"], 44100)

    def test_desktop_shortcut_endpoint_writes_executable_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="samplepack-desktop-") as temp:
            home = Path(temp)
            (home / "Desktop").mkdir()
            with patch.dict(os.environ, {"HOME": str(home), "XDG_DESKTOP_DIR": ""}):
                status, _headers, raw = self.request(
                    "POST", "/api/desktop-shortcut", body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
            self.assertEqual(status, 200, raw)
            result = json.loads(raw)
            self.assertTrue(result["ok"])
            target = home / "Desktop" / "samplepack-browser.desktop"
            self.assertTrue(target.is_file())
            self.assertTrue(os.access(target, os.X_OK))
            text = target.read_text(encoding="utf-8")
            self.assertIn("[Desktop Entry]", text)
            self.assertIn("Exec=", text)
            self.assertEqual(Path(result["path"]), target)

    def test_windows_desktop_directory_uses_user_profile(self) -> None:
        with patch.object(sys, "platform", "win32"), \
                patch.dict(os.environ, {"USERPROFILE": r"C:\Users\Example"}):
            self.assertEqual(server.desktop_directory(), Path(r"C:\Users\Example") / "Desktop")

    def test_windows_desktop_shortcut_writes_internet_shortcut(self) -> None:
        with tempfile.TemporaryDirectory(prefix="samplepack-desktop-win-") as temp:
            desktop = Path(temp) / "Desktop"
            desktop.mkdir()
            with patch.object(sys, "platform", "win32"), \
                    patch.object(server, "desktop_directory", return_value=desktop), \
                    patch.object(server, "windows_launcher_target", return_value=None):
                result = server.create_desktop_shortcut("http://127.0.0.1:12012/")
            self.assertTrue(result["ok"])
            self.assertEqual(result["kind"], "url")
            target = desktop / "Samplepack Browser.url"
            self.assertEqual(Path(result["path"]), target)
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn("[InternetShortcut]", text)
            self.assertIn("URL=http://127.0.0.1:12012/", text)
            self.assertIn("IconFile=", text)
            self.assertFalse((desktop / "samplepack-browser.desktop").exists())

    def test_windows_desktop_shortcut_prefers_lnk_when_launcher_installed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="samplepack-desktop-lnk-") as temp:
            desktop = Path(temp) / "Desktop"
            desktop.mkdir()
            launcher = Path(temp) / "samplepack-browser.exe"
            launcher.write_text("", encoding="utf-8")

            def fake_lnk(desktop_dir: Path, target: Path, arguments: str) -> Path | None:
                shortcut = desktop_dir / "Samplepack Browser.lnk"
                shortcut.write_text(f"{target}\n{arguments}", encoding="utf-8")
                return shortcut

            with patch.object(sys, "platform", "win32"), \
                    patch.object(server, "desktop_directory", return_value=desktop), \
                    patch.object(server, "windows_launcher_target",
                                 return_value=(launcher, "")), \
                    patch.object(server, "_write_windows_lnk", side_effect=fake_lnk):
                result = server.create_desktop_shortcut("http://127.0.0.1:12012/")
            self.assertTrue(result["ok"])
            self.assertEqual(result["kind"], "lnk")
            target = desktop / "Samplepack Browser.lnk"
            self.assertEqual(Path(result["path"]), target)
            self.assertTrue(target.is_file())
            self.assertFalse((desktop / "Samplepack Browser.url").exists())


class StartupMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="samplepack-startup-")
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
