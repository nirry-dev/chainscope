# PyInstaller spec — freeze the Chainscope engine into a single, Python-free
# binary.
#
#   python -m PyInstaller engine.spec --noconfirm   ->   dist/chainscope-engine[.exe]
#
# The Tauri desktop shell ships this frozen engine as an externalBin "sidecar"
# so the installed app needs no Python on the target machine. collect_submodules
# guarantees the whole chainscope package is frozen (api.py reaches most of it,
# but the adapters/sources are pulled in explicitly to be safe). The web UI is
# served by Tauri in the desktop app, so it is NOT bundled into the engine; the
# frozen engine only answers /api/*.
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("chainscope")

a = Analysis(
    ["api.py"],
    pathex=["."],
    binaries=[],
    datas=[("chainscope/samples/*.json", "chainscope/samples")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # stdlib-only, headless HTTP server — drop GUI/dev baggage from the freeze.
    excludes=["tkinter", "PyInstaller", "pytest", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="chainscope-engine",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # headless HTTP server bound to 127.0.0.1 — no window
    disable_windowed_traceback=False,
)
