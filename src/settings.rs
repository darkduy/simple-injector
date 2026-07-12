use std::path::PathBuf;
use std::sync::LazyLock;

pub const OFFSETS_URL: &str =
    "https://raw.githubusercontent.com/darkduy/simple-injector/refs/heads/main/fflags.hpp";

pub const TARGET_PROCESS: &str = "RobloxPlayerBeta.exe";

pub const RETRY_COUNT: u32 = 2;
pub const POLL_INTERVAL_MS: u64 = 1500;
pub const RETRY_DELAY_MS: u64 = 50;

/// Longest-prefix-first so e.g. "DFFlag" is stripped before "FFlag".
pub const FLAG_PREFIXES: &[&str] = &[
    "DFString", "FString",
    "DFFlag", "FFlag",
    "DFInt", "FInt",
    "FLog",
];

pub static DATA_PATH: LazyLock<PathBuf> = LazyLock::new(|| {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            dirs_home().join(".local").join("share")
        });
    let path = base.join("ez");
    if let Err(e) = std::fs::create_dir_all(&path) {
        eprintln!(
            "Warning: could not create data directory {}: {e}. Saving FFlags will fail until this is resolved.",
            path.display()
        );
    }
    path
});

pub static FFS_FILE: LazyLock<PathBuf> = LazyLock::new(|| DATA_PATH.join("ffs.json"));

fn dirs_home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}