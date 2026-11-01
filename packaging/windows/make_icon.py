#!/usr/bin/env python3
"""Generate the Windows application icon from the repository icon.png.

The repository ships a single 128x128 RGBA PNG. Windows installers, shortcuts
and the executable itself need a multi-resolution .ico, so this script renders
one containing 16 through 256 pixel entries.

Usage:
    python packaging/windows/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - environment guard
    raise SystemExit("Pillow is required: pip install pillow")

# Sizes written into the .ico, smallest first. 256 is the largest size Windows
# stores as a PNG-compressed entry and the one Explorer uses for large tiles.
ICON_SIZES = [
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "icon.png"
TARGET = Path(__file__).resolve().parent / "icon.ico"


def build_icon(source: Path = SOURCE, target: Path = TARGET) -> Path:
    """Render ``source`` into a multi-resolution ``target`` .ico file."""
    if not source.is_file():
        raise SystemExit(f"missing source image: {source}")

    with Image.open(source) as image:
        icon = image.convert("RGBA")

    # Upscale the master to 256x256 with LANCZOS first so every smaller entry
    # is downsampled from one clean, high-quality base instead of chaining
    # resizes from the 128x128 original.
    largest = ICON_SIZES[-1]
    if icon.size != largest:
        icon = icon.resize(largest, Image.LANCZOS)

    target.parent.mkdir(parents=True, exist_ok=True)
    icon.save(target, format="ICO", sizes=ICON_SIZES)

    # Read the file back so the caller sees what actually landed on disk.
    with Image.open(target) as written:
        stored = sorted(written.ico.sizes())

    expected = sorted(ICON_SIZES)
    if stored != expected:
        raise SystemExit(f"icon sizes mismatch: wrote {stored}, expected {expected}")

    print(f"wrote {target} ({target.stat().st_size} bytes)")
    print("sizes: " + ", ".join(f"{w}x{h}" for w, h in stored))
    return target


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else SOURCE
    target = Path(argv[2]) if len(argv) > 2 else TARGET
    build_icon(source, target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
