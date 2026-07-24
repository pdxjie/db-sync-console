#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
HOST_ARCH="$(uname -m)"
if [[ "$(uname -s)" == "Darwin" ]] && [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]]; then
  HOST_ARCH="arm64"
fi
REQUESTED_ARCH="${BACKEND_ARCH:-$HOST_ARCH}"

case "$REQUESTED_ARCH" in
  arm64|aarch64)
    BACKEND_ARCH_NAME="arm64"
    ARCH_ARGS=(/usr/bin/arch -arm64)
    ;;
  x64|x86_64|amd64)
    BACKEND_ARCH_NAME="x64"
    ARCH_ARGS=(/usr/bin/arch -x86_64)
    ;;
  *)
    echo "Unsupported BACKEND_ARCH: $REQUESTED_ARCH" >&2
    exit 1
    ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  ARCH_ARGS=()
fi

if ! "${ARCH_ARGS[@]}" "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller is not installed. Install build dependencies with:" >&2
  echo "  $PYTHON_BIN -m pip install -r requirements-build.txt" >&2
  exit 1
fi

DIST_ROOT="$ROOT_DIR/desktop/backend-dist/darwin-$BACKEND_ARCH_NAME"
WORK_ROOT="$ROOT_DIR/build/pyinstaller/darwin-$BACKEND_ARCH_NAME"
SPEC_ROOT="$ROOT_DIR/build/pyinstaller/spec"

"${ARCH_ARGS[@]}" "$PYTHON_BIN" - <<PY
from pathlib import Path
import shutil

for path in [
    Path("$DIST_ROOT"),
    Path("$WORK_ROOT"),
    Path("$SPEC_ROOT/syncdog-backend.spec"),
]:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
PY

"${ARCH_ARGS[@]}" "$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --name syncdog-backend \
  --distpath "$DIST_ROOT" \
  --workpath "$WORK_ROOT" \
  --specpath "$SPEC_ROOT" \
  --paths "$ROOT_DIR" \
  --collect-submodules sync_tool \
  --collect-data sync_tool \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import pymysql \
  --exclude-module uvloop \
  --exclude-module httptools \
  --exclude-module watchfiles \
  --exclude-module tkinter \
  "$ROOT_DIR/sync_tool/backend_runtime.py"

BACKEND_BIN="$DIST_ROOT/syncdog-backend/syncdog-backend"
if [[ ! -x "$BACKEND_BIN" ]]; then
  echo "Backend binary was not created: $BACKEND_BIN" >&2
  exit 1
fi

"${ARCH_ARGS[@]}" "$PYTHON_BIN" - <<PY
from pathlib import Path
import shutil

internal = Path("$DIST_ROOT/syncdog-backend/_internal")
framework = internal / "Python.framework"
versions = framework / "Versions"
version_dirs = [path for path in versions.iterdir() if path.name != "Current" and path.is_dir()]
if version_dirs:
    version_dir = sorted(version_dirs, key=lambda path: path.name)[-1]

    def replace_path(target: Path, source: Path) -> None:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False)
        else:
            shutil.copy2(source, target)

    replace_path(internal / "Python", version_dir / "Python")
    replace_path(framework / "Python", version_dir / "Python")
    replace_path(framework / "Resources", version_dir / "Resources")
    replace_path(versions / "Current", version_dir)
PY

echo "Built backend runtime: $BACKEND_BIN"
