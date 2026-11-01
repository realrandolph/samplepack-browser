#!/usr/bin/env bash
# Build the samplepack-browser Debian package and the release zip.
#
# Outputs:
#   dist/samplepack-browser_1.0.0_all.deb
#   dist/samplepack-browser-linux-mint.zip   (contains the .deb, mode 0755)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="samplepack-browser"
VERSION="1.0.0"
ARCH="all"
LIBDIR="/usr/lib/${PKG}"

DIST="${ROOT}/dist"
BUILD="${ROOT}/build"
STAGE="${BUILD}/${PKG}_${VERSION}_${ARCH}"
DEB="${DIST}/${PKG}_${VERSION}_${ARCH}.deb"
ZIP="${DIST}/${PKG}-linux-mint.zip"

PYTHON_MODULES=(server.py catalog_db.py tray.py mcp_server.py mcp_protocol.py)

log() { printf '[build_deb] %s\n' "$*"; }
die() { printf '[build_deb] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
for tool in dpkg-deb zip unzip; do
	command -v "$tool" >/dev/null 2>&1 || die "missing build tool: $tool"
done

for f in "${PYTHON_MODULES[@]}" index.html icon.png; do
	[ -f "${ROOT}/${f}" ] || die "missing source file: ${f}"
done
[ -d "${ROOT}/vendor/pystray" ] || die "missing vendored pystray in vendor/pystray"
[ -f "${ROOT}/packaging/samplepack-browser.desktop" ] || die "missing desktop entry"
[ -f "${ROOT}/packaging/bin/samplepack-browser" ] || die "missing launcher script"
[ -f "${ROOT}/packaging/bin/samplepack-mcp" ] || die "missing MCP launcher script"

# ------------------------------------------------------------------- staging --
log "staging into ${STAGE}"
rm -rf "$STAGE"
install -d "${STAGE}/DEBIAN"
install -d "${STAGE}/usr/bin"
install -d "${STAGE}${LIBDIR}/vendor"
install -d "${STAGE}/usr/share/applications"
install -d "${STAGE}/usr/share/icons/hicolor/256x256/apps"
install -d "${STAGE}/usr/share/doc/${PKG}"

# Python application, static UI, and icon.
for f in "${PYTHON_MODULES[@]}" index.html icon.png; do
	install -m 0644 "${ROOT}/${f}" "${STAGE}${LIBDIR}/${f}"
done

# Vendored pure-Python dependencies (pystray + its licence texts, six).
cp -R "${ROOT}/vendor/." "${STAGE}${LIBDIR}/vendor/"
find "${STAGE}${LIBDIR}/vendor" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${STAGE}${LIBDIR}/vendor" -name '*.pyc' -delete

# Launchers.
install -m 0755 "${ROOT}/packaging/bin/samplepack-browser" "${STAGE}/usr/bin/samplepack-browser"
install -m 0755 "${ROOT}/packaging/bin/samplepack-mcp" "${STAGE}/usr/bin/samplepack-mcp"

# Desktop entry and hicolor icon.
install -m 0644 "${ROOT}/packaging/samplepack-browser.desktop" \
	"${STAGE}/usr/share/applications/${PKG}.desktop"

ICON_SRC="${ROOT}/icon.png"
if python3 - "$ICON_SRC" "${STAGE}/usr/share/icons/hicolor/256x256/apps/${PKG}.png" <<'PY'
import sys

source, target = sys.argv[1], sys.argv[2]
try:
    from PIL import Image
except ImportError:
    raise SystemExit(1)

with Image.open(source) as image:
    icon = image.convert("RGBA")
    if icon.size != (256, 256):
        icon = icon.resize((256, 256), Image.LANCZOS)
    icon.save(target, "PNG", optimize=True)
PY
then
	log "installed 256x256 icon rendered from icon.png"
else
	log "Pillow unavailable; installing icon.png as provided"
	install -m 0644 "$ICON_SRC" "${STAGE}/usr/share/icons/hicolor/256x256/apps/${PKG}.png"
fi

# Metadata.
install -m 0644 "${ROOT}/packaging/debian/control" "${STAGE}/DEBIAN/control"
install -m 0755 "${ROOT}/packaging/debian/postinst" "${STAGE}/DEBIAN/postinst"
install -m 0755 "${ROOT}/packaging/debian/postrm" "${STAGE}/DEBIAN/postrm"
install -m 0644 "${ROOT}/packaging/debian/copyright" "${STAGE}/usr/share/doc/${PKG}/copyright"

# md5sums over the shipped payload (dpkg-deb does not generate these).
(
	cd "$STAGE"
	find . -type f ! -path '*/__pycache__/*' -printf '%P\0' |
		sort -z |
		xargs -0 md5sum > "${STAGE}/DEBIAN/md5sums"
)

# --------------------------------------------------------------------- build --
log "building ${DEB}"
mkdir -p "$DIST"
rm -f "$DEB" "$ZIP"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
chmod 0755 "$DEB"

log "packaging ${ZIP}"
# -X strips extra attributes, -9 compresses, and the stored unix mode (0755)
# comes from the file on disk so the .deb stays executable for Mint users.
(cd "$DIST" && zip -X -9 -q "$(basename "$ZIP")" "$(basename "$DEB")")

# -------------------------------------------------------------------- verify --
CPU_ARCH="$(dpkg --print-architecture)"
[ "$(dpkg-deb -f "$DEB" Architecture)" = "all" ] || die "unexpected Architecture field"
[ "$(dpkg-deb -f "$DEB" Version)" = "$VERSION" ] || die "unexpected Version field"

CONTENTS="$(dpkg-deb -c "$DEB" | awk '{print $6}')"

for want in ./usr/bin/samplepack-browser ./usr/bin/samplepack-mcp \
	".${LIBDIR}/server.py" ".${LIBDIR}/catalog_db.py" ".${LIBDIR}/tray.py" \
	".${LIBDIR}/index.html" ".${LIBDIR}/vendor/pystray/__init__.py" \
	./usr/share/applications/samplepack-browser.desktop \
	./usr/share/icons/hicolor/256x256/apps/samplepack-browser.png; do
	printf '%s\n' "$CONTENTS" | grep -qx "$want" || die "payload missing: ${want}"
done

# The .deb inside the archive must carry the executable bit.
ZIP_MODE="$(unzip -Z "$ZIP" | awk -v name="$(basename "$DEB")" '$NF == name {print $1}')"
[ -n "$ZIP_MODE" ] || die "cannot inspect $(basename "$DEB") inside ${ZIP}"
case "$ZIP_MODE" in
-rwx*) : ;;
*) die "$(basename "$DEB") inside the zip is not executable (mode ${ZIP_MODE})" ;;
esac

log "deb  : ${DEB} ($(du -h "$DEB" | cut -f1), mode $(stat -c '%A' "$DEB"))"
log "zip  : ${ZIP} ($(du -h "$ZIP" | cut -f1))"
log "zip  : $(basename "$DEB") mode ${ZIP_MODE} (verified executable)"
log "host : ${CPU_ARCH}; package arch ${ARCH}"
log "done"
