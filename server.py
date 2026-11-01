#!/usr/bin/env python3
"""Local sample browser API backed by SQLite and file-based WAV assets."""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from email import policy
from email.parser import BytesParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

import catalog_db
import mcp_protocol

PACK_DIR = catalog_db.PACK_DIR
DATA_DIR = catalog_db.DATA_DIR
SAMPLES_DIR = catalog_db.SAMPLES_DIR
PROVENANCE_PATH = DATA_DIR / "source" / "archival" / "provenance.json"
DEFAULT_PORT = 12012
DESKTOP_ENTRY_NAME = "samplepack-browser.desktop"
WINDOWS_URL_SHORTCUT_NAME = "Samplepack Browser.url"
WINDOWS_LNK_SHORTCUT_NAME = "Samplepack Browser.lnk"
MAX_BODY_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MUTATION_LOCK = threading.RLock()
SAFE_CATEGORY_RE = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)
TAG_SPLIT_RE = re.compile(r"[,;\n]+")
USLT_CHUNK_IDS = (b"id3 ", b"ID3 ")
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/icon.png": "icon.png"}
JSON_MUTATION_ROUTES = frozenset({
    "/api/edit", "/api/sample", "/api/delete", "/api/revisit", "/api/flag",
    "/api/review", "/api/approve", "/api/favorite", "/api/favorites/import",
    "/api/import-dir", "/api/desktop-shortcut",
})


class APIError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def clean_text(raw: object, field: str, required: bool = False) -> str:
    if raw is None:
        if required:
            raise APIError(f"'{field}' is required")
        return ""
    if not isinstance(raw, str):
        raise APIError(f"'{field}' must be a string")
    value = raw.strip()
    if required and not value:
        raise APIError(f"'{field}' is required")
    return value


def clean_tags(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = TAG_SPLIT_RE.split(raw)
    elif isinstance(raw, list):
        parts = []
        for value in raw:
            if not isinstance(value, str):
                raise APIError("'tags' must contain strings")
            parts.extend(TAG_SPLIT_RE.split(value))
    else:
        raise APIError("'tags' must be a list or a comma-separated string")
    seen: set[str] = set()
    result = []
    for part in parts:
        tag = part.strip().lstrip("#").strip()
        if tag and tag.casefold() not in seen:
            result.append(tag)
            seen.add(tag.casefold())
    return result


def slugify(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")


def validate_category(raw: object) -> str:
    category = clean_text(raw, "category", required=True).lower()
    if not SAFE_CATEGORY_RE.fullmatch(category) or category.casefold() in {"source", "work", ".git", "samples"}:
        raise APIError("category must be a simple folder name using letters, digits, '-' or '_'")
    return category


def desktop_directory() -> Path:
    """Return the user's desktop directory for the current platform."""
    if sys.platform == "win32":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile).expanduser() / "Desktop"
        try:
            return Path.home() / "Desktop"
        except RuntimeError:
            return Path(os.environ.get("HOME") or ".") / "Desktop"
    configured = os.environ.get("XDG_DESKTOP_DIR")
    if configured:
        return Path(configured).expanduser()
    try:
        return Path.home() / "Desktop"
    except RuntimeError:
        return Path(os.environ.get("HOME") or "/tmp") / "Desktop"


def _powershell_quote(value: str) -> str:
    """Quote one PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _desktop_quote(token: str) -> str:
    escaped = (str(token).replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$"))
    return f'"{escaped}"'


def shortcut_command() -> list[str]:
    """Choose the installed launcher, a checkout launcher, or the tray script."""
    installed = shutil.which(catalog_db.APP_NAME)
    if installed:
        return [installed]
    launcher = PACK_DIR / "launch.sh"
    if launcher.is_file():
        return [str(launcher)]
    tray_script = PACK_DIR / "tray.py"
    if tray_script.is_file():
        return [sys.executable, str(tray_script)]
    return [sys.executable, str(PACK_DIR / "server.py")]


def windows_launcher_target() -> tuple[Path, str] | None:
    """Return an installed Windows launcher and its arguments, when present."""
    for name in (f"{catalog_db.APP_NAME}.exe", f"{catalog_db.APP_NAME}.cmd",
                 f"{catalog_db.APP_NAME}.bat"):
        installed = shutil.which(name)
        if installed:
            return Path(installed), ""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    tray_script = PACK_DIR / "tray.py"
    if pythonw.is_file() and tray_script.is_file():
        return pythonw, f'"{tray_script}"'
    if tray_script.is_file():
        return Path(sys.executable), f'"{tray_script}"'
    if (PACK_DIR / "server.py").is_file():
        return Path(sys.executable), '"{}"'.format(PACK_DIR / "server.py")
    return None


def _write_windows_shortcut_file(desktop: Path, url: str) -> Path:
    """Write a plain-text InternetShortcut (``.url``) that opens the web UI."""
    icon = PACK_DIR / "icon.png"
    lines = ["[InternetShortcut]", f"URL={url}"]
    if icon.is_file():
        lines.extend([f"IconFile={icon}", "IconIndex=0"])
    target = desktop / WINDOWS_URL_SHORTCUT_NAME
    target.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return target


def _write_windows_lnk(desktop: Path, target: Path, arguments: str) -> Path | None:
    """Create a ``.lnk`` shortcut through WScript.Shell when PowerShell exists."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return None
    shortcut = desktop / WINDOWS_LNK_SHORTCUT_NAME
    icon = PACK_DIR / "icon.png"
    description = "Browse, audition, and manage local WAV sample libraries"
    script = ";".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$shortcut = $shell.CreateShortcut({_powershell_quote(str(shortcut))})",
        f"$shortcut.TargetPath = {_powershell_quote(str(target))}",
        f"$shortcut.Arguments = {_powershell_quote(arguments)}",
        f"$shortcut.WorkingDirectory = {_powershell_quote(str(PACK_DIR))}",
        f"$shortcut.Description = {_powershell_quote(description)}",
    ])
    if icon.is_file():
        script += f";$shortcut.IconLocation = {_powershell_quote(str(icon))}"
    script += ";$shortcut.Save()"
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return None
    if completed.returncode != 0 or not shortcut.is_file():
        return None
    return shortcut


def create_windows_shortcut(desktop: Path, url: str) -> dict[str, Any]:
    """Create a Windows shortcut that opens the browser UI."""
    launcher = windows_launcher_target()
    if launcher is not None:
        target, arguments = launcher
        shortcut = _write_windows_lnk(desktop, target, arguments)
        if shortcut is not None:
            return {
                "ok": True,
                "path": str(shortcut),
                "kind": "lnk",
                "target": str(target),
                "arguments": arguments,
                "url": url,
                "icon": str(PACK_DIR / "icon.png"),
            }
    shortcut = _write_windows_shortcut_file(desktop, url)
    return {
        "ok": True,
        "path": str(shortcut),
        "kind": "url",
        "url": url,
        "icon": str(PACK_DIR / "icon.png"),
    }


def create_desktop_shortcut(url: str | None = None) -> dict[str, Any]:
    """Write a desktop shortcut for the running browser."""
    desktop = desktop_directory()
    if not desktop.is_dir():
        raise APIError(f"Desktop folder not found: {desktop}", status=404)
    target_url = url or f"http://127.0.0.1:{DEFAULT_PORT}/"
    if sys.platform == "win32":
        return create_windows_shortcut(desktop, target_url)
    command = shortcut_command()
    exec_line = " ".join(_desktop_quote(token) for token in command)
    icon = PACK_DIR / "icon.png"
    icon_value = str(icon) if icon.is_file() else "audio-x-generic"
    content = "\n".join([
        "[Desktop Entry]",
        "Version=1.0",
        "Type=Application",
        "Name=Samplepack Browser",
        "GenericName=Sample Library Browser",
        "Comment=Browse, audition, and manage local WAV sample libraries",
        f"Exec={exec_line}",
        f"Icon={icon_value}",
        "Terminal=false",
        "Categories=AudioVideo;Audio;",
        "Keywords=sample;audio;wav;music;",
        "StartupNotify=true",
        "",
    ])
    target = desktop / DESKTOP_ENTRY_NAME
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(target), "exec": exec_line, "icon": icon_value}


def sample_path(file_name: object, *, must_exist: bool = True) -> tuple[str, Path]:
    relative = catalog_db.normalize_file(file_name)
    target = (SAMPLES_DIR / Path(*PurePosixPath(relative).parts)).resolve()
    try:
        target.relative_to(SAMPLES_DIR.resolve())
    except ValueError:
        raise APIError("sample path escapes samples/") from None
    if must_exist and not target.is_file():
        raise APIError(f"sample file does not exist: {relative}", status=404)
    return relative, target


def decode_request_path(route: str) -> str:
    """Decode one URL path and reject encoded control, backslash, or traversal tricks."""
    try:
        decoded = unquote_to_bytes(route).decode("utf-8")
    except UnicodeDecodeError:
        raise APIError("request path is not valid UTF-8", status=400) from None
    if not decoded.startswith("/") or "\x00" in decoded or "\\" in decoded:
        raise APIError("invalid request path", status=400)
    return decoded


def static_file(route: str) -> Path | None:
    """Resolve an allowed static file: the browser shell, its icon, or a WAV under samples/."""
    decoded = decode_request_path(route)
    if decoded in STATIC_FILES:
        return PACK_DIR / STATIC_FILES[decoded]
    if not decoded.startswith("/samples/"):
        return None
    parts = PurePosixPath(decoded).parts
    if any(part in ("", ".", "..") for part in parts[1:]):
        return None
    relative_parts = parts[2:]
    if len(relative_parts) < 2 or PurePosixPath(*relative_parts).suffix.lower() != ".wav":
        return None
    target = (SAMPLES_DIR / Path(*relative_parts)).resolve()
    try:
        target.relative_to(SAMPLES_DIR.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_wav_chunks(path: Path) -> tuple[list[tuple[bytes, bytes]], bytes]:
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise APIError("file is not a RIFF/WAVE audio file")
    chunks: list[tuple[bytes, bytes]] = []
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        start = pos + 8
        end = start + size
        if end > len(data):
            raise APIError("file is a truncated or corrupt WAV")
        chunks.append((chunk_id, data[start:end]))
        pos = end + (size & 1)
    return chunks, data[pos:]


def _pack_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    return chunk_id + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")


def build_uslt_tag(transcript: str) -> bytes:
    body = b"\x01eng\xff\xfe\x00\x00" + transcript.encode("utf-16")
    frame = b"USLT" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    tag_size = len(frame)
    syncsafe = bytes(((tag_size >> 21) & 0x7f, (tag_size >> 14) & 0x7f,
                      (tag_size >> 7) & 0x7f, tag_size & 0x7f))
    return b"ID3\x03\x00\x00" + syncsafe + frame


def write_wav_uslt(path: Path, transcript: str) -> None:
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
    atomic_write_bytes(path, b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)


def analyze_uploaded_wav(data: bytes) -> dict[str, Any]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            width = wav.getsampwidth()
            frames = wav.getnframes()
            if wav.getcomptype() != "NONE":
                raise APIError("uploaded WAV must use uncompressed PCM")
            if channels != 2 or sample_rate != 48000 or width != 3:
                raise APIError("uploaded WAV must be stereo, 48000 Hz, 24-bit PCM")
            if frames <= 0:
                raise APIError("uploaded WAV contains no audio frames")
            peak = 0
            square_sum = 0
            sample_count = 0
            while True:
                block = wav.readframes(16384)
                if not block:
                    break
                for offset in range(0, len(block) - 2, 3):
                    value = int.from_bytes(block[offset:offset + 3], "little", signed=False)
                    if value & 0x800000:
                        value -= 0x1000000
                    magnitude = abs(value)
                    peak = max(peak, magnitude)
                    square_sum += value * value
                    sample_count += 1
    except APIError:
        raise
    except (wave.Error, EOFError) as exc:
        raise APIError(f"could not read uploaded WAV: {exc}") from None
    peak_linear = peak / 8388608
    rms_linear = (square_sum / sample_count) ** 0.5 / 8388608 if sample_count else 0
    return {
        "duration": frames / sample_rate,
        "sr": sample_rate,
        "channels": channels,
        "bit_depth": width * 8,
        "peak": round(peak_linear, 6),
        "rms": round(rms_linear, 6),
        "crest": round(peak_linear / rms_linear, 2) if rms_linear else None,
        "centroid_hz": None,
        "pitch": None,
    }


def _write_provenance_change(old_file: str, new_file: str | None = None,
                             *, transcript: str | None = None,
                             suggested_use: str | None = None) -> None:
    if not PROVENANCE_PATH.exists():
        return
    try:
        document = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    changed = False
    for section in ("extracts", "full_sources"):
        entries = document.get(section)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            old_pack_path = f"samples/{old_file}"
            refs = {entry.get("file"), entry.get("pack_file")}
            if old_file not in refs and old_pack_path not in refs:
                kept.append(entry)
                continue
            if new_file is None:
                changed = True
                continue
            pack_path = f"samples/{new_file}"
            if entry.get("file") in {old_file, old_pack_path}:
                entry["file"] = pack_path
            if entry.get("pack_file") in {old_file, old_pack_path}:
                entry["pack_file"] = pack_path
            if transcript is not None:
                if transcript:
                    entry["transcript"] = transcript
                else:
                    entry.pop("transcript", None)
            if suggested_use is not None:
                if suggested_use:
                    entry["suggested_use"] = suggested_use
                else:
                    entry.pop("suggested_use", None)
            kept.append(entry)
            changed = True
        document[section] = kept
    if changed:
        atomic_write_bytes(PROVENANCE_PATH, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())


def edit_sample(payload: dict[str, Any]) -> dict[str, Any]:
    old_file, source = sample_path(payload.get("file"))
    category = validate_category(payload.get("category"))
    name = clean_text(payload.get("name"), "name", required=True)
    role = clean_text(payload.get("role"), "role", required=True)
    tags = clean_tags(payload.get("tags"))
    catalog = catalog_db.load_catalog()
    entry = next((sample for sample in catalog["samples"] if sample["file"] == old_file), None)
    if entry is None:
        raise APIError(f"sample not found in catalog: {old_file}", status=404)

    suggested_use = clean_text(payload["suggested_use"], "suggested_use") if "suggested_use" in payload else str(entry.get("suggested_use") or "")
    transcript = clean_text(payload["transcript"], "transcript") if "transcript" in payload else str(entry.get("transcript") or "")
    description = clean_text(payload["description"], "description") if "description" in payload else entry.get("description")
    new_file = f"{category}/{PurePosixPath(old_file).name}"
    _, destination = sample_path(new_file, must_exist=False)
    moved = new_file != old_file
    if moved and destination.exists():
        raise APIError(f"destination already exists: {new_file}", status=409)

    with MUTATION_LOCK:
        if "transcript" in payload:
            write_wav_uslt(source, transcript)
        if moved:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)

        entry.update({
            "file": new_file, "name": name, "category": category, "role": role,
            "tags": tags, "suggested_use": suggested_use, "transcript": transcript,
            "description": description,
        })
        uses = [value for value in (entry.get("use") or []) if isinstance(value, str)]
        for candidate in (role, slugify(suggested_use)):
            if candidate and candidate not in uses:
                uses.append(candidate)
        entry["use"] = uses
        for sample in catalog["samples"]:
            if sample.get("parent") == old_file:
                sample["parent"] = new_file
        try:
            catalog_db.save_catalog(catalog)
        except BaseException:
            if moved and destination.exists():
                destination.rename(source)
            raise
        _write_provenance_change(
            old_file, new_file,
            transcript=transcript if "transcript" in payload else None,
            suggested_use=suggested_use if "suggested_use" in payload else None,
        )
    updated = catalog_db.get_sample(new_file)
    assert updated is not None
    return updated


def delete_sample(file_name: object) -> dict[str, Any]:
    relative, target = sample_path(file_name, must_exist=False)
    with MUTATION_LOCK:
        tombstone = target.with_name(f".{target.name}.delete-pending")
        if tombstone.exists():
            raise APIError(f"delete recovery file already exists: {tombstone.name}", status=409)
        moved = target.exists()
        if moved:
            target.rename(tombstone)
        try:
            if not catalog_db.delete_sample(relative):
                raise APIError(f"sample not found in catalog: {relative}", status=404)
        except BaseException:
            if moved and tombstone.exists():
                tombstone.rename(target)
            raise
        tombstone.unlink(missing_ok=True)
        _write_provenance_change(relative)
    return {"ok": True, "deleted": relative, "count": catalog_db.load_catalog()["count"]}


def _decode_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], tuple[str, bytes]]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", errors="strict")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise APIError("upload must use multipart/form-data")
    fields: dict[str, str] = {}
    upload: tuple[str, bytes] | None = None
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if field_name == "audio":
                upload = (filename, payload)
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                fields[field_name] = payload.decode(charset)
            except (LookupError, UnicodeDecodeError):
                raise APIError(f"invalid text field encoding: {field_name}") from None
    if upload is None:
        raise APIError("choose a WAV file to upload")
    return fields, upload


def add_sample(fields: dict[str, str], upload: tuple[str, bytes]) -> dict[str, Any]:
    category = validate_category(fields.get("category"))
    name = clean_text(fields.get("name"), "name", required=True)
    role = clean_text(fields.get("role"), "role", required=True)
    original_name, audio_bytes = upload
    if not original_name.casefold().endswith(".wav"):
        raise APIError("select a .wav audio file")
    stats = analyze_uploaded_wav(audio_bytes)
    stem = slugify(PurePosixPath(original_name.replace("\\", "/")).stem) or slugify(name) or "sample"
    file_name = catalog_db.normalize_file(f"{category}/{stem}.wav")
    _, destination = sample_path(file_name, must_exist=False)
    tags = clean_tags(fields.get("tags"))
    suggested_use = clean_text(fields.get("suggested_use"), "suggested_use")
    transcript = clean_text(fields.get("transcript"), "transcript")
    description = clean_text(fields.get("description"), "description")
    license_text = clean_text(fields.get("license"), "license") or "Unknown — verify rights before distribution"
    uses = [role]
    if suggested_use:
        uses.extend(slugify(item) for item in TAG_SPLIT_RE.split(suggested_use) if slugify(item))
    sample = {
        "file": file_name,
        "name": name,
        "category": category,
        "tags": tags,
        **stats,
        "dry_processed": "processed",
        "parent": None,
        "processing": ["local library import"],
        "provenance": {"type": "local_import", "original_filename": PurePosixPath(original_name.replace("\\", "/")).name,
                       "license": license_text},
        "license": license_text,
        "description": description,
        "source_window": None,
        "role": role,
        "use": sorted(set(uses)),
        "suggested_use": suggested_use,
        "transcript": transcript or None,
        "reviewed": False,
        "reviewed_date": None,
        "revisit": True,
        "revisit_reason": "New import: verify source, license, and audio quality",
        "revisit_date": catalog_db.now_string(),
    }
    with MUTATION_LOCK:
        if destination.exists() or catalog_db.get_sample(file_name) is not None:
            raise APIError(f"a sample already exists at {file_name}", status=409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".wav", dir=destination.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            atomic_write_bytes(temporary, audio_bytes)
            if transcript:
                write_wav_uslt(temporary, transcript)
            os.replace(temporary, destination)
            try:
                catalog_db.add_sample(sample)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        finally:
            temporary.unlink(missing_ok=True)
    return catalog_db.get_sample(file_name) or sample


def set_reviewed(payload: dict[str, Any]) -> dict[str, Any]:
    relative = catalog_db.normalize_file(payload.get("file"))
    reviewed = bool(payload.get("reviewed", True))
    try:
        sample = catalog_db.set_state(relative, reviewed=reviewed)
    except KeyError:
        raise APIError(f"sample not found in catalog: {relative}", status=404) from None
    cat = catalog_db.load_catalog()
    return {
        "ok": True, "file": relative, "reviewed": reviewed,
        "reviewed_date": sample.get("reviewed_date"),
        "reviewed_count": sum(bool(s.get("reviewed")) for s in cat["samples"]),
        "revisit_count": sum(bool(s.get("revisit")) for s in cat["samples"]),
        "sample": sample,
    }


def set_revisit(payload: dict[str, Any]) -> dict[str, Any]:
    relative = catalog_db.normalize_file(payload.get("file"))
    flagged = bool(payload.get("flagged", True))
    reason = clean_text(payload.get("reason"), "reason")
    try:
        sample = catalog_db.set_state(relative, flagged=flagged, reason=reason)
    except KeyError:
        raise APIError(f"sample not found in catalog: {relative}", status=404) from None
    flagged_samples = [s for s in catalog_db.load_catalog()["samples"] if s.get("revisit")]
    return {
        "ok": True, "file": relative, "flagged": flagged,
        "reason": sample.get("revisit_reason"), "count": len(flagged_samples),
        "samples": [{"file": s["file"], "reason": s.get("revisit_reason"), "date": s.get("revisit_date")} for s in flagged_samples],
        "reviewed": sample.get("reviewed"),
    }


def _read_body(handler: SimpleHTTPRequestHandler, limit: int) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise APIError("invalid Content-Length") from None
    if length <= 0:
        raise APIError("request body is required")
    if length > limit:
        raise APIError("request body is too large", status=413)
    return handler.rfile.read(length)


class SamplepackHandler(SimpleHTTPRequestHandler):
    server_version = "SamplepackHTTP/2.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(PACK_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route in ("/api/catalog", "/catalog.json"):
            self._send_json(catalog_db.load_catalog())
        elif route == "/api/revisit":
            samples = [s for s in catalog_db.load_catalog()["samples"] if s.get("revisit")]
            self._send_json({"ok": True, "count": len(samples), "samples": [
                {"file": s["file"], "reason": s.get("revisit_reason"), "date": s.get("revisit_date")} for s in samples
            ]})
        elif route in ("/api/review", "/api/approve"):
            samples = [s for s in catalog_db.load_catalog()["samples"] if s.get("reviewed")]
            self._send_json({"ok": True, "count": len(samples), "total": catalog_db.load_catalog()["count"], "samples": [
                {"file": s["file"], "name": s.get("name"), "reviewed_date": s.get("reviewed_date")} for s in samples
            ]})
        elif route == "/api/favorites":
            self._send_json({"ok": True, "files": catalog_db.list_favorites()})
        elif route == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif route.startswith("/api/"):
            self._send_json({"ok": False, "error": "not found"}, status=404)
        elif route.startswith("/catalog.sqlite3"):
            self.send_error(404)
        else:
            self._serve_static(route)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve_static(urlsplit(self.path).path, head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        try:
            self._guard_mutation(route)
        except APIError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
            return
        if route == "/mcp":
            self._handle_mcp()
            return
        if route == "/api/shutdown":
            self._handle_shutdown()
            return
        if route == "/api/add":
            self._handle_add()
            return
        try:
            payload = self._read_json()
            if route in ("/api/edit", "/api/sample"):
                result = {"ok": True, "sample": edit_sample(payload), "count": catalog_db.load_catalog()["count"]}
            elif route == "/api/delete":
                result = delete_sample(payload.get("file"))
            elif route in ("/api/revisit", "/api/flag"):
                result = set_revisit(payload)
            elif route in ("/api/review", "/api/approve"):
                result = set_reviewed(payload)
            elif route == "/api/favorite":
                relative = catalog_db.normalize_file(payload.get("file"))
                try:
                    sample = catalog_db.set_state(relative, favorite=bool(payload.get("favorite", True)))
                except KeyError:
                    raise APIError(f"sample not found in catalog: {relative}", status=404) from None
                result = {"ok": True, "file": relative, "favorite": sample["favorite"]}
            elif route == "/api/favorites/import":
                files = payload.get("files", [])
                if not isinstance(files, list):
                    raise APIError("'files' must be a list")
                result = {"ok": True, "files": catalog_db.set_favorites(files, merge=True)}
            elif route == "/api/import-dir":
                result = self._handle_import_directory(payload)
            elif route == "/api/desktop-shortcut":
                result = create_desktop_shortcut(
                    f"http://127.0.0.1:{self.server.server_address[1]}/"
                )
            else:
                self._send_json({"ok": False, "error": "not found"}, status=404)
                return
            self._send_json(result)
        except APIError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except (ValueError, KeyError, FileExistsError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self.log_error("request failed: %s", exc)
            self._send_json({"ok": False, "error": f"server error: {exc}"}, status=500)

    def do_PUT(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/sample":
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route != "/api/sample":
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        try:
            self._guard_mutation(route)
            self._send_json(delete_sample(self._read_json().get("file")))
        except APIError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _guard_mutation(self, route: str) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and not self._same_origin(origin):
            raise APIError("cross-origin requests are not allowed", status=403)
        if route in JSON_MUTATION_ROUTES:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise APIError("Content-Type must be application/json", status=415)

    def _same_origin(self, origin: str) -> bool:
        parts = urlsplit(origin.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        host = self.headers.get("Host", "").strip()
        return bool(host) and parts.netloc.casefold() == host.casefold()

    def _handle_shutdown(self) -> None:
        self._send_json({"ok": True, "message": "Server shutting down..."})
        callback = getattr(self.server, "shutdown_callback", None)
        shutdown = callback if callable(callback) else self.server.shutdown
        threading.Thread(target=shutdown, daemon=True).start()

    def _serve_static(self, route: str, *, head_only: bool = False) -> None:
        try:
            target = static_file(route)
        except APIError as exc:
            self.send_error(exc.status, str(exc))
            return
        if target is None:
            self.send_error(404)
            return
        try:
            size = target.stat().st_size
        except OSError:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        try:
            with target.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)
        except OSError:
            pass

    def _handle_import_directory(self, payload: dict[str, Any]) -> dict[str, Any]:
        directory = clean_text(payload.get("directory"), "directory", required=True)
        raw_category = payload.get("category")
        category = validate_category(raw_category) if raw_category not in (None, "") else None
        with MUTATION_LOCK:
            result = catalog_db.import_directory(directory, category=category)
        return {"ok": True, **result, "total": catalog_db.load_catalog()["count"]}

    def _handle_add(self) -> None:
        try:
            body = _read_body(self, MAX_UPLOAD_BYTES)
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data;"):
                raise APIError("upload must use multipart/form-data")
            fields, upload = _decode_multipart(content_type, body)
            sample = add_sample(fields, upload)
            self._send_json({"ok": True, "sample": sample, "count": catalog_db.load_catalog()["count"]}, status=201)
        except APIError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except Exception as exc:
            self.log_error("sample upload failed: %s", exc)
            self._send_json({"ok": False, "error": f"upload failed: {exc}"}, status=500)

    def _handle_mcp(self) -> None:
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept and "text/event-stream" not in accept:
            self.send_error(406, "Accept must include application/json or text/event-stream")
            return
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in mcp_protocol.SUPPORTED_PROTOCOLS:
            self.send_error(400, "unsupported MCP protocol version")
            return
        try:
            raw = _read_body(self, MAX_BODY_BYTES)
            message = json.loads(raw.decode("utf-8"))
            response = mcp_protocol.handle_message(message)
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(response, content_type="application/json; charset=utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"Parse error: {exc}"}}, status=400)
        except APIError as exc:
            self._send_json({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": str(exc)}}, status=exc.status)
        except Exception as exc:
            self.log_error("MCP request failed: %s", exc)
            self._send_json({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32603, "message": "Internal MCP error"}}, status=500)

    def _read_json(self) -> dict[str, Any]:
        try:
            raw = _read_body(self, MAX_BODY_BYTES)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(f"invalid JSON body: {exc}") from None
        if not isinstance(value, dict):
            raise APIError("JSON body must be an object")
        return value

    def _send_json(self, value: Any, status: int = 200,
                   content_type: str = "application/json; charset=utf-8") -> None:
        body = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class SamplepackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Samplepack Browser HTTP server")
    parser.add_argument("port", nargs="?", type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--port", dest="port_option", type=int, help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-tray", "--cli", dest="cli", action="store_true",
                        help="Run without the desktop tray (the server never starts one)")
    args = parser.parse_args(argv)
    port = args.port_option if args.port_option is not None else (args.port if args.port is not None else DEFAULT_PORT)
    if not 1 <= port <= 65535:
        print(f"invalid port: {port}", file=sys.stderr)
        return 2
    catalog_db.migrate_legacy_files()
    with SamplepackServer(("127.0.0.1", port), SamplepackHandler) as httpd:
        print(f"Serving samplepack from {PACK_DIR} at http://127.0.0.1:{port}/")
        print(f"Library data: {DATA_DIR}")
        print(f"MCP endpoint: http://127.0.0.1:{port}/mcp")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
