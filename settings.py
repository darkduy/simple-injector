import os
from ctypes import wintypes
from pathlib import Path

# Paths
_LOCAL_APP_DATA = Path(os.getenv("LOCALAPPDATA", Path.home() / ".local" / "share"))
DATA_PATH = _LOCAL_APP_DATA / "ez"
DATA_PATH.mkdir(parents=True, exist_ok=True)
FFS_FILE = DATA_PATH / "ffs.json"

# Remote offsets source
OFFSETS_URL = (
    "https://raw.githubusercontent.com/darkduy/simple-injector"
    "/refs/heads/main/fflags.hpp"
)

# Target process
TARGET_PROCESS = "RobloxPlayerBeta.exe"
TARGET_PROCESS_LOWER = TARGET_PROCESS.lower()

# Process-access rights
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_OPERATION = 0x0008
_PROCESS_VM_WRITE = 0x0020
PROCESS_ACCESS = _PROCESS_QUERY_INFORMATION | _PROCESS_VM_OPERATION | _PROCESS_VM_WRITE

# Toolhelp32 snapshot flags
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

INVALID_HANDLE = wintypes.HANDLE(-1).value

# Known Fast Flag prefixes (longest first to avoid partial matches)
FLAG_PREFIXES = (
    "DFString", "FString",
    "DFFlag", "FFlag",
    "DFInt", "FInt",
    "FLog",
)

# Service tuning
RETRY_COUNT = 2
POLL_INTERVAL = 1.5
RETRY_DELAY = 0.05