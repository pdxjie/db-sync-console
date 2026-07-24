#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-dmg}"
HOST_ARCH="$(uname -m)"
if [[ "$(uname -s)" == "Darwin" ]] && [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]]; then
  HOST_ARCH="arm64"
fi
REQUESTED_ARCH="${ELECTRON_ARCH:-$HOST_ARCH}"

case "$REQUESTED_ARCH" in
  arm64|aarch64)
    ELECTRON_ARCH_FLAG="--arm64"
    BACKEND_ARCH_VALUE="arm64"
    ;;
  x64|x86_64|amd64)
    ELECTRON_ARCH_FLAG="--x64"
    BACKEND_ARCH_VALUE="x64"
    ;;
  *)
    echo "Unsupported ELECTRON_ARCH: $REQUESTED_ARCH" >&2
    exit 1
    ;;
esac

cd "$ROOT_DIR"

npm run renderer:build
"${PYTHON:-python3}" - <<'PY'
from pathlib import Path
import shutil

path = Path("desktop/backend-dist")
if path.exists():
    shutil.rmtree(path)
PY
BACKEND_ARCH="$BACKEND_ARCH_VALUE" npm run backend:build
electron-builder --mac "$TARGET" "$ELECTRON_ARCH_FLAG"
