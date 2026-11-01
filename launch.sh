#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-12012}"

# Ensure uv-managed venv in samplepack/.venv is available
VENV_DIR="${DIR}/.venv"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "Initializing virtual environment in ${VENV_DIR}..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python /usr/bin/python3 --system-site-packages "${VENV_DIR}"
    uv pip install --python "${VENV_DIR}/bin/python" pystray pillow
  elif [[ -x "/home/dolf/.local/bin/uv" ]]; then
    /home/dolf/.local/bin/uv venv --python /usr/bin/python3 --system-site-packages "${VENV_DIR}"
    /home/dolf/.local/bin/uv pip install --python "${VENV_DIR}/bin/python" pystray pillow
  fi
fi

PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

# If --no-tray or --cli or no graphical display is present, run in terminal/headless mode
if [[ "${*:-}" == *"--no-tray"* ]] || [[ "${*:-}" == *"--cli"* ]] || [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  echo "Samplepack Browser running (CLI mode):"
  echo "  http://127.0.0.1:${PORT}/"
  echo "  http://127.0.0.1:${PORT}/#category=inspirational"
  echo ""
  echo "Press Ctrl+C to stop."
  exec "${PYTHON}" "${DIR}/server.py" "${PORT}"
fi

# Otherwise, launch with native Cinnamon GTK tray icon & auto-open browser
exec "${PYTHON}" "${DIR}/tray.py" "${PORT}"
