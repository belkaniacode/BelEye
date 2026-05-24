#!/usr/bin/env bash
# Install BelEye into the user's desktop environment:
#   - ~/.local/share/applications/beleye.desktop (menu / dock entry)
#   - ~/.local/share/icons/hicolor/<size>x<size>/apps/beleye.png  (raster icons)
#   - ~/.local/share/icons/hicolor/scalable/apps/beleye.svg       (vector icon)
#
# Re-runs are idempotent. Run from anywhere; the script resolves its own
# project root.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

# Prefer the project's virtualenv if it exists, otherwise system python3.
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

mkdir -p "$APP_DIR"

# Generate the .desktop content with absolute paths so it works from
# any working directory the WM might launch us from.
cat > "$APP_DIR/beleye.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BelEye
GenericName=Video Surveillance
Comment=RTSP camera viewer
Categories=AudioVideo;Video;
Terminal=false
StartupNotify=true
StartupWMClass=BelEye
Exec=$PYTHON_BIN $PROJECT_ROOT/main.py
Icon=beleye
Path=$PROJECT_ROOT
EOF
echo "installed: $APP_DIR/beleye.desktop"

for size in 16 24 32 48 64 96 128 256 512; do
    dst="$ICON_BASE/${size}x${size}/apps"
    mkdir -p "$dst"
    cp "$PROJECT_ROOT/resources/icons/beleye-${size}.png" "$dst/beleye.png"
done
mkdir -p "$ICON_BASE/scalable/apps"
cp "$PROJECT_ROOT/resources/icons/beleye.svg" "$ICON_BASE/scalable/apps/beleye.svg"
echo "installed icons under $ICON_BASE"

command -v update-desktop-database >/dev/null \
    && update-desktop-database "$APP_DIR" 2>/dev/null \
    && echo "update-desktop-database: ok" || true
command -v gtk-update-icon-cache >/dev/null \
    && gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null \
    && echo "gtk-update-icon-cache: ok" || true

echo "Done. BelEye is now in the system menu."
