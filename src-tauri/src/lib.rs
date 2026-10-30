//! Chainscope desktop shell.
//!
//! The shell is deliberately thin: it launches the Python engine as a local
//! sidecar process and loads the web UI (../ui) which talks to that engine over
//! http://127.0.0.1:8799. All real logic lives in the engine, so the UI is
//! identical whether it runs here or in a plain browser.
//!
//! Prod: a frozen, Python-free engine (PyInstaller) is bundled as a Tauri
//!       externalBin and launched directly — no Python on the target machine.
//! Dev:  if no frozen sidecar is present, fall back to `python api.py` from the
//!       repo root. The spawn/kill lifecycle is identical either way.
//!
//! The investigation database lives in the per-user app-data directory (passed
//! as `--db`), so it is writable even when the app is installed read-only.
//! See BUILD-DESKTOP.md for freezing (build-engine.ps1 / build-engine.sh).

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

/// Holds the sidecar child so we can terminate it when the window closes.
struct Engine(Mutex<Option<Child>>);

/// Path to the frozen engine next to the app executable, if the installer
/// shipped one. Tauri copies an externalBin beside the main binary with the
/// target-triple suffix stripped, so it is just `chainscope-engine[.exe]`.
fn bundled_sidecar() -> Option<PathBuf> {
    let dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let name = if cfg!(windows) { "chainscope-engine.exe" } else { "chainscope-engine" };
    let path = dir.join(name);
    path.exists().then_some(path)
}

fn engine_args(db: &Path) -> Vec<String> {
    vec![
        "--host".into(), "127.0.0.1".into(),
        "--port".into(), "8799".into(),
        "--db".into(), db.to_string_lossy().into_owned(),
    ]
}

fn spawn_engine(db: &Path) -> Option<Child> {
    let args = engine_args(db);

    // Production: launch the frozen, self-contained engine directly.
    if let Some(sidecar) = bundled_sidecar() {
        match Command::new(&sidecar).args(&args).spawn() {
            Ok(child) => {
                println!("[shell] frozen chainscope-engine started (pid {})", child.id());
                return Some(child);
            }
            Err(e) => eprintln!("[shell] frozen sidecar failed to start: {e}; trying python"),
        }
    }

    // Dev fallback: run the engine from source with the system Python. Under
    // `tauri dev` the working directory is `src-tauri`, so the repo root (which
    // holds api.py and the chainscope package) is one level up.
    let repo_root = std::env::current_dir().ok()?.parent()?.to_path_buf();
    let python = if cfg!(windows) { "python" } else { "python3" };
    match Command::new(python)
        .arg("api.py")
        .args(&args)
        .current_dir(&repo_root)
        .spawn()
    {
        Ok(child) => {
            println!("[shell] chainscope-engine started from source (pid {})", child.id());
            Some(child)
        }
        Err(e) => {
            eprintln!("[shell] failed to start engine: {e}. Start it manually: python api.py");
            None
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Per-user, writable location for the investigation DB.
            let data_dir = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| std::env::temp_dir());
            let _ = std::fs::create_dir_all(&data_dir);
            let db = data_dir.join("chainscope.db");
            app.manage(Engine(Mutex::new(spawn_engine(&db))));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<Engine>() {
                    if let Some(mut child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Chainscope shell");
}
