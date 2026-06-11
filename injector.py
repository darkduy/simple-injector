import json
import os
import re
import threading
import time
import ctypes
from pathlib import Path

import pymem
import requests

DATA_PATH = Path(os.getenv('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'ez'
DATA_PATH.mkdir(parents=True, exist_ok=True)
FFS_FILE = DATA_PATH / 'ffs.json'
SETTINGS_FILE = DATA_PATH / 'settings.json'
OFFSETS_URL = 'https://raw.githubusercontent.com/azayan165-svg/fflags.hpp/refs/heads/main/fflags.hpp'

ntdll = ctypes.WinDLL('ntdll', use_last_error=True)

class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_int), ('Information', ctypes.c_void_p)]

NtWriteVirtualMemory = ntdll.NtWriteVirtualMemory
NtWriteVirtualMemory.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(IO_STATUS_BLOCK),
]


class InjectorService:
    def __init__(self):
        self.added_flags = self.load_json(FFS_FILE, {})
        self.settings = self.load_json(SETTINGS_FILE, {'auto_apply': False})
        self.offsets = {}
        self.pm = None
        self.is_connected = False
        self.status_callback = None
        self.auto_apply_callback = None

    def load_json(self, path: Path, default):
        return json.loads(path.read_text()) if path.exists() else default

    def save_data(self):
        FFS_FILE.write_text(json.dumps(self.added_flags, indent=4))
        SETTINGS_FILE.write_text(json.dumps(self.settings))

    def clean_prefix(self, name: str) -> str:
        prefixes = ['FFlag', 'DFFlag', 'FInt', 'DFInt', 'FString', 'DFString', 'FLog']
        for prefix in prefixes:
            if name.startswith(prefix):
                return name[len(prefix):]
        return name

    def fetch_offsets(self):
        try:
            response = requests.get(OFFSETS_URL, timeout=5)
            matches = re.findall(r'(\w+)\s*=\s*(0x[0-9A-Fa-f]+)', response.text)
            self.offsets = {self.clean_prefix(name): int(value, 16) for name, value in matches}
            print(f'Offsets loaded: {len(self.offsets)}')
        except Exception:
            print('Offsets loaded: 0 (Error)')

    def start_monitor(self, status_callback=None, auto_apply_callback=None):
        self.status_callback = status_callback
        self.auto_apply_callback = auto_apply_callback
        threading.Thread(target=self.process_monitor, daemon=True).start()

    def process_monitor(self):
        while True:
            try:
                pm_obj = pymem.Pymem('RobloxPlayerBeta.exe')
                if not self.is_connected:
                    self.pm = pm_obj
                    self.is_connected = True
                    print(f'Roblox connected: {pm_obj.process_id}')
                    if self.status_callback:
                        self.status_callback(True)
                    if self.settings.get('auto_apply') and self.auto_apply_callback:
                        self.auto_apply_callback()
            except Exception:
                if self.is_connected:
                    self.is_connected = False
                    self.pm = None
                    if self.status_callback:
                        self.status_callback(False)
            time.sleep(1.5)

    def run_apply_all(self):
        if not self.is_connected or not self.pm:
            return None

        def task():
            applied_count = 0
            for _ in range(5):
                applied_count = 0
                for name, value in self.added_flags.items():
                    if self.inject(name, value):
                        applied_count += 1
                time.sleep(0.05)
            print(f'Applied FFlags: {applied_count}')

        threading.Thread(target=task, daemon=True).start()

    def inject(self, name: str, value) -> bool:
        if not self.pm:
            return False

        name = self.clean_prefix(name)
        if name not in self.offsets:
            return False

        address = self.pm.base_address + self.offsets[name]
        try:
            text_value = str(value).lower()
            val = 1 if text_value == 'true' else 0 if text_value == 'false' else int(value)
            data = ctypes.c_int32(val)
            sb = IO_STATUS_BLOCK()
            result = NtWriteVirtualMemory(self.pm.process_handle, address, ctypes.byref(data), 4, ctypes.byref(sb))
            return result == 0
        except Exception:
            return False

    def toggle_auto_apply(self, state):
        self.settings['auto_apply'] = bool(state)
        self.save_data()

    def export_to_file(self, path: Path):
        path.write_text(json.dumps(self.added_flags, indent=4))
