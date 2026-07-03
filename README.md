# simple-injector

A small Rust GUI application for Roblox Fast Flag memory injection.

## Overview

- Uses `egui`/`eframe` for the GUI.
- Enumerates processes and modules via the Windows Toolhelp32 API
  (`windows` crate) to locate `RobloxPlayerBeta.exe` and its base address.
- Writes flag values directly into process memory with `WriteProcessMemory`.
- Fetches flag offsets from a remote URL and parses them out of a C++ header.

## Files

- `src/main.rs` — application entry point
- `src/gui.rs` — egui GUI implementation
- `src/injector.rs` — process discovery, offset parsing, and memory injection
- `src/settings.rs` — paths, remote URL, and tuning constants
- `.github/workflows/build-exe.yml` — GitHub Actions workflow to build a Windows executable
- `.github/workflows/codeql.yml` — CodeQL static analysis workflow

## Run locally

```bash
cargo run --release
```

Requires the Rust toolchain (edition 2024, so a recent stable compiler —
install via [rustup](https://rustup.rs)).

## Troubleshooting

- If the app fails to start, confirm you're running on Windows — the
  process/memory APIs used here are Windows-only.
- If injection doesn't attach, run the app with administrator privileges
  and confirm `RobloxPlayerBeta.exe` is running.
- If the GitHub Actions build fails, verify the workflow is using
  `windows-latest` and that the Rust toolchain step succeeded.

## Build executable locally

```bash
cargo build --release
```

The binary is produced at `target/release/simple-injector.exe`. Rust
produces a single native executable directly — no separate packaging step
(like PyInstaller) is needed.

## GitHub Actions

A workflow is included at `.github/workflows/build-exe.yml`.
It builds the project on `windows-latest` with `cargo build --release`
and uploads `target/release/simple-injector.exe` as an artifact.

A second workflow, `.github/workflows/codeql.yml`, runs CodeQL static
analysis against the Rust source and the workflow files themselves.