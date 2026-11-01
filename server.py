#!/usr/bin/env python3
"""Static samplepack browser plus small sample edit/delete APIs.

Serves this directory like ``python3 -m http.server`` and adds:

    POST   /api/edit      PUT /api/sample   edit metadata (and category)
    POST   /api/delete    DELETE /api/sample   delete a sample

Delete accepts a JSON body ``{"file": "<path relative to samplepack/>"}``.
Edit accepts the same ``file`` plus the metadata fields ``name``,
``category``, ``role``, ``suggested_use``, ``tags``, ``transcript`` and
``description``. Renaming the category moves the WAV into the new
category directory; every generated index follows the move.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

PACK_DIR = Path(__file__).resolve().parent
CATALOG_PATH = PACK_DIR / "catalog.json"
PROVENANCE_PATH = PACK_DIR / "source" / "archival" / "provenance.json"
INDEX_PATH = PACK_DIR / "INDEX.md"
MANIFEST_PATH = PACK_DIR / "manifest.txt"
REVISIT_JSON_PATH = PACK_DIR / "revisit_list.json"
REVISIT_TXT_PATH = PACK_DIR / "revisit_list.txt"

MAX_BODY_BYTES = 64 * 1024
MUTATION_LOCK = threading.Lock()
FORBIDDEN_PARTS = {"source", ".git"}
SAFE_CATEGORY_RE = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)
TAG_SPLIT_RE = re.compile(r"[,;\n]+")
USLT_CHUNK_IDS = (b"id3 ", b"ID3 ")


class DeleteError(Exception):
    """A client-visible validation or processing error.

    The name predates editing support; both APIs raise it so the HTTP
    handler can map it to a status code in one place.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# Editing shares the same error contract as deletion.
EditError = DeleteError


def slugify(value: str) -> str:
    """Collapse text into the lowercase underscore slugs used by catalog uses."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def clean_text(raw: object, field: str, required: bool = False) -> str:
    """Validate and trim one optional or required string field."""
    if raw is None:
        if required:
            raise EditError(f"'{field}' is required")
        return ""
    if not isinstance(raw, str):
        raise EditError(f"'{field}' must be a string")
    value = raw.strip()
    if required and not value:
        raise EditError(f"'{field}' is required")
    return value


def clean_tags(raw: object) -> list[str]:
    """Normalize tags given as a list or a comma/semicolon/newline string."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts: list[str] = TAG_SPLIT_RE.split(raw)
    elif isinstance(raw, list):
        parts = []
        for item in raw:
            if not isinstance(item, str):
                raise EditError("'tags' must contain strings")
            parts.extend(TAG_SPLIT_RE.split(item))
    else:
        raise EditError("'tags' must be a list or a comma-separated string")

    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = part.strip().lstrip("#").strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def validate_category(raw: object) -> str:
    """Return a clean category directory slug or raise EditError."""
    if not isinstance(raw, str):
        raise EditError("'category' must be a string")
    category = raw.strip()
    if not category:
        raise EditError("'category' is required")
    if not SAFE_CATEGORY_RE.match(category):
        raise EditError(
            "'category' must be a simple directory slug using letters, digits, '-' or '_'"
        )
    if category.lower() in FORBIDDEN_PARTS:
        raise EditError("'category' must not be source or .git")
    return category


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to *path* via a same-directory temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def validate_rel_file(raw: object) -> str:
    """Return a normalized pack-relative WAV path or raise DeleteError."""
    if not isinstance(raw, str):
        raise DeleteError("'file' must be a string")
    rel = raw.strip().replace("\\", "/")
    if not rel:
        raise DeleteError("'file' is required")
    if "\x00" in rel:
        raise DeleteError("'file' contains a NUL byte")
    if rel.startswith("/"):
        raise DeleteError("'file' must be relative to samplepack/")

    rel_path = PurePosixPath(rel)
    if rel_path.is_absolute() or any(part in ("", ".", "..") for part in rel_path.parts):
        raise DeleteError("'file' contains an invalid path segment")
    if rel_path.suffix.lower() != ".wav":
        raise DeleteError("'file' must end with .wav")
    if any(part in FORBIDDEN_PARTS for part in rel_path.parts):
        raise DeleteError("'file' must not point into source/ or .git/")

    target = (PACK_DIR / Path(*rel_path.parts)).resolve()
    try:
        target.relative_to(PACK_DIR)
    except ValueError:
        raise DeleteError("'file' escapes the samplepack directory") from None
    if target.exists() and not target.is_file():
        raise DeleteError("'file' is not a regular file")

    return target.relative_to(PACK_DIR).as_posix()


def read_wav_chunks(path: Path) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """Return ``(chunks, trailing_bytes)`` for a RIFF/WAVE file.

    Each chunk is ``(id, payload)`` with the RIFF pad byte removed. Any bytes
    after the last complete chunk are returned untouched so a rewrite does not
    drop them.
    """
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise EditError("file is not a RIFF/WAVE audio file")

    chunks: list[tuple[bytes, bytes]] = []
    pos = 12
    total = len(data)
    while pos + 8 <= total:
        chunk_id = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        start = pos + 8
        end = start + size
        if end > total:
            raise EditError("file is a truncated or corrupt WAV")
        chunks.append((chunk_id, data[start:end]))
        pos = end + (size & 1)
    tail = data[pos:]
    return chunks, tail


def build_uslt_tag(transcript: str) -> bytes:
    """Build an ID3v2.3 tag holding one UTF-16 USLT frame.

    Matches the frame layout already used across the pack: encoding byte 0x01,
    language ``eng``, empty content descriptor, then the transcript.
    """
    body = b"\x01eng\xff\xfe\x00\x00" + transcript.encode("utf-16")
    frame = b"USLT" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    tag_size = len(frame)
    syncsafe = bytes((
        (tag_size >> 21) & 0x7F,
        (tag_size >> 14) & 0x7F,
        (tag_size >> 7) & 0x7F,
        tag_size & 0x7F,
    ))
    return b"ID3\x03\x00\x00" + syncsafe + frame


def write_wav_uslt(path: Path, transcript: str) -> None:
    """Set (or remove, when *transcript* is empty) the WAV's USLT tag."""
    chunks, tail = read_wav_chunks(path)
    replacement = build_uslt_tag(transcript) if transcript else None

    out: list[bytes] = []
    used = False
    for chunk_id, payload in chunks:
        if chunk_id in USLT_CHUNK_IDS:
            if replacement is not None and not used:
                out.append(_pack_chunk(b"id3 ", replacement))
                used = True
            continue
        out.append(_pack_chunk(chunk_id, payload))
    if replacement is not None and not used:
        out.append(_pack_chunk(b"id3 ", replacement))

    body = b"".join(out) + tail
    data = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body
    tmp_path = path.with_name(f".{path.name}.edit.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _pack_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def read_catalog() -> dict:
    """Load catalog.json or raise a 500-flavoured EditError."""
    try:
        with CATALOG_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise EditError("catalog.json not found", status=500) from None
    except json.JSONDecodeError as exc:
        raise EditError(f"catalog.json is not valid JSON: {exc}", status=500) from None


def rebuild_catalog_totals(catalog: dict) -> None:
    """Recompute count, categories, roles and uses from catalog["samples"]."""
    samples = catalog.get("samples", [])
    catalog["count"] = len(samples)
    catalog["categories"] = sorted(
        {s.get("category") for s in samples if s.get("category")}
    )

    roles: dict[str, list[str]] = {}
    uses: dict[str, list[str]] = {}
    for sample in samples:
        file_name = sample.get("file")
        if not file_name:
            continue
        role = sample.get("role")
        if role:
            roles.setdefault(role, []).append(file_name)
        for use in sample.get("use", []) or []:
            uses.setdefault(use, []).append(file_name)
    catalog["roles"] = dict(sorted(roles.items()))
    catalog["uses"] = dict(sorted(uses.items()))


def prune_catalog(rel_file: str) -> dict:
    """Remove the sample from catalog.json and return the updated catalog."""
    catalog = read_catalog()
    catalog["samples"] = [
        s for s in catalog.get("samples", []) if s.get("file") != rel_file
    ]
    rebuild_catalog_totals(catalog)
    atomic_write_json(CATALOG_PATH, catalog)
    return catalog


def prune_provenance(rel_file: str) -> None:
    """Remove extract entries that point at the deleted pack file."""
    if not PROVENANCE_PATH.exists():
        return
    try:
        with PROVENANCE_PATH.open("r", encoding="utf-8") as handle:
            provenance = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DeleteError(f"could not read provenance.json: {exc}", status=500) from None

    extracts = provenance.get("extracts")
    if isinstance(extracts, list):
        provenance["extracts"] = [
            entry
            for entry in extracts
            if not (
                isinstance(entry, dict)
                and (
                    entry.get("file") == rel_file
                    or entry.get("pack_file") == rel_file
                )
            )
        ]
        atomic_write_json(PROVENANCE_PATH, provenance)


def prune_index(rel_file: str, samples: list[dict]) -> None:
    """Drop the INDEX.md row and refresh count, category and role totals."""
    if not INDEX_PATH.exists():
        return
    text = INDEX_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    row_prefix = f"| `{rel_file}`"
    kept = [
        line
        for line in lines
        if not (line.lstrip().startswith(row_prefix) or row_prefix in line)
    ]

    category_counts = Counter(s.get("category") for s in samples if s.get("category"))
    role_counts = Counter(s.get("role") for s in samples if s.get("role"))

    updated: list[str] = []
    for line in kept:
        stripped = line.rstrip("\n")

        if stripped.startswith("Count:"):
            stripped = re.sub(
                r"Count:\s*\*\*\d+\*\*\s+production files\.",
                f"Count: **{len(samples)}** production files.",
                stripped,
            )
        else:
            category_match = re.match(r"^###\s+(\S+)\s+\(\d+\)\s*$", stripped)
            if category_match:
                category = category_match.group(1)
                stripped = f"### {category} ({category_counts.get(category, 0)})"

            role_match = re.match(r"^\|\s+`([^`]+)`\s+\|\s+\d+\s+\|", stripped)
            if role_match:
                role = role_match.group(1)
                stripped = re.sub(
                    r"^(\|\s+`" + re.escape(role) + r"`\s+\|\s+)\d+(\s+\|)",
                    lambda m: f"{m.group(1)}{role_counts.get(role, 0)}{m.group(2)}",
                    stripped,
                )

        updated.append(stripped + "\n")

    atomic_write_text(INDEX_PATH, "".join(updated))


def prune_manifest(rel_file: str, sample_count: int) -> None:
    """Drop manifest lines mentioning the sample and refresh the total."""
    if not MANIFEST_PATH.exists():
        return
    basename = PurePosixPath(rel_file).name
    lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [
        line
        for line in lines
        if rel_file not in line and basename not in line
    ]
    text = re.sub(
        r"\(\s*\d+\s+production files\s*\)",
        f"({sample_count} production files)",
        "".join(kept),
    )
    atomic_write_text(MANIFEST_PATH, text)


def prune_revisit(rel_file: str) -> None:
    """Remove a sample from revisit_list.json and revisit_list.txt."""
    if not REVISIT_JSON_PATH.exists():
        return
    try:
        data = json.loads(REVISIT_JSON_PATH.read_text(encoding="utf-8"))
        samples = [s for s in data.get("samples", []) if s.get("file") != rel_file]
        if len(samples) != len(data.get("samples", [])):
            now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            data["count"] = len(samples)
            data["updated"] = now_str
            data["samples"] = samples
            atomic_write_json(REVISIT_JSON_PATH, data)
            txt_lines = [
                "# Samplepack Revisit List",
                "# Format: <file> | <reason> | <date>",
                f"# Total flagged: {len(samples)} (Updated {now_str})",
                "",
            ]
            for s in samples:
                txt_lines.append(f"{s['file']} | {s.get('reason', '')} | {s.get('date', '')}")
            REVISIT_TXT_PATH.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def delete_sample(raw_file: object) -> dict:
    """Delete one pack WAV and prune every generated index. Idempotent."""
    rel_file = validate_rel_file(raw_file)
    target = PACK_DIR / rel_file

    with MUTATION_LOCK:
        if target.exists():
            try:
                target.unlink()
            except OSError as exc:
                raise DeleteError(f"could not delete {rel_file}: {exc}", status=500) from None

        catalog = prune_catalog(rel_file)
        prune_provenance(rel_file)
        prune_index(rel_file, catalog["samples"])
        prune_manifest(rel_file, catalog["count"])
        prune_revisit(rel_file)

    return {"ok": True, "deleted": rel_file, "count": catalog["count"]}


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

CATEGORY_HEADER_RE = re.compile(r"^###\s+(\S+)\s+\((\d+)\)\s*$")
ROLE_ROW_RE = re.compile(r"^\|\s+`([^`]+)`\s+\|\s+\d+\s+\|(.*)\|\s*$")


def _categories_region(lines: list[str]) -> tuple[int, int] | None:
    """Return the line span holding every ``### category`` section."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## Categories"):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start, end


def _index_sections(
    lines: list[str], start: int, end: int
) -> tuple[list[str], list[dict]]:
    """Split the category region into preamble lines and per-category sections."""
    preamble: list[str] = []
    sections: list[dict] = []
    current: dict | None = None
    for i in range(start, end):
        line = lines[i]
        header = CATEGORY_HEADER_RE.match(line.rstrip("\n"))
        if header:
            current = {"category": header.group(1), "head": [line], "rows": []}
            sections.append(current)
        elif current is None:
            preamble.append(line)
        elif line.lstrip().startswith("| `"):
            current["rows"].append(line)
        elif not current["rows"]:
            current["head"].append(line)
        # Blank separators after the rows are dropped and rebuilt on output.
    return preamble, sections


def _index_row_parts(line: str) -> tuple[str, str, str, str] | None:
    """Return ``(file, dur, use, description)`` for one INDEX.md table row."""
    parts = line.rstrip("\n").split("|")
    if len(parts) < 6:
        return None
    rel = parts[1].strip().strip("`")
    if not rel:
        return None
    return rel, parts[2].strip(), parts[3].strip(), "|".join(parts[4:-1]).strip()


def _render_index_row(rel_file: str, sample: dict, fallback_description: str) -> str:
    duration = sample.get("duration")
    dur = f"{float(duration):.2f}" if isinstance(duration, (int, float)) else ""
    uses = ",".join(sample.get("use") or [])
    description = sample.get("description")
    text = fallback_description if description is None else str(description)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    return f"| `{rel_file}` | {dur} | {uses} | {text} |\n"


def _sorted_insert(rows: list[str], row: str, rel_file: str) -> None:
    for i, existing in enumerate(rows):
        parts = _index_row_parts(existing)
        if parts and parts[0] > rel_file:
            rows.insert(i, row)
            return
    rows.append(row)


def edit_index(old_rel: str, new_rel: str, sample: dict, samples: list[dict]) -> None:
    """Move/update the INDEX.md row and refresh every count."""
    if not INDEX_PATH.exists():
        return

    lines = INDEX_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    category_counts = Counter(s.get("category") for s in samples if s.get("category"))
    role_counts = Counter(s.get("role") for s in samples if s.get("role"))
    region = _categories_region(lines)

    if region is not None:
        start, end = region
        preamble, sections = _index_sections(lines, start, end)

        old_section = old_row = None
        old_description = ""
        for si, section in enumerate(sections):
            for ri, row in enumerate(section["rows"]):
                parts = _index_row_parts(row)
                if parts and parts[0] == old_rel:
                    old_section, old_row = si, ri
                    old_description = parts[3]
                    break
            if old_section is not None:
                break

        row = _render_index_row(new_rel, sample, old_description)
        if old_section is not None and old_row is not None:
            sections[old_section]["rows"].pop(old_row)

        new_category = sample.get("category") or ""
        target = next(
            (s for s in sections if s["category"] == new_category), None
        )
        if target is None:
            target = {
                "category": new_category,
                "head": [
                    f"### {new_category} ({category_counts.get(new_category, 0)})\n",
                    "\n",
                    "| file | dur | use | description |\n",
                    "|---|---:|---|---|\n",
                ],
                "rows": [],
            }
            position = len(sections)
            for i, section in enumerate(sections):
                if section["category"] > new_category:
                    position = i
                    break
            sections.insert(position, target)

        _sorted_insert(target["rows"], row, new_rel)

        rebuilt = list(preamble)
        for section in sections:
            body = list(section["head"])
            body[0] = (
                f"### {section['category']} "
                f"({category_counts.get(section['category'], 0)})\n"
            )
            rebuilt.extend(body)
            rebuilt.extend(section["rows"])
            rebuilt.append("\n")
        lines = lines[:start] + rebuilt + lines[end:]

    count_line = re.compile(r"Count:\s*\*\*\d+\*\*\s+production files\.")
    updated: list[str] = []
    in_roles = False
    last_role_row = None
    seen_roles: set[str] = set()
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped.startswith("## "):
            in_roles = stripped.strip() == "## Roles"
        if stripped.startswith("Count:"):
            line = count_line.sub(
                f"Count: **{len(samples)}** production files.", line, count=1
            )
        elif in_roles:
            match = ROLE_ROW_RE.match(stripped)
            if match:
                role = match.group(1)
                seen_roles.add(role)
                last_role_row = len(updated)
                line = f"| `{role}` | {role_counts.get(role, 0)} |{match.group(2)}|\n"
        updated.append(line)

    missing_roles = sorted(set(role_counts) - seen_roles)
    if missing_roles and last_role_row is not None:
        new_rows = [
            f"| `{role}` | {role_counts[role]} |  |\n" for role in missing_roles
        ]
        updated[last_role_row + 1:last_role_row + 1] = new_rows

    atomic_write_text(INDEX_PATH, "".join(updated))


def edit_provenance(
    old_rel: str,
    new_rel: str,
    transcript: str,
    suggested_use: str,
    transcript_provided: bool,
    suggested_use_provided: bool,
) -> None:
    """Follow the moved file in provenance.json and refresh edited metadata."""
    if not PROVENANCE_PATH.exists():
        return
    try:
        with PROVENANCE_PATH.open("r", encoding="utf-8") as handle:
            provenance = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise EditError(f"could not read provenance.json: {exc}", status=500) from None

    extracts = provenance.get("extracts")
    if not isinstance(extracts, list):
        return

    changed = False
    for entry in extracts:
        if not isinstance(entry, dict):
            continue
        if entry.get("file") != old_rel and entry.get("pack_file") != old_rel:
            continue
        entry["file"] = new_rel
        entry["pack_file"] = new_rel
        if transcript_provided:
            if transcript:
                entry["transcript"] = transcript
            else:
                entry.pop("transcript", None)
        if suggested_use_provided:
            if suggested_use:
                entry["suggested_use"] = suggested_use
            else:
                entry.pop("suggested_use", None)
        changed = True

    if changed:
        atomic_write_json(PROVENANCE_PATH, provenance)


def edit_manifest(old_rel: str, new_rel: str) -> None:
    """Point every manifest mention of the sample at its new path."""
    if not MANIFEST_PATH.exists() or old_rel == new_rel:
        return
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    if old_rel not in text:
        return
    atomic_write_text(MANIFEST_PATH, text.replace(old_rel, new_rel))


def edit_sample(payload: dict) -> dict:
    """Apply an edit to one pack WAV and every generated index."""
    old_rel = validate_rel_file(payload.get("file"))
    source = PACK_DIR / old_rel
    if not source.exists():
        raise EditError(f"sample not found: {old_rel}", status=404)
    if not source.is_file():
        raise EditError(f"not a regular file: {old_rel}")

    new_category = validate_category(payload.get("category"))
    name = clean_text(payload.get("name"), "name", required=True)
    role = clean_text(payload.get("role"), "role", required=True)
    tags = clean_tags(payload.get("tags"))

    with MUTATION_LOCK:
        catalog = read_catalog()
        samples = catalog.get("samples", [])
        entry = next((s for s in samples if s.get("file") == old_rel), None)
        if entry is None:
            raise EditError(
                f"sample not found in catalog.json: {old_rel}", status=404
            )

        if "suggested_use" in payload:
            suggested_use = clean_text(payload["suggested_use"], "suggested_use")
        else:
            suggested_use = entry.get("suggested_use") or ""

        if "transcript" in payload:
            transcript = clean_text(payload["transcript"], "transcript")
        else:
            transcript = entry.get("transcript") or ""

        if "description" in payload:
            description = clean_text(payload["description"], "description")
        else:
            description = entry.get("description")

        old_category = entry.get("category")
        if new_category != old_category:
            new_rel = f"{new_category}/{PurePosixPath(old_rel).name}"
            destination = PACK_DIR / new_rel
            if destination.exists() and destination.resolve() != source.resolve():
                raise EditError(
                    f"destination already exists: {new_rel}", status=409
                )
        else:
            new_rel = old_rel
            destination = None

        # Tag the WAV before moving it so a malformed file fails cleanly.
        if "transcript" in payload:
            try:
                write_wav_uslt(source, transcript)
            except EditError:
                raise
            except OSError as exc:
                raise EditError(
                    f"could not update WAV tags for {old_rel}: {exc}", status=500
                ) from None

        if destination is not None:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
            except OSError as exc:
                raise EditError(
                    f"could not move {old_rel} to {new_rel}: {exc}", status=500
                ) from None

        entry["file"] = new_rel
        entry["name"] = name
        entry["category"] = new_category
        entry["role"] = role
        entry["tags"] = tags
        entry["transcript"] = transcript
        entry["description"] = description
        entry["suggested_use"] = suggested_use

        uses = [u for u in (entry.get("use") or []) if isinstance(u, str)]
        for candidate in (role, slugify(suggested_use)):
            if candidate and candidate not in uses:
                uses.append(candidate)
        entry["use"] = uses

        rebuild_catalog_totals(catalog)
        atomic_write_json(CATALOG_PATH, catalog)

        edit_provenance(
            old_rel,
            new_rel,
            transcript,
            suggested_use,
            "transcript" in payload,
            "suggested_use" in payload,
        )
        edit_index(old_rel, new_rel, entry, samples)
        edit_manifest(old_rel, new_rel)

        return {"ok": True, "sample": entry, "count": catalog["count"]}


def get_revisit_list() -> dict:
    if not REVISIT_JSON_PATH.exists():
        return {"ok": True, "count": 0, "samples": []}
    try:
        with open(REVISIT_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "ok": True,
                "count": len(data.get("samples", [])),
                "updated": data.get("updated"),
                "samples": data.get("samples", []),
            }
    except Exception as exc:
        return {"ok": True, "count": 0, "samples": [], "error": str(exc)}


def toggle_revisit(payload: dict) -> dict:
    rel_path = (payload.get("file") or "").strip()
    if not rel_path:
        raise EditError("file path is required")
    flagged = bool(payload.get("flagged", True))
    reason = (payload.get("reason") or "").strip() or None

    with MUTATION_LOCK:
        revisit_data = {"count": 0, "samples": []}
        if REVISIT_JSON_PATH.exists():
            try:
                revisit_data = json.loads(REVISIT_JSON_PATH.read_text(encoding="utf-8"))
            except Exception:
                revisit_data = {"count": 0, "samples": []}

        existing_samples = [s for s in revisit_data.get("samples", []) if s.get("file") != rel_path]

        now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if flagged:
            existing_samples.append({
                "file": rel_path,
                "reason": reason or "Flagged for revisit",
                "date": now_str,
            })

        existing_samples.sort(key=lambda s: s["file"])
        revisit_data = {
            "count": len(existing_samples),
            "updated": now_str,
            "samples": existing_samples,
        }

        atomic_write_json(REVISIT_JSON_PATH, revisit_data)

        # Write clean plain text list for agents/human reading
        txt_lines = [
            "# Samplepack Revisit List",
            "# Format: <file> | <reason> | <date>",
            f"# Total flagged: {len(existing_samples)} (Updated {now_str})",
            "",
        ]
        for s in existing_samples:
            txt_lines.append(f"{s['file']} | {s.get('reason', '')} | {s.get('date', '')}")
        REVISIT_TXT_PATH.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

        # Also update catalog.json for that entry
        catalog = read_catalog()
        for s in catalog.get("samples", []):
            if s.get("file") == rel_path:
                s["revisit"] = flagged
                s["revisit_reason"] = reason if flagged else None
                s["revisit_date"] = now_str if flagged else None
                if flagged:
                    s["reviewed"] = False
                    s["reviewed_date"] = None
                break
        atomic_write_json(CATALOG_PATH, catalog)

        return {
            "ok": True,
            "file": rel_path,
            "flagged": flagged,
            "reason": reason,
            "count": len(existing_samples),
            "samples": existing_samples,
            "reviewed": False if flagged else None,
        }


def get_reviewed_list() -> dict:
    catalog = read_catalog()
    samples = catalog.get("samples", [])
    reviewed = [
        {
            "file": s.get("file"),
            "name": s.get("name"),
            "reviewed_date": s.get("reviewed_date"),
        }
        for s in samples
        if s.get("reviewed")
    ]
    return {
        "ok": True,
        "count": len(reviewed),
        "total": len(samples),
        "samples": reviewed,
    }


def set_reviewed(payload: dict) -> dict:
    rel_path = validate_rel_file(payload.get("file"))
    reviewed = bool(payload.get("reviewed", True))
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    with MUTATION_LOCK:
        catalog = read_catalog()
        samples = catalog.get("samples", [])
        target = None
        for s in samples:
            if s.get("file") == rel_path:
                target = s
                s["reviewed"] = reviewed
                s["reviewed_date"] = now_str if reviewed else None
                if reviewed and s.get("revisit"):
                    s["revisit"] = False
                    s["revisit_reason"] = None
                    s["revisit_date"] = None
                break

        if target is None:
            raise EditError(f"sample not found in catalog.json: {rel_path}", status=404)

        if reviewed:
            prune_revisit(rel_path)

        reviewed_count = sum(1 for s in samples if s.get("reviewed"))
        revisit_count = sum(1 for s in samples if s.get("revisit"))
        atomic_write_json(CATALOG_PATH, catalog)

        return {
            "ok": True,
            "file": rel_path,
            "reviewed": reviewed,
            "reviewed_date": target.get("reviewed_date"),
            "reviewed_count": reviewed_count,
            "revisit_count": revisit_count,
            "sample": target,
        }


class SamplepackHandler(SimpleHTTPRequestHandler):
    server_version = "SamplepackHTTP/1.0"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(PACK_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        route = self.path.split("?", 1)[0]
        if route == "/api/revisit":
            self._send_json(get_revisit_list(), status=200)
            return
        if route in ("/api/review", "/api/approve"):
            self._send_json(get_reviewed_list(), status=200)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        route = self.path.split("?", 1)[0]
        if route == "/api/edit":
            self._handle_edit()
            return
        if route == "/api/delete":
            self._handle_delete()
            return
        if route in ("/api/revisit", "/api/flag"):
            self._handle_revisit()
            return
        if route in ("/api/review", "/api/approve"):
            self._handle_review()
            return
        if route == "/api/shutdown":
            self._handle_shutdown()
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def _handle_review(self) -> None:
        try:
            payload = self._read_json_body()
            result = set_reviewed(payload)
        except (DeleteError, EditError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=getattr(exc, "status", 400))
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": f"server error: {exc}"}, status=500)
            return
        self._send_json(result)

    def _handle_revisit(self) -> None:
        try:
            payload = self._read_json_body()
            result = toggle_revisit(payload)
        except (DeleteError, EditError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=getattr(exc, "status", 400))
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": f"server error: {exc}"}, status=500)
            return
        self._send_json(result)

    def _handle_shutdown(self) -> None:
        self._send_json({"ok": True, "message": "Server shutting down..."}, status=200)
        server = self.server
        shutdown_cb = getattr(server, "shutdown_callback", None)

        def _do_shutdown() -> None:
            if callable(shutdown_cb):
                try:
                    shutdown_cb()
                except Exception:
                    pass
            try:
                server.shutdown()
            except Exception:
                pass

        threading.Thread(target=_do_shutdown, name="api-shutdown", daemon=True).start()

    def do_PUT(self) -> None:  # noqa: N802 (http.server API)
        if self.path.split("?", 1)[0] != "/api/sample":
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        self._handle_edit()

    def do_DELETE(self) -> None:  # noqa: N802 (http.server API)
        if self.path.split("?", 1)[0] != "/api/sample":
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        self._handle_delete()

    def _handle_edit(self) -> None:
        try:
            payload = self._read_json_body()
            result = edit_sample(payload)
        except EditError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
            return
        except Exception as exc:  # keep the API contract even on surprises
            self.log_error("edit failed: %s", exc)
            self._send_json({"ok": False, "error": f"edit failed: {exc}"}, status=500)
            return
        self._send_json(result, status=200)

    def _handle_delete(self) -> None:
        try:
            payload = self._read_json_body()
            result = delete_sample(payload.get("file"))
        except DeleteError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
            return
        except Exception as exc:  # keep the API contract even on surprises
            self.log_error("delete failed: %s", exc)
            self._send_json({"ok": False, "error": f"delete failed: {exc}"}, status=500)
            return
        self._send_json(result, status=200)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise DeleteError("invalid Content-Length") from None
        if length <= 0:
            raise DeleteError("JSON body is required")
        if length > MAX_BODY_BYTES:
            raise DeleteError("JSON body is too large", status=413)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeleteError(f"invalid JSON body: {exc}") from None
        if not isinstance(body, dict):
            raise DeleteError("JSON body must be an object")
        return body

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = (json.dumps(data) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SamplepackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    port = 12012
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"invalid port: {sys.argv[1]}", file=sys.stderr)
            return 2

    with SamplepackServer(("127.0.0.1", port), SamplepackHandler) as httpd:
        print(f"Serving samplepack from {PACK_DIR} at http://127.0.0.1:{port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
