# Freeze api.py into a Python-free sidecar and stage it for Tauri.
#
#   ./build-engine.ps1
#
# Produces dist/chainscope-engine.exe, then copies it to
# src-tauri/binaries/chainscope-engine-<target-triple>.exe where `tauri build`
# expects an externalBin. Run this before `tauri build` whenever the engine
# (api.py or the chainscope package) changes.
#
# Note: PyInstaller logs INFO to stderr, which Windows PowerShell flags as a
# NativeCommandError; we gate on $LASTEXITCODE instead of letting that abort us.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "==> Freezing engine with PyInstaller..."
python -m PyInstaller engine.spec --noconfirm --distpath dist --workpath build\pyi
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller failed ($LASTEXITCODE)"; exit 1 }

# Tauri names an externalBin "<name>-<rustc host triple>[.exe]".
$hostLine = (rustc -vV | Select-String '^host:')
if (-not $hostLine) { Write-Error "could not read rustc host triple (is rustc on PATH?)"; exit 1 }
$triple = $hostLine.ToString().Split(' ')[1]

$ext = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }
$binDir = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$dest = Join-Path $binDir "chainscope-engine-$triple$ext"
Copy-Item (Join-Path $root "dist\chainscope-engine$ext") $dest -Force

Write-Host "==> Sidecar staged: $dest"
Write-Host "    tauri.conf.json bundle.externalBin -> binaries/chainscope-engine"
