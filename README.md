# Samplepack Browser

A lightweight, standalone audio sample browser, audition player, and metadata manager with native system tray integration for Linux Mint (Cinnamon / X11).

![Samplepack Browser Icon](icon.png)

## Overview

Samplepack Browser provides an interface for electronic music producers and sound designers to quickly search, audition, curate, and organize production sample libraries (WAV PCM 24-bit 48 kHz).

### Key Features

- **100% Vanilla Web UI**: Zero external JavaScript or CSS dependencies. High-performance waveform visualization, keyboard-driven navigation (`Space`, `↑`/`↓`, `C`, `F`, `L`), filter rails, tag search, and responsive grid/table views.
- **Audition Player**: HTML5 Web Audio transport with click-to-seek waveform canvas, peak/RMS/crest/centroid telemetry, volume slider, and loop playback.
- **In-Browser Sample Management**:
  - **Inline Metadata Editing**: Rename samples, assign categories, roles, suggested uses, tags, transcripts, and descriptions. Changing a category physically moves the audio file on disk and updates indexes atomically.
  - **Sample Deletion**: Red `✕` action button with confirmation dialog and atomic deletion from disk and metadata catalogs.
  - **👍 Thumbs Up & Approved Marking**: Mark samples as reviewed and accepted directly next to the waveform or on each card/row, displaying a green checkmark (`✓`) and badge.
  - **👎 Thumbs Down & Action Menu**: Quick triage dialog prompting whether to permanently delete the sample, flag it for revisit with notes, edit its metadata, or do nothing.
  - **🚩 Revisit & Review System**: Flag samples that need remediation (e.g. truncated tails, miscategorizations, level issues) with preset tags or custom notes. Filter exclusively by flagged or approved items, and export/copy the review list.
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

By default, the browser launches in GUI mode: it initializes the background HTTP server on `http://127.0.0.1:12012`, starts the Cinnamon panel tray icon, and opens the default web browser.

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

## Architecture

- `server.py` — Multi-threaded HTTP server handling static assets, atomic mutations (`/api/edit`, `/api/delete`, `/api/revisit`, `/api/review`), and process termination (`/api/shutdown`).
- `tray.py` — Tray application providing Linux Mint status icon, desktop notifications (`notify-send`), and process lifecycle control.
- `index.html` — Vanilla single-page application and player interface.
- `launch.sh` — Entrypoint shell script handling virtual environment setup and launch flags.
- `Samplepack Browser.desktop` — FreeDesktop desktop launcher.

## License

MIT
