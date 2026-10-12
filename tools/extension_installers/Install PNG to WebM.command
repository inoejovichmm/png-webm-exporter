#!/bin/bash
# Installs the PNG to WebM After Effects panel for the current user.
set -euo pipefail
cd "$(dirname "$0")"
target="$HOME/Library/Application Support/Adobe/CEP/extensions/org.pngwebmexporter.ae"
rm -rf "$target"
mkdir -p "$target"
ditto -x -k "PNG-to-WebM-AE.zxp" "$target"
# Zip extraction drops execute bits and may carry the download quarantine flag.
chmod +x "$target/bin/png-webm-encoder/png-webm-encoder" \
         "$target/bin/png-webm-encoder/_internal/bin/ffmpeg" \
         "$target/bin/png-webm-encoder/_internal/bin/ffprobe"
xattr -dr com.apple.quarantine "$target" 2>/dev/null || true
echo "PNG to WebM is installed."
echo "Restart After Effects, then open Window > Extensions > PNG to WebM."
