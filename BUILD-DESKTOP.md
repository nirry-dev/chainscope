# Building the Chainscope desktop app

Chainscope ships as a small native desktop app using **Tauri v2**: a system-
webview window (no bundled Chromium) that loads the web UI and launches the
Python engine as a local **sidecar** process. The engine is frozen with
**PyInstaller** into a single Python-free binary, so the installed app needs no
Python on the target machine.

```
┌─────────────────────────────┐
│ Tauri window (system webview)│  loads ui/  ──HTTP──▶  127.0.0.1:8799
│   src-tauri/ (Rust shell)    │                         │
└──────────────┬──────────────┘                         │
               │ spawns sidecar                          │
               ▼                                         ▼
     chainscope-engine[.exe]  ◀── frozen api.py + chainscope pkg (stdlib only)
               │
               ▼
     chainscope.db  (per-user app-data dir, writable)
```

The investigation database lives in the per-user app-data directory (passed to
the engine as `--db`), so it persists and stays writable even when the app is
installed read-only.

## Prerequisites

- **Rust** ≥ 1.77 (`rustc`, `cargo`) — Tauri v2 builds the shell.
- **Tauri CLI v2** — `npm install -g @tauri-apps/cli` (or `cargo install tauri-cli`).
- **Python 3** + **PyInstaller** (`pip install pyinstaller`) — to freeze the engine.
- Platform webview: WebView2 on Windows (preinstalled on Win 11), WebKitGTK on
  Linux, WKWebView on macOS.

## Dev loop (no freezing)

```bash
cd src-tauri
cargo tauri dev
```

`tauri dev` loads `ui/` straight from disk and the Rust shell falls back to
launching the engine from source (`python api.py`) when no frozen sidecar is
present — so you can iterate without PyInstaller. Edit the UI and reload.

## Packaged build

1. **Freeze + stage the engine sidecar** (run from the repo root):

   ```bash
   ./build-engine.sh          # macOS / Linux / Git-Bash
   ```
   ```powershell
   ./build-engine.ps1         # Windows PowerShell
   ```

   This runs PyInstaller (`engine.spec`) to produce `dist/chainscope-engine`
   and copies it to `src-tauri/binaries/chainscope-engine-<rustc-triple>` —
   the name Tauri's `externalBin` expects.

2. **Build the app + installer:**

   ```bash
   cd src-tauri
   cargo tauri build
   ```

   Output lands in `src-tauri/target/release/`:
   - the app executable (`Chainscope.exe` / `Chainscope.app` / binary), and
   - installers under `bundle/` (`.msi`/NSIS `.exe` on Windows, `.dmg` on
     macOS, `.deb`/`.AppImage` on Linux).

   Re-run `build-engine` whenever `api.py` or the `chainscope` package changes,
   then `cargo tauri build` again.

## Notes / gotchas

- **PowerShell + PyInstaller:** PyInstaller logs INFO to stderr, which Windows
  PowerShell 5.1 flags as a `NativeCommandError`. `build-engine.ps1` gates on
  `$LASTEXITCODE`, not on error output, so that is expected and harmless.
- **File lock on rebuild:** if a previous frozen engine is still running it will
  lock `dist/chainscope-engine.exe`. Kill any `chainscope-engine` process before
  re-freezing.
- **Icons:** generated from `src-tauri/icons/source.png` via `cargo tauri icon
  src-tauri/icons/source.png`. Replace `source.png` and re-run to rebrand.
- **The frozen engine only answers `/api/*`** — the desktop app serves the UI
  through Tauri, so the engine's own static-file serving (used in browser mode)
  is not needed in the packaged app.
