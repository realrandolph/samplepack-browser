#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=12012
MODE="tray"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--port PORT] [--no-tray|--cli] [PORT]

  --port PORT    HTTP port (default: 12012)
  --no-tray      Run the HTTP server in the foreground without the tray
  --cli          Alias for --no-tray
  -h, --help     Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-tray|--cli)
      MODE="cli"
      shift
      ;;
    --port)
      if [[ $# -lt 2 ]]; then
        echo "error: --port requires a value" >&2
        exit 2
      fi
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    [0-9]*)
      PORT="$1"
      shift
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "error: invalid port: ${PORT}" >&2
  exit 2
fi

# No graphical session means there is no tray to start.
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  MODE="cli"
fi

# A source checkout manages its own virtual environment. A packaged install
# uses the system Python and its dependency packages.
VENV_DIR="${SAMPLEPACK_VENV:-${DIR}/.venv}"
PYTHON="python3"

if [[ -x "${VENV_DIR}/bin/python" ]]; then
  PYTHON="${VENV_DIR}/bin/python"
elif [[ -e "${DIR}/.git" ]] && [[ -w "${DIR}" ]] && command -v uv >/dev/null 2>&1; then
  echo "Initializing virtual environment in ${VENV_DIR}..."
  uv venv --python python3 --system-site-packages "${VENV_DIR}"
  uv pip install --python "${VENV_DIR}/bin/python" pystray pillow
  PYTHON="${VENV_DIR}/bin/python"
fi

if [[ "${MODE}" == "cli" ]]; then
  echo "Samplepack Browser running (CLI mode):"
  echo "  http://127.0.0.1:${PORT}/"
  echo ""
  echo "Press Ctrl+C to stop."
  exec "${PYTHON}" "${DIR}/server.py" --port "${PORT}"
fi

# Otherwise, launch with the native Cinnamon GTK tray icon and auto-open browser.
exec "${PYTHON}" "${DIR}/tray.py" --port "${PORT}"
