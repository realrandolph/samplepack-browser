# Samplepack Browser

A lightweight, standalone audio sample browser, audition player, and metadata manager with native system tray integration for Linux Mint (Cinnamon / X11). WAV files live in `samples/<category>/`; catalog metadata and browser review state live in SQLite.

![Samplepack Browser Icon](icon.png)

## Overview

Samplepack Browser provides an interface for electronic music producers and sound designers to quickly search, audition, curate, and organize production sample libraries (WAV PCM 24-bit 48 kHz).

### Key Features

- **100% Vanilla Web UI**: Zero external JavaScript or CSS dependencies. High-performance waveform visualization, keyboard-driven navigation (`Space`, `↑`/`↓`, `C`, `F`, `L`), filter rails, tag search, and responsive grid/table views.
- **Audition Player**: HTML5 Web Audio transport with click-to-seek waveform canvas, peak/RMS/crest/centroid telemetry, volume slider, and loop playback.
- **In-Browser Sample Management**:
  - **Add Sample**: Upload a stereo 48 kHz, 24-bit PCM WAV from the web app, categorize it, add metadata, and flag it for source/license/audio review.
  - **Inline Metadata Editing**: Rename samples, assign categories, roles, suggested uses, tags, transcripts, and descriptions. Changing a category moves the audio file within `samples/` and updates the SQLite catalog.
  - **Sample Deletion**: Red `✕` action button with confirmation dialog; removes the WAV and catalog record.
  - **👍 Thumbs Up & Approved Marking**: Mark samples as reviewed and accepted directly next to the waveform or on each card/row, displaying a green checkmark (`✓`) and badge.
  - **👎 Thumbs Down & Action Menu**: Quick triage dialog prompting whether to permanently delete the sample, flag it for revisit with notes, edit its metadata, or do nothing.
  - **🚩 Revisit & Review System**: Favorites, approvals, and revisit flags/notes persist in SQLite. Filter exclusively by flagged or approved items, and copy the review list.
- **Native Cinnamon Desktop Tray**:
  - Implemented via `pystray` with the native GTK backend and Cinnamon popup coordinate patch.
  - Right-click menu with **Open in Browser**, **Copy URL**, **Open Samplepack Folder** (Nemo), and **Shutdown**.
  - High-contrast neon-acid waveform badge icon rendered dynamically via Pillow.
- **Process Management & Clean Shutdown**:
  - `launch.sh` ensures single-instance execution on port `12012`.
  - Shutdown button directly in the web UI header (`⏹ Shutdown Server`) and via the tray icon right-click menu, terminating background processes cleanly.
  - Automated local virtual environment management via `uv`.

## Quick Start

### Prerequisites

- Linux (tested on Linux Mint / Cinnamon)
- Python 3.10+
- `uv` (recommended for fast, self-contained virtual environment management)

### Launching

Simply double-click **`Samplepack Browser.desktop`** in Nemo, or run from the terminal:

```bash
./launch.sh
```

By default, the browser launches in GUI mode: it initializes the background HTTP server on `http://127.0.0.1:12012`, starts the Cinnamon panel tray icon, and opens the default web browser. The server initializes `catalog.sqlite3` from the previous JSON catalog on first launch, verifies the WAV references, then removes the obsolete JSON state files.

#### Headless / Terminal Mode

To run in CLI mode without the desktop tray:

```bash
./launch.sh --no-tray
# or
./launch.sh --cli
```

## Keyboard Shortcuts

| Key | Action |
|---|---|
| <kbd>Space</kbd> | Play / Pause selected sample |
| <kbd>↑</kbd> / <kbd>↓</kbd> (or <kbd>k</kbd> / <kbd>j</kbd>) | Navigate previous / next sample (with auto-play) |
| <kbd>A</kbd> | Mark as reviewed & approved (👍) |
| <kbd>D</kbd> | Needs action menu (👎: delete, flag, edit, or do nothing) |
| <kbd>F</kbd> / <kbd>R</kbd> | Flag sample for revisit / open review dialog |
| <kbd>L</kbd> | Toggle loop playback |
| <kbd>C</kbd> | Copy relative sample path to clipboard |
| <kbd>S</kbd> | Toggle sample favorite (★) |
| <kbd>/</kbd> | Focus search box |
| <kbd>Esc</kbd> | Close active modal dialog or clear search focus |

## Agent access

The web API serves the catalog at `GET /api/catalog`, supports review/favorite changes, and exposes a stateless MCP Streamable HTTP endpoint at `POST /mcp`. The stdio transport can also be launched as a local MCP server:

```json
{
  "mcp": {
    "samplepack": {
      "type": "local",
      "command": ["python3", "/home/dolf/Documents/music/samplepack/mcp_server.py"]
    }
  }
}
```

The `search_samples` tool filters catalog metadata and review state. Pass one returned file to `select_sample` to verify it and obtain the absolute WAV path. Project references use `samplepack/samples/<category>/<filename>.wav`.

## Catalog and files

- `catalog.sqlite3` is the source of truth for sample metadata, favorites, approval, and revisit state. It is local state and is ignored by Git.
- `INDEX.md` and `manifest.txt` are generated from SQLite with `python3 catalog_db.py export-index` and after catalog mutations.
- `samples/` contains the production WAV files and is ignored by Git. `source/`, `work/`, and `sapolsky samples/` remain separate staging/archive areas.
- The historical `GET /catalog.json` endpoint remains a generated JSON API view; no JSON catalog file is maintained.

## Architecture

- `catalog_db.py` — SQLite schema, one-time legacy migration, state/catalog operations, and generated indexes.
- `server.py` — Multi-threaded HTTP server for static assets, the browser API, sample uploads, and MCP Streamable HTTP.
- `mcp_server.py` / `mcp_protocol.py` — Local stdio and shared MCP tool implementation (`search_samples`, `select_sample`).
- `tray.py` — Tray application providing Linux Mint status icon, desktop notifications (`notify-send`), and process lifecycle control.
- `index.html` — Vanilla single-page application and player interface.
- `launch.sh` — Entrypoint shell script handling virtual environment setup and launch flags.
- `Samplepack Browser.desktop` — FreeDesktop desktop launcher.

## License

MIT
