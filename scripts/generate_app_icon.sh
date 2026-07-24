#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSET_DIR="$ROOT_DIR/assets"
SVG_FILE="$ASSET_DIR/app-icon.svg"
PNG_FILE="$ASSET_DIR/app-icon.png"
ICONSET_DIR="$ASSET_DIR/app-icon.iconset"
ICNS_FILE="$ASSET_DIR/app-icon.icns"

if command -v magick >/dev/null 2>&1; then
  RENDER_CMD=(magick -background none "$SVG_FILE" -resize 1024x1024 "$PNG_FILE")
elif command -v convert >/dev/null 2>&1; then
  RENDER_CMD=(convert -background none "$SVG_FILE" -resize 1024x1024 "$PNG_FILE")
else
  echo "ImageMagick is required. Install it with: brew install imagemagick" >&2
  exit 1
fi

"${RENDER_CMD[@]}"

rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

sips -z 16 16 "$PNG_FILE" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
sips -z 32 32 "$PNG_FILE" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$PNG_FILE" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
sips -z 64 64 "$PNG_FILE" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$PNG_FILE" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
sips -z 256 256 "$PNG_FILE" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$PNG_FILE" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
sips -z 512 512 "$PNG_FILE" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$PNG_FILE" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$PNG_FILE" --out "$ICONSET_DIR/icon_512x512@2x.png" >/dev/null

iconutil -c icns "$ICONSET_DIR" -o "$ICNS_FILE"

echo "Generated $PNG_FILE and $ICNS_FILE"
