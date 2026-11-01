#!/usr/bin/env python3
"""SQLite-backed catalog and local review state for the sample browser."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

PACK_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = PACK_DIR / "samples"
DB_PATH = PACK_DIR / "catalog.sqlite3"
LEGACY_CATALOG_PATH = PACK_DIR / "catalog.json"
LEGACY_REVISIT_PATH = PACK_DIR / "revisit_list.json"
INDEX_PATH = PACK_DIR / "INDEX.md"
MANIFEST_PATH = PACK_DIR / "manifest.txt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    file TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_category_idx ON samples(category);
CREATE INDEX IF NOT EXISTS samples_role_idx ON samples(role);
CREATE TABLE IF NOT EXISTS sample_state (
    file TEXT PRIMARY KEY REFERENCES samples(file) ON UPDATE CASCADE ON DELETE CASCADE,
    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    reviewed_date TEXT,
    revisit INTEGER NOT NULL DEFAULT 0 CHECK (revisit IN (0, 1)),
    revisit_reason TEXT,
    revisit_date TEXT
);
"""


def database_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("SAMPLEPACK_DB")
    return Path(configured) if configured else DB_PATH


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    db_path = database_path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    return conn


def now_string() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_file(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("'file' must be a string")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if path.parts and path.parts[0] == "samples":
        path = PurePosixPath(*path.parts[1:])
    if (not raw or path.is_absolute() or "\x00" in raw or
            any(part in ("", ".", "..") for part in path.parts) or
            path.suffix.lower() != ".wav"):
        raise ValueError("'file' must be a safe relative WAV path inside samples/")
    return path.as_posix()


def _metadata_from_catalog(catalog: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(catalog.get("name") or "samplepack"),
        "description": str(catalog.get("description") or "Production sample library."),
        "format": str(catalog.get("format") or "WAV PCM_24 stereo 48000 Hz"),
        "updated": str(catalog.get("updated") or now_string()[:10]),
    }


def _state_from_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "favorite": bool(sample.get("favorite", False)),
        "reviewed": bool(sample.get("reviewed", False)),
        "reviewed_date": sample.get("reviewed_date"),
        "revisit": bool(sample.get("revisit", False)),
        "revisit_reason": sample.get("revisit_reason"),
        "revisit_date": sample.get("revisit_date"),
    }


def _store_sample(conn: sqlite3.Connection, sample: dict[str, Any]) -> None:
    file_name = normalize_file(sample.get("file"))
    category = str(sample.get("category") or PurePosixPath(file_name).parts[0])
    name = str(sample.get("name") or PurePosixPath(file_name).stem)
    role = str(sample.get("role") or "")
    data = dict(sample)
    data["file"] = file_name
    data["category"] = category
    data["name"] = name
    data["role"] = role
    state = _state_from_sample(data)
    for key in ("favorite", "reviewed", "reviewed_date", "revisit", "revisit_reason", "revisit_date"):
        data.pop(key, None)

    conn.execute(
        """INSERT INTO samples(file, category, name, role, data_json)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(file) DO UPDATE SET
             category=excluded.category, name=excluded.name,
             role=excluded.role, data_json=excluded.data_json""",
        (file_name, category, name, role, json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    )
    conn.execute("INSERT OR IGNORE INTO sample_state(file) VALUES (?)", (file_name,))
    conn.execute(
        """UPDATE sample_state SET favorite=?, reviewed=?, reviewed_date=?,
           revisit=?, revisit_reason=?, revisit_date=? WHERE file=?""",
        (int(state["favorite"]), int(state["reviewed"]), state["reviewed_date"],
         int(state["revisit"]), state["revisit_reason"], state["revisit_date"], file_name),
    )


def save_catalog(catalog: dict[str, Any], path: str | os.PathLike[str] | None = None,
                 *, write_indexes: bool = True) -> None:
    """Upsert a complete catalog and retain SQLite as the editable source of truth."""
    samples = catalog.get("samples")
    if not isinstance(samples, list):
        raise ValueError("catalog.samples must be a list")
    conn = connect(path)
    try:
        with conn:
            for key, value in _metadata_from_catalog(catalog).items():
                conn.execute(
                    "INSERT INTO app_metadata(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            reserved = {"samples", "root", "count", "categories", "roles", "uses"}
            extras = {key: value for key, value in catalog.items() if key not in reserved and key not in _metadata_from_catalog(catalog)}
            conn.execute(
                "INSERT INTO app_metadata(key, value) VALUES ('extra', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(extras, ensure_ascii=False),),
            )
            keep: set[str] = set()
            for item in samples:
                if not isinstance(item, dict):
                    raise ValueError("catalog samples must be objects")
                keep.add(normalize_file(item.get("file")))
                _store_sample(conn, item)
            if keep:
                placeholders = ",".join("?" for _ in keep)
                conn.execute(f"DELETE FROM samples WHERE file NOT IN ({placeholders})", tuple(sorted(keep)))
            else:
                conn.execute("DELETE FROM samples")
    finally:
        conn.close()
    if write_indexes:
        generate_indexes(path)


def migrate_legacy(path: str | os.PathLike[str] | None = None) -> int:
    """One-time import of the old JSON catalog and revisit list."""
    db_path = database_path(path)
    conn = connect(db_path)
    try:
        existing = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    finally:
        conn.close()
    catalog_exists = LEGACY_CATALOG_PATH.exists()
    catalog = None
    if catalog_exists:
        catalog = json.loads(LEGACY_CATALOG_PATH.read_text(encoding="utf-8"))
        legacy_count = len(catalog.get("samples", []))
        if existing and existing != legacy_count:
            raise RuntimeError(
                f"database has {existing} samples but legacy catalog has {legacy_count}; "
                "refusing to replace or discard either catalog"
            )
    elif not existing:
        return 0

    revisits: dict[str, dict[str, Any]] = {}
    if LEGACY_REVISIT_PATH.exists():
        revisit_doc = json.loads(LEGACY_REVISIT_PATH.read_text(encoding="utf-8"))
        for entry in revisit_doc.get("samples", []):
            if isinstance(entry, dict) and entry.get("file"):
                revisits[normalize_file(entry["file"])] = entry

    if catalog is not None and not existing:
        for sample in catalog.get("samples", []):
            sample["file"] = normalize_file(sample.get("file"))
            revisit = revisits.get(sample["file"])
            if revisit:
                sample["revisit"] = True
                sample["revisit_reason"] = revisit.get("reason")
                sample["revisit_date"] = revisit.get("date")
                sample["reviewed"] = False
                sample["reviewed_date"] = None
        save_catalog(catalog, db_path)
    elif revisits:
        conn = connect(db_path)
        try:
            with conn:
                for file_name, revisit in revisits.items():
                    conn.execute(
                        """UPDATE sample_state SET revisit=1, revisit_reason=?, revisit_date=?,
                           reviewed=0, reviewed_date=NULL WHERE file=?""",
                        (revisit.get("reason"), revisit.get("date"), file_name),
                    )
        finally:
            conn.close()
    return int(catalog_db_count(db_path))


def catalog_db_count(path: str | os.PathLike[str] | None = None) -> int:
    conn = connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
    finally:
        conn.close()


def load_catalog(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    conn = connect(path)
    try:
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM app_metadata")}
        rows = conn.execute(
            """SELECT s.data_json, st.favorite, st.reviewed, st.reviewed_date,
                      st.revisit, st.revisit_reason, st.revisit_date
               FROM samples s JOIN sample_state st USING(file)
               ORDER BY s.file COLLATE NOCASE"""
        ).fetchall()
    finally:
        conn.close()

    samples: list[dict[str, Any]] = []
    roles: dict[str, list[str]] = defaultdict(list)
    uses: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        sample = json.loads(row["data_json"])
        sample.update({
            "favorite": bool(row["favorite"]),
            "reviewed": bool(row["reviewed"]),
            "reviewed_date": row["reviewed_date"],
            "revisit": bool(row["revisit"]),
            "revisit_reason": row["revisit_reason"],
            "revisit_date": row["revisit_date"],
        })
        samples.append(sample)
        if sample.get("role"):
            roles[str(sample["role"])].append(sample["file"])
        for use in sample.get("use") or []:
            if isinstance(use, str):
                uses[use].append(sample["file"])

    categories = sorted({str(s.get("category")) for s in samples if s.get("category")})
    try:
        extras = json.loads(metadata.get("extra", "{}"))
    except json.JSONDecodeError:
        extras = {}
    return {
        **(extras if isinstance(extras, dict) else {}),
        "name": metadata.get("name", "samplepack"),
        "description": metadata.get("description", "Production sample library."),
        "format": metadata.get("format", "WAV PCM_24 stereo 48000 Hz"),
        "updated": metadata.get("updated", now_string()[:10]),
        "root": str(PACK_DIR),
        "count": len(samples),
        "categories": categories,
        "roles": {key: roles[key] for key in sorted(roles)},
        "uses": {key: uses[key] for key in sorted(uses)},
        "samples": samples,
    }


def get_sample(file_name: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    file_name = normalize_file(file_name)
    conn = connect(path)
    try:
        row = conn.execute(
            """SELECT s.data_json, st.favorite, st.reviewed, st.reviewed_date,
                      st.revisit, st.revisit_reason, st.revisit_date
               FROM samples s JOIN sample_state st USING(file) WHERE s.file=?""",
            (file_name,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    sample = json.loads(row["data_json"])
    sample.update({
        "favorite": bool(row["favorite"]), "reviewed": bool(row["reviewed"]),
        "reviewed_date": row["reviewed_date"], "revisit": bool(row["revisit"]),
        "revisit_reason": row["revisit_reason"], "revisit_date": row["revisit_date"],
    })
    return sample


def add_sample(sample: dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    conn = connect(path)
    try:
        with conn:
            if conn.execute("SELECT 1 FROM samples WHERE file=?", (normalize_file(sample.get("file")),)).fetchone():
                raise ValueError("sample already exists")
            _store_sample(conn, sample)
    finally:
        conn.close()
    generate_indexes(path)


def update_sample(old_file: str, sample: dict[str, Any],
                  path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    old_file = normalize_file(old_file)
    new_file = normalize_file(sample.get("file"))
    conn = connect(path)
    try:
        with conn:
            current = conn.execute("SELECT 1 FROM samples WHERE file=?", (old_file,)).fetchone()
            if current is None:
                raise KeyError(old_file)
            if new_file != old_file and conn.execute("SELECT 1 FROM samples WHERE file=?", (new_file,)).fetchone():
                raise FileExistsError(new_file)
            if new_file != old_file:
                conn.execute("UPDATE samples SET file=? WHERE file=?", (new_file, old_file))
            _store_sample(conn, sample)
    finally:
        conn.close()
    generate_indexes(path)
    result = get_sample(new_file, path)
    assert result is not None
    return result


def delete_sample(file_name: str, path: str | os.PathLike[str] | None = None) -> bool:
    file_name = normalize_file(file_name)
    conn = connect(path)
    try:
        with conn:
            cursor = conn.execute("DELETE FROM samples WHERE file=?", (file_name,))
            deleted = cursor.rowcount > 0
    finally:
        conn.close()
    if deleted:
        generate_indexes(path)
    return deleted


def set_state(file_name: str, *, favorite: bool | None = None, reviewed: bool | None = None,
              flagged: bool | None = None, reason: str | None = None,
              path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    file_name = normalize_file(file_name)
    conn = connect(path)
    try:
        with conn:
            if conn.execute("SELECT 1 FROM samples WHERE file=?", (file_name,)).fetchone() is None:
                raise KeyError(file_name)
            conn.execute("INSERT OR IGNORE INTO sample_state(file) VALUES (?)", (file_name,))
            if favorite is not None:
                conn.execute("UPDATE sample_state SET favorite=? WHERE file=?", (int(favorite), file_name))
            if reviewed is not None:
                stamp = now_string() if reviewed else None
                conn.execute(
                    "UPDATE sample_state SET reviewed=?, reviewed_date=?, "
                    "revisit=CASE WHEN ? THEN 0 ELSE revisit END, "
                    "revisit_reason=CASE WHEN ? THEN NULL ELSE revisit_reason END, "
                    "revisit_date=CASE WHEN ? THEN NULL ELSE revisit_date END WHERE file=?",
                    (int(reviewed), stamp, int(reviewed), int(reviewed), int(reviewed), file_name),
                )
            if flagged is not None:
                stamp = now_string() if flagged else None
                conn.execute(
                    "UPDATE sample_state SET revisit=?, revisit_reason=?, revisit_date=?, "
                    "reviewed=CASE WHEN ? THEN 0 ELSE reviewed END, "
                    "reviewed_date=CASE WHEN ? THEN NULL ELSE reviewed_date END WHERE file=?",
                    (int(flagged), (reason or "Flagged for revisit") if flagged else None,
                     stamp, int(flagged), int(flagged), file_name),
                )
    finally:
        conn.close()
    result = get_sample(file_name, path)
    assert result is not None
    return result


def set_favorites(files: Iterable[str], *, merge: bool = False,
                  path: str | os.PathLike[str] | None = None) -> list[str]:
    normalized = sorted({normalize_file(item) for item in files})
    conn = connect(path)
    try:
        with conn:
            if not merge:
                conn.execute("UPDATE sample_state SET favorite=0")
            for file_name in normalized:
                conn.execute("UPDATE sample_state SET favorite=1 WHERE file=?", (file_name,))
    finally:
        conn.close()
    return list_favorites(path)


def list_favorites(path: str | os.PathLike[str] | None = None) -> list[str]:
    conn = connect(path)
    try:
        return [row[0] for row in conn.execute(
            "SELECT file FROM sample_state WHERE favorite=1 ORDER BY file COLLATE NOCASE"
        )]
    finally:
        conn.close()


def search_samples(*, query: str = "", category: str | None = None,
                   role: str | None = None, use: str | None = None,
                   tag: str | None = None, reviewed: bool | None = None,
                   flagged: bool | None = None, favorite: bool | None = None,
                   limit: int = 30,
                   path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    catalog = load_catalog(path)
    needle = query.strip().casefold()
    terms = [term for term in re.split(r"\s+", needle) if term]
    results = []
    for sample in catalog["samples"]:
        if category and sample.get("category", "").casefold() != category.casefold():
            continue
        if role and sample.get("role", "").casefold() != role.casefold():
            continue
        if use and use.casefold() not in {str(x).casefold() for x in sample.get("use") or []}:
            continue
        if tag and tag.casefold() not in {str(x).casefold() for x in sample.get("tags") or []}:
            continue
        if reviewed is not None and bool(sample.get("reviewed")) != reviewed:
            continue
        if flagged is not None and bool(sample.get("revisit")) != flagged:
            continue
        if favorite is not None and bool(sample.get("favorite")) != favorite:
            continue
        haystack = json.dumps(sample, ensure_ascii=False).casefold()
        if terms and not all(term in haystack for term in terms):
            continue
        results.append(sample)
    return results[:max(1, min(int(limit), 200))]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_index(catalog: dict[str, Any]) -> str:
    samples = catalog["samples"]
    categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    role_counts: dict[str, int] = defaultdict(int)
    for sample in samples:
        categories[str(sample.get("category") or "uncategorized")].append(sample)
        if sample.get("role"):
            role_counts[str(sample["role"])] += 1

    lines = [
        "# Samplepack index (generated from catalog.sqlite3)",
        "",
        f"Root: `{PACK_DIR}`",
        f"Audio root: `{SAMPLES_DIR}`",
        f"Format: {catalog.get('format', 'WAV PCM_24 stereo 48000 Hz')}",
        f"Count: **{len(samples)}** production files.",
        "Machine catalog and review state: `catalog.sqlite3` (query through the web API or samplepack MCP).",
        "",
        "## How to select a sample",
        "1. Search with the samplepack MCP `search_samples` tool or inspect the category tables below.",
        "2. Call `select_sample` to verify the asset and obtain its local path.",
        "3. In project files, use `samplepack/samples/<category>/<filename>.wav`.",
        "4. `dry_processed: dry` with no `parent` marks a canonical original; a `parent` identifies a derived pack sample.",
        "",
        "## Do not use",
        "- `samplepack/source/` — archival sources, engines, LMMS patches, and dry extracts.",
        "- `samplepack/work/` — batch renders, layers, and precut backups.",
        "- `samplepack/sapolsky samples/` — staging copies; use the catalogued speech samples instead.",
        "- `*_assets/stem_*.wav`, `preview_mix.wav`, and `*_src.wav` — full-song stems.",
        "",
        "## Roles",
        "",
        "| role | n | when to use |",
        "|---|---:|---|",
    ]
    for role, count in sorted(role_counts.items()):
        lines.append(f"| `{role}` | {count} | See sample descriptions and tags below. |")
    lines += ["", "## Categories", ""]
    for category in sorted(categories):
        entries = sorted(categories[category], key=lambda s: str(s.get("file", "")).casefold())
        lines += [
            f"### {category} ({len(entries)})",
            "",
            "| file | dur | role | use | description |",
            "|---|---:|---|---|---|",
        ]
        for sample in entries:
            file_name = str(sample.get("file", ""))
            duration = sample.get("duration")
            dur = f"{float(duration):.2f}" if isinstance(duration, (int, float)) else ""
            role = str(sample.get("role") or "")
            use = ",".join(str(x) for x in sample.get("use") or [])
            description = str(sample.get("description") or "").replace("|", "\\|").replace("\n", " ").strip()
            tags = ", ".join(str(x) for x in sample.get("tags") or [])
            if tags:
                description = f"{description} [{tags}]" if description else f"[{tags}]"
            lines.append(f"| `{file_name}` | {dur} | {role} | {use} | {description} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_indexes(path: str | os.PathLike[str] | None = None) -> None:
    catalog = load_catalog(path)
    _atomic_write(INDEX_PATH, render_index(catalog))
    manifest = "\n".join(
        f"{('samples/' + sample['file']):<72} {sample.get('description') or ''}".rstrip()
        for sample in catalog["samples"]
    ) + "\n"
    _atomic_write(MANIFEST_PATH, manifest)


def migrate_legacy_files(path: str | os.PathLike[str] | None = None) -> int:
    """Import first, then remove ignored legacy JSON state once the DB is verified."""
    had_legacy_catalog = LEGACY_CATALOG_PATH.exists()
    had_legacy_revisit = LEGACY_REVISIT_PATH.exists()
    count = migrate_legacy(path)
    catalog = load_catalog(path)
    if count != len(catalog["samples"]):
        raise RuntimeError("legacy catalog migration count did not validate")
    if had_legacy_catalog:
        legacy_count = len(json.loads(LEGACY_CATALOG_PATH.read_text(encoding="utf-8")).get("samples", []))
        if len(catalog["samples"]) != legacy_count:
            raise RuntimeError(
                f"database has {len(catalog['samples'])} samples but legacy catalog has {legacy_count}; "
                "refusing to remove the legacy catalog"
            )
        expected = {normalize_file(s["file"]) for s in catalog["samples"]}
        available = {p.relative_to(SAMPLES_DIR).as_posix() for p in SAMPLES_DIR.rglob("*.wav")} if SAMPLES_DIR.exists() else set()
        missing = sorted(expected - available)
        if missing:
            raise RuntimeError(f"sample files missing under samples/: {missing[:10]}")
        LEGACY_CATALOG_PATH.unlink(missing_ok=True)
    if had_legacy_revisit:
        LEGACY_REVISIT_PATH.unlink(missing_ok=True)
        (PACK_DIR / "revisit_list.txt").unlink(missing_ok=True)
    generate_indexes(path)
    return count


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "export-index", "count"))
    args = parser.parse_args()
    if args.command == "migrate":
        print(f"Migrated and verified {migrate_legacy_files()} samples.")
    elif args.command == "export-index":
        generate_indexes()
        print(f"Generated {INDEX_PATH}")
    else:
        print(len(load_catalog()["samples"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
