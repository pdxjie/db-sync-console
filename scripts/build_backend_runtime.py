#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "pyinstaller" / "syncdog-backend.spec"
BACKEND_NAME = "syncdog-backend"


def default_platform_name() -> str:
    system_map = {
        "Darwin": "darwin",
        "Windows": "win32",
        "Linux": "linux",
    }
    machine_map = {
        "AMD64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    system = system_map.get(platform.system(), sys.platform)
    arch = machine_map.get(platform.machine(), platform.machine())
    return f"{system}-{arch}"


def executable_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def main() -> int:
    parser = argparse.ArgumentParser(description="Build bundled SyncDog backend runtime")
    parser.add_argument("--platform", default=default_platform_name(), help="Output platform name, for example win32-x64")
    parser.add_argument("--clean", action="store_true", help="Pass --clean to PyInstaller")
    args = parser.parse_args()

    dist_path = ROOT / "build" / "pyinstaller" / args.platform
    work_path = ROOT / "build" / "pyinstaller" / f"work-{args.platform}"
    source_dir = dist_path / BACKEND_NAME
    target_dir = ROOT / "desktop" / "backend-dist" / args.platform / BACKEND_NAME

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
    ]
    if args.clean:
        command.append("--clean")
    command.append(str(SPEC))

    subprocess.run(command, cwd=ROOT, check=True)

    if not (source_dir / executable_name(BACKEND_NAME)).exists():
        raise FileNotFoundError(f"PyInstaller output not found: {source_dir}")

    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir)
    print(f"Bundled backend copied to {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
