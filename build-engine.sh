#!/usr/bin/env bash
# Freeze api.py into a Python-free sidecar and stage it for Tauri.
#
#   ./build-engine.sh
#
# Produces dist/chainscope-engine, then copies it to
# src-tauri/binaries/chainscope-engine-<target-triple> where `tauri build`
# expects an externalBin. Run this before `tauri build` whenever the engine
# (api.py or the chainscope package) changes.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

echo "==> Freezing engine with PyInstaller..."
python -m PyInstaller engine.spec --noconfirm --distpath dist --workpath build/pyi

# Tauri names an externalBin "<name>-<rustc host triple>".
triple="$(rustc -vV | sed -n 's/^host: //p')"
[ -n "$triple" ] || { echo "could not read rustc host triple (is rustc on PATH?)"; exit 1; }

ext=""
case "$triple" in *windows*) ext=".exe" ;; esac
mkdir -p src-tauri/binaries
dest="src-tauri/binaries/chainscope-engine-${triple}${ext}"
cp "dist/chainscope-engine${ext}" "$dest"

echo "==> Sidecar staged: $dest"
echo "    tauri.conf.json bundle.externalBin -> binaries/chainscope-engine"
