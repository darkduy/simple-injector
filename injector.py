import json
import os
import re
import threading
import time
import ctypes
import urllib.request
from ctypes import wintypes
from pathlib import Path

DATA_PATH = Path(os.getenv('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'ez'
DATA_PATH.mkdir(parents=True, exist_ok=True)
FFS_FILE = DATA_PATH / 'ffs.json'
SETTINGS_FILE = DATA_PATH / 'settings.json'
OFFSETS_URL = 'https://raw.githubusercontent.com/azayan165-svg/fflags.hpp/refs/heads/main/fflags.hpp'

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE

WriteProcessMemory = kernel32.WriteProcessMemory
WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
WriteProcessMemory.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
CreateToolhelp32Snapshot.restype = wintypes.HANDLE

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * wintypes.MAX_PATH),
    ]

Process32FirstW = kernel32.Process32FirstW
Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
Process32FirstW.restype = wintypes.BOOL

Process32NextW = kernel32.Process32NextW
Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
Process32NextW.restype = wintypes.BOOL

class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('th32ModuleID', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('GlblcntUsage', wintypes.DWORD),
        ('ProccntUsage', wintypes.DWORD),
        ('modBaseAddr', ctypes.POINTER(ctypes.c_byte)),
        ('modBaseSize', wintypes.DWORD),
        ('hModule', wintypes.HMODULE),
        ('szModule', ctypes.c_wchar * 256),
        ('szExePath', ctypes.c_wchar * wintypes.MAX_PATH),
    ]

Module32FirstW = kernel32.Module32FirstW
Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
Module32FirstW.restype = wintypes.BOOL

Module32NextW = kernel32.Module32NextW
Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
Module32NextW.restype = wintypes.BOOL

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_OPEN_ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class InjectorService:
    def __init__(self):
        self.added_flags = self.load_json(FFS_FILE, {})
        self.settings = self.load_json(SETTINGS_FILE, {'auto_apply': False})
        self.offsets = {}
        self.process_handle = None
        self.base_address = None
        self.process_pid = None
        self.is_connected = False
        self.status_callback = None
        self.auto_apply_callback = None
        self.target_process_name = 'RobloxPlayerBeta.exe'
        self.running = False
        self.monitor_thread = None
        self.process_lock = threading.Lock()
        self.inject_lock = threading.Lock()
        self.apply_lock = threading.Lock()
        self.retry_count = 2

    def load_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as exc:
            print(f'Warning: failed to read {path.name}: {exc}')
            return default

    def save_data(self):
        FFS_FILE.write_text(json.dumps(self.added_flags, indent=4), encoding='utf-8')
        SETTINGS_FILE.write_text(json.dumps(self.settings), encoding='utf-8')

    def clean_prefix(self, name: str) -> str:
        prefixes = ['FFlag', 'DFFlag', 'FInt', 'DFInt', 'FString', 'DFString', 'FLog']
        for prefix in prefixes:
            if name.startswith(prefix):
                return name[len(prefix):]
        return name

    def fetch_offsets(self):
        try:
            with urllib.request.urlopen(OFFSETS_URL, timeout=5) as response:
                text = response.read().decode('utf-8', errors='replace')
            matches = re.findall(r'(\w+)\s*=\s*(0x[0-9A-Fa-f]+)', text)
            new_offsets = {self.clean_prefix(name): int(value, 16) for name, value in matches}
            if new_offsets:
                self.offsets = new_offsets
                print(f'Offsets loaded: {len(self.offsets)}')
            else:
                print('Warning: no offsets found; keeping existing offsets')
        except Exception as exc:
            print(f'Warning: failed to fetch offsets: {exc}')

    def start_monitor(self, status_callback=None, auto_apply_callback=None):
        if self.running:
            return
        self.status_callback = status_callback
        self.auto_apply_callback = auto_apply_callback
        self.running = True
        self.monitor_thread = threading.Thread(target=self.process_monitor, daemon=True)
        self.monitor_thread.start()

    def set_connection_state(self, state: bool):
        callback = None
        with self.process_lock:
            if self.is_connected == state:
                return
            self.is_connected = state
            callback = self.status_callback
        if callback:
            callback(state)

    def stop_monitor(self):
        self.running = False
        if self.monitor_thread is not None and self.monitor_thread is not threading.current_thread():
            self.monitor_thread.join(timeout=2.0)
            self.monitor_thread = None
        else:
            self.monitor_thread = None
        self.close_process()

    def find_process_by_name(self, name: str):
        snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return None

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)

        pid = None
        if Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile and entry.szExeFile.lower() == name.lower():
                    pid = entry.th32ProcessID
                    break
                if not Process32NextW(snapshot, ctypes.byref(entry)):
                    break

        CloseHandle(snapshot)
        return pid

    def open_process(self, pid: int):
        handle = OpenProcess(PROCESS_OPEN_ACCESS, False, pid)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        return handle

    def close_process(self):
        handle = None
        with self.inject_lock:
            with self.process_lock:
                handle = self.process_handle
                self.process_handle = None
                self.base_address = None
                self.process_pid = None
        if handle:
            CloseHandle(handle)
        self.set_connection_state(False)

    def get_main_module_base_address(self, pid: int):
        snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snapshot == INVALID_HANDLE_VALUE:
            return None

        module_entry = MODULEENTRY32W()
        module_entry.dwSize = ctypes.sizeof(module_entry)

        base_addr = None
        if Module32FirstW(snapshot, ctypes.byref(module_entry)):
            while True:
                if module_entry.szModule and module_entry.szModule.lower() == self.target_process_name.lower():
                    base_addr = ctypes.cast(module_entry.modBaseAddr, ctypes.c_void_p).value
                    break
                if not Module32NextW(snapshot, ctypes.byref(module_entry)):
                    break

        CloseHandle(snapshot)
        return base_addr

    def process_monitor(self):
        while self.running:
            pid = self.find_process_by_name(self.target_process_name)
            with self.process_lock:
                connected = self.is_connected
                current_pid = self.process_pid
            if pid and (not connected or current_pid != pid):
                handle = self.open_process(pid)
                if handle:
                    base_addr = self.get_main_module_base_address(pid)
                    if base_addr is not None:
                        self.close_process()
                        with self.process_lock:
                            self.process_handle = handle
                            self.base_address = base_addr
                            self.process_pid = pid
                        self.set_connection_state(True)
                        print(f'Roblox connected: {pid}')
                        if self.settings.get('auto_apply') and self.auto_apply_callback:
                            threading.Thread(target=self.auto_apply_callback, daemon=True).start()
                    else:
                        CloseHandle(handle)
            elif not pid and connected:
                self.close_process()
            time.sleep(1.5)

    def run_apply_all(self):
        if not self.is_connected or not self.process_handle:
            return None
        if not self.apply_lock.acquire(blocking=False):
            return None

        def task():
            try:
                with self.process_lock:
                    if not self.is_connected or not self.process_handle or self.base_address is None:
                        return
                applied_count = 0
                for _ in range(self.retry_count):
                    if not self.is_connected:
                        break
                    applied_count = 0
                    for name, value in self.added_flags.items():
                        if not self.is_connected:
                            break
                        if self.inject(name, value):
                            applied_count += 1
                    time.sleep(0.05)
                print(f'Applied FFlags: {applied_count}')
            finally:
                self.apply_lock.release()

        threading.Thread(target=task, daemon=True).start()

    def inject(self, name: str, value) -> bool:
        name = self.clean_prefix(name)
        if name not in self.offsets:
            return False

        text_value = str(value).strip().lower()
        try:
            if text_value == 'true':
                val = 1
            elif text_value == 'false':
                val = 0
            else:
                val = int(text_value, 0)
        except ValueError:
            print(f'Invalid FFlag value for {name}: {value}')
            return False

        buffer = ctypes.c_uint32(val)
        written = ctypes.c_size_t()
        with self.inject_lock:
            with self.process_lock:
                if not self.process_handle or self.base_address is None:
                    return False
                process_handle = self.process_handle
                address = ctypes.c_void_p(self.base_address + self.offsets[name])
            try:
                success = WriteProcessMemory(
                    process_handle,
                    address,
                    ctypes.byref(buffer),
                    ctypes.sizeof(buffer),
                    ctypes.byref(written),
                )
                return bool(success) and written.value == ctypes.sizeof(buffer)
            except OSError as exc:
                print(f'WriteProcessMemory failed for {name}: {exc}')
                return False
            except Exception as exc:
                print(f'Injection error for {name}: {exc}')
                return False

    def toggle_auto_apply(self, state):
        self.settings['auto_apply'] = bool(state)
        self.save_data()

    def export_to_file(self, path: Path):
        path.write_text(json.dumps(self.added_flags, indent=4), encoding='utf-8')
