"""
InjectorService – Roblox Fast Flags memory injector.

Lock ordering (always acquire in this order to prevent deadlock):
    1. _apply_lock   (coarse: one apply batch at a time)
    2. _state_lock   (fine:   guards process handle / connection state)

WriteProcessMemory is called **outside** _state_lock after a safe snapshot
of (handle, address), so we never hold a lock across a syscall.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_LOCAL_APP_DATA = Path(os.getenv("LOCALAPPDATA", Path.home() / ".local" / "share"))
DATA_PATH = _LOCAL_APP_DATA / "ez"
DATA_PATH.mkdir(parents=True, exist_ok=True)
FFS_FILE = DATA_PATH / "ffs.json"

OFFSETS_URL = (
    "https://raw.githubusercontent.com/darkduy/simple-injector"
    "/refs/heads/main/fflags.hpp"
)

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

# Known flag type prefixes (longest first to avoid partial matches)
_FLAG_PREFIXES = (
    "DFString", "FString",
    "DFFlag", "FFlag",
    "DFInt", "FInt",
    "FLog",
)

# ---------------------------------------------------------------------------
# Win32 API wiring
# ---------------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

def _bind(name, argtypes, restype):
    fn = getattr(_k32, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn

_OpenProcess = _bind(
    "OpenProcess",
    [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    wintypes.HANDLE,
)
_WriteProcessMemory = _bind(
    "WriteProcessMemory",
    [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ],
    wintypes.BOOL,
)
_CloseHandle = _bind("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)

_CreateToolhelp32Snapshot = _bind(
    "CreateToolhelp32Snapshot",
    [wintypes.DWORD, wintypes.DWORD],
    wintypes.HANDLE,
)
_Process32FirstW = _bind(
    "Process32FirstW",
    [wintypes.HANDLE, ctypes.c_void_p],
    wintypes.BOOL,
)
_Process32NextW = _bind(
    "Process32NextW",
    [wintypes.HANDLE, ctypes.c_void_p],
    wintypes.BOOL,
)
_Module32FirstW = _bind(
    "Module32FirstW",
    [wintypes.HANDLE, ctypes.c_void_p],
    wintypes.BOOL,
)
_Module32NextW = _bind(
    "Module32NextW",
    [wintypes.HANDLE, ctypes.c_void_p],
    wintypes.BOOL,
)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * wintypes.MAX_PATH),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * wintypes.MAX_PATH),
    ]


# ---------------------------------------------------------------------------
# Low-level helpers (module-level, no class state)
# ---------------------------------------------------------------------------

def _iter_snapshot(snapshot, entry_type, first_fn, next_fn):
    """Yield successive entries from a Toolhelp32 snapshot."""
    entry = entry_type()
    entry.dwSize = ctypes.sizeof(entry)
    if first_fn(snapshot, ctypes.byref(entry)):
        while True:
            yield entry
            if not next_fn(snapshot, ctypes.byref(entry)):
                break


def _find_pid_and_base(target_lower: str) -> tuple[int | None, int | None]:
    """
    Single pass: enumerate processes, then enumerate modules for the match.
    Returns (pid, base_address) or (None, None).
    """
    # --- pass 1: find pid ---
    snap = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE:
        return None, None

    pid = None
    try:
        for e in _iter_snapshot(snap, _PROCESSENTRY32W, _Process32FirstW, _Process32NextW):
            if e.szExeFile and e.szExeFile.lower() == target_lower:
                pid = e.th32ProcessID
                break
    finally:
        _CloseHandle(snap)

    if pid is None:
        return None, None

    # --- pass 2: find base address ---
    snap = _CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE:
        return pid, None

    base = None
    try:
        for e in _iter_snapshot(snap, _MODULEENTRY32W, _Module32FirstW, _Module32NextW):
            if e.szModule and e.szModule.lower() == target_lower:
                base = ctypes.cast(e.modBaseAddr, ctypes.c_void_p).value
                break
    finally:
        _CloseHandle(snap)

    return pid, base


def _open_process(pid: int) -> wintypes.HANDLE | None:
    handle = _OpenProcess(PROCESS_ACCESS, False, pid)
    if not handle or handle == INVALID_HANDLE:
        return None
    return handle


def _strip_flag_prefix(name: str) -> str:
    for prefix in _FLAG_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _parse_flag_value(value) -> int | None:
    """
    Map a flag value to a uint32 integer suitable for WriteProcessMemory.
    Accepts: bool strings, integers (decimal / hex), floats (as bit-cast).
    """
    s = str(value).strip().lower()
    if s == "true":
        return 1
    if s == "false":
        return 0
    try:
        return int(s, 0)
    except ValueError:
        pass
    try:
        # float flag: reinterpret bits as uint32
        f = float(s)
        return ctypes.c_uint32.from_buffer_copy(ctypes.c_float(f)).value
    except ValueError:
        return None


def _write_uint32(handle: wintypes.HANDLE, address: int, value: int) -> bool:
    buf = ctypes.c_uint32(value)
    written = ctypes.c_size_t(0)
    ok = _WriteProcessMemory(
        handle,
        ctypes.c_void_p(address),
        ctypes.byref(buf),
        ctypes.sizeof(buf),
        ctypes.byref(written),
    )
    return bool(ok) and written.value == ctypes.sizeof(buf)


# ---------------------------------------------------------------------------
# InjectorService
# ---------------------------------------------------------------------------

class InjectorService:
    """
    Monitors Roblox, attaches when found, injects Fast Flag values into memory.

    Thread safety
    -------------
    _state_lock  – guards: process_handle, base_address, process_pid,
                            is_connected, status_callback, apply_result_callback
    _apply_lock  – ensures only one apply-all batch runs at a time
    """

    # Retry attempts when a flag write fails on the first pass
    RETRY_COUNT = 2
    # Seconds between process-monitor polls
    POLL_INTERVAL = 1.5
    # Seconds between retry passes inside run_apply_all
    RETRY_DELAY = 0.05

    def __init__(self) -> None:
        self.added_flags: dict[str, object] = _load_json(FFS_FILE, {})
        self.offsets: dict[str, int] = {}

        # Protected by _state_lock
        self._process_handle: wintypes.HANDLE | None = None
        self._base_address: int | None = None
        self._process_pid: int | None = None
        self._is_connected: bool = False
        self._status_callback: Callable[[bool], None] | None = None
        self._apply_result_callback: Callable[[dict[str, bool]], None] | None = None

        self._state_lock = threading.Lock()
        self._apply_lock = threading.Lock()

        self._running = False
        self._monitor_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public properties (read-only snapshots, no lock needed for bools)
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_data(self) -> None:
        FFS_FILE.write_text(json.dumps(self.added_flags, indent=4), encoding="utf-8")

    def export_to_file(self, path: Path) -> None:
        path.write_text(json.dumps(self.added_flags, indent=4), encoding="utf-8")

    # ------------------------------------------------------------------
    # Offset fetching
    # ------------------------------------------------------------------

    def fetch_offsets(self) -> None:
        try:
            with urllib.request.urlopen(OFFSETS_URL, timeout=5) as resp:
                text = resp.read().decode("utf-8", errors="replace")

            matches = re.findall(
                r"\b(?:static\s+)?inline\s+constexpr\s+(?:const\s+)?"
                r"(?:uintptr_t|auto)\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)",
                text,
            )
            new_offsets = {
                _strip_flag_prefix(name): int(val, 16)
                for name, val in matches
            }
            if new_offsets:
                self.offsets = new_offsets
                print(f"Offsets loaded: {len(self.offsets)}")
            else:
                print("Warning: no offsets parsed; retaining previous offsets")
        except Exception as exc:
            print(f"Warning: fetch_offsets failed: {exc}")

    # ------------------------------------------------------------------
    # Monitor lifecycle
    # ------------------------------------------------------------------

    def start_monitor(
        self,
        status_callback: Callable[[bool], None] | None = None,
        apply_result_callback: Callable[[dict[str, bool]], None] | None = None,
    ) -> None:
        if self._running:
            return
        with self._state_lock:
            self._status_callback = status_callback
            self._apply_result_callback = apply_result_callback
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="ProcessMonitor", daemon=True
        )
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._running = False
        t = self._monitor_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._monitor_thread = None
        self._detach()

    # ------------------------------------------------------------------
    # Inject
    # ------------------------------------------------------------------

    def inject(self, name: str, value) -> bool:
        """Write a single flag to process memory. Thread-safe."""
        clean_name = _strip_flag_prefix(name)
        offset = self.offsets.get(clean_name)
        if offset is None:
            return False

        val = _parse_flag_value(value)
        if val is None:
            print(f"inject: invalid value for '{clean_name}': {value!r}")
            return False

        # Snapshot handle + address under lock, then write outside lock
        with self._state_lock:
            if not self._process_handle or self._base_address is None:
                return False
            handle = self._process_handle
            address = self._base_address + offset

        try:
            return _write_uint32(handle, address, val)
        except OSError as exc:
            print(f"inject: WriteProcessMemory failed for '{clean_name}': {exc}")
            return False
        except Exception as exc:
            print(f"inject: unexpected error for '{clean_name}': {exc}")
            return False

    def run_apply_all(self) -> None:
        """
        Asynchronously inject every flag in added_flags.
        No-op if not connected or a batch is already running.
        """
        if not self._is_connected or not self._process_handle:
            return
        if not self._apply_lock.acquire(blocking=False):
            return  # batch already in progress

        items = list(self.added_flags.items())
        if not items:
            self._apply_lock.release()
            return

        def _task() -> None:
            try:
                self._apply_batch(items)
            finally:
                self._apply_lock.release()

        threading.Thread(target=_task, name="ApplyBatch", daemon=True).start()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_batch(self, items: list[tuple[str, object]]) -> None:
        status: dict[str, bool] = {name: False for name, _ in items}
        remaining = len(items)

        for attempt in range(self.RETRY_COUNT):
            if not self._is_connected or remaining == 0:
                break
            for name, value in items:
                if status[name]:
                    continue
                if not self._is_connected:
                    break
                if self.inject(name, value):
                    status[name] = True
                    remaining -= 1
            if remaining == 0 or attempt == self.RETRY_COUNT - 1:
                break
            time.sleep(self.RETRY_DELAY)

        with self._state_lock:
            cb = self._apply_result_callback
        if cb:
            cb(status)

        applied = sum(status.values())
        print(f"Applied FFlags: {applied}/{len(status)}")

    def _attach(self, pid: int) -> bool:
        """Open process, resolve base address, store state atomically."""
        handle = _open_process(pid)
        if not handle:
            return False

        _, base = _find_pid_and_base(TARGET_PROCESS_LOWER)
        if base is None:
            _CloseHandle(handle)
            return False

        # Swap out old handle before storing the new one
        old_handle = None
        with self._state_lock:
            old_handle = self._process_handle
            self._process_handle = handle
            self._base_address = base
            self._process_pid = pid

        if old_handle:
            _CloseHandle(old_handle)

        self._set_connected(True)
        print(f"Roblox attached: PID={pid}, base={base:#x}")
        return True

    def _detach(self) -> None:
        handle = None
        with self._state_lock:
            handle = self._process_handle
            self._process_handle = None
            self._base_address = None
            self._process_pid = None

        if handle:
            _CloseHandle(handle)

        self._set_connected(False)

    def _set_connected(self, state: bool) -> None:
        cb = None
        with self._state_lock:
            if self._is_connected == state:
                return
            self._is_connected = state
            cb = self._status_callback
        if cb:
            try:
                cb(state)
            except Exception as exc:
                print(f"status_callback raised: {exc}")

    def _monitor_loop(self) -> None:
        while self._running:
            pid, _ = _find_pid_and_base(TARGET_PROCESS_LOWER)

            with self._state_lock:
                connected = self._is_connected
                current_pid = self._process_pid

            if pid and (not connected or current_pid != pid):
                self._attach(pid)
            elif not pid and connected:
                self._detach()

            time.sleep(self.POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not read {path.name}: {exc}")
        return default
