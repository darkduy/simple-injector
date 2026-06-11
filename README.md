# simple-injector

A small Python injector GUI app for Roblox flag injection.

## Overview

- Uses `PyQt6` for the GUI.
- Uses `pymem` to attach to `RobloxPlayerBeta.exe`.
- Fetches flag offsets from a remote URL and writes values directly into process memory.

## Files

- `main.py` — application launcher
- `gui.py` — PyQt6 GUI implementation
- `injector.py` — backend injector logic
- `.github/workflows/build-exe.yml` — GitHub Actions workflow to build a Windows executable

## Run locally

```bash
python main.py
```

> Install required Python modules:

```bash
pip install -r requirements.txt
```

## Troubleshooting

- If the app fails to start, confirm `PyQt6` is installed for your Python version.
- If `pymem` cannot attach, run the app with administrator privileges and ensure `RobloxPlayerBeta.exe` is running.
- If the GitHub Actions build fails, verify the workflow is using `windows-latest` and the `pyinstaller` package is installed.

## Build executable locally

```bash
pyinstaller --onefile --noconsole --name simple-injector main.py
```

## GitHub Actions

A workflow is included at `.github/workflows/build-exe.yml`.
It builds the project on `windows-latest`, installs dependencies, runs PyInstaller, and uploads `dist/simple-injector.exe` as an artifact.
