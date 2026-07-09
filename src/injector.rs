use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Mutex, TryLockError};
use std::thread;
use std::time::Duration;

use regex::Regex;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Module32FirstW, Module32NextW, Process32FirstW, Process32NextW,
    MODULEENTRY32W, PROCESSENTRY32W, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_OPERATION, PROCESS_VM_WRITE,
};

use crate::settings;

pub enum InjectorEvent {
    ConnectionChanged(bool),
    ApplyResult(HashMap<String, bool>),
}

struct ProcessState {
    handle: Option<HANDLE>,
    base_address: Option<usize>,
    pid: Option<u32>,
    is_connected: bool,
}

impl Default for ProcessState {
    fn default() -> Self {
        Self { handle: None, base_address: None, pid: None, is_connected: false }
    }
}

unsafe impl Send for ProcessState {}

pub struct InjectorService {
    pub added_flags: Mutex<HashMap<String, String>>,
    offsets: Mutex<HashMap<String, usize>>,

    state: Mutex<ProcessState>,
    apply_lock: Mutex<()>,

    running: AtomicBool,
    event_tx: Mutex<Option<Sender<InjectorEvent>>>,
}

impl InjectorService {
    pub fn new() -> Self {
        let added_flags = load_flags_from_disk();
        Self {
            added_flags: Mutex::new(added_flags),
            offsets: Mutex::new(HashMap::new()),
            state: Mutex::new(ProcessState::default()),
            apply_lock: Mutex::new(()),
            running: AtomicBool::new(false),
            event_tx: Mutex::new(None),
        }
    }

    pub fn is_connected(&self) -> bool {
        self.state.lock().unwrap().is_connected
    }

    pub fn save_data(&self) {
        let flags = self.added_flags.lock().unwrap();
        if let Ok(json) = serde_json::to_string_pretty(&*flags) {
            let _ = std::fs::write(&*settings::FFS_FILE, json);
        }
    }

    pub fn export_to_file(&self, path: &std::path::Path) -> std::io::Result<()> {
        let flags = self.added_flags.lock().unwrap();
        let json = serde_json::to_string_pretty(&*flags).unwrap_or_default();
        std::fs::write(path, json)
    }

    pub fn fetch_offsets(&self) {
        let result: Result<String, String> = ureq::get(settings::OFFSETS_URL)
            .timeout(Duration::from_secs(5))
            .call()
            .map_err(|e| e.to_string())
            .and_then(|resp| resp.into_string().map_err(|e| e.to_string()));

        let text = match result {
            Ok(t) => t,
            Err(e) => {
                eprintln!("Warning: fetch_offsets failed: {e}");
                return;
            }
        };

        let re = Regex::new(
            r"\b(?:static\s+)?inline\s+constexpr\s+(?:const\s+)?(?:uintptr_t|auto)\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)",
        ).unwrap();

        let mut new_offsets = HashMap::new();
        for cap in re.captures_iter(&text) {
            let name = strip_flag_prefix(&cap[1]).to_string();
            if let Ok(val) = usize::from_str_radix(cap[2].trim_start_matches("0x"), 16) {
                new_offsets.insert(name, val);
            }
        }

        if new_offsets.is_empty() {
            eprintln!("Warning: no offsets parsed; retaining previous offsets");
            return;
        }

        let count = new_offsets.len();
        *self.offsets.lock().unwrap() = new_offsets;
        println!("Offsets loaded: {count}");
    }

    pub fn start_monitor(self: &'static Self, event_tx: Sender<InjectorEvent>) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        *self.event_tx.lock().unwrap() = Some(event_tx);

        thread::Builder::new()
            .name("ProcessMonitor".into())
            .spawn(move || self.monitor_loop())
            .expect("failed to spawn monitor thread");
    }

    pub fn stop_monitor(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.detach();
    }

    pub fn inject(&self, name: &str, value: &str) -> bool {
        let clean_name = strip_flag_prefix(name);

        let offset = match self.offsets.lock().unwrap().get(clean_name).copied() {
            Some(o) => o,
            None => return false,
        };

        let val = match parse_flag_value(value) {
            Some(v) => v,
            None => {
                eprintln!("inject: invalid value for '{clean_name}': {value:?}");
                return false;
            }
        };

        let (handle, address) = {
            let state = self.state.lock().unwrap();
            match (state.handle, state.base_address) {
                (Some(h), Some(base)) => (h, base + offset),
                _ => return false,
            }
        };

        write_u32(handle, address, val)
    }

    pub fn run_apply_all(self: &'static Self) {
        let ready = self.state.lock().unwrap().is_connected;
        if !ready {
            return;
        }

        let guard = match self.apply_lock.try_lock() {
            Ok(guard) => guard,
            Err(TryLockError::WouldBlock) => return,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
        };

        let items: Vec<(String, String)> = self
            .added_flags
            .lock()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();

        if items.is_empty() {
            return;
        }

        thread::Builder::new()
            .name("ApplyBatch".into())
            .spawn(move || {
                let _guard = guard;
                self.apply_batch(&items);
            })
            .expect("failed to spawn apply thread");
    }

    fn apply_batch(&self, items: &[(String, String)]) {
        let mut status: HashMap<String, bool> =
            items.iter().map(|(name, _)| (name.clone(), false)).collect();
        let mut remaining = items.len();

        for attempt in 0..settings::RETRY_COUNT {
            if !self.is_connected() || remaining == 0 {
                break;
            }
            for (name, value) in items {
                if status[name] {
                    continue;
                }
                if !self.is_connected() {
                    break;
                }
                if self.inject(name, value) {
                    status.insert(name.clone(), true);
                    remaining -= 1;
                }
            }
            if remaining == 0 || attempt == settings::RETRY_COUNT - 1 {
                break;
            }
            thread::sleep(Duration::from_millis(settings::RETRY_DELAY_MS));
        }

        let applied = status.values().filter(|v| **v).count();
        println!("Applied FFlags: {applied}/{}", status.len());

        self.emit(InjectorEvent::ApplyResult(status));
    }

    fn attach(&self, pid: u32, base: Option<usize>) -> bool {
        let handle = match open_process(pid) {
            Some(h) => h,
            None => return false,
        };

        let base = match base.or_else(|| find_pid_and_base().and_then(|(_, b)| b)) {
            Some(b) => b,
            None => {
                unsafe { let _ = CloseHandle(handle); }
                return false;
            }
        };

        let old_handle = {
            let mut state = self.state.lock().unwrap();
            let old = state.handle.take();
            state.handle = Some(handle);
            state.base_address = Some(base);
            state.pid = Some(pid);
            old
        };

        if let Some(old) = old_handle {
            unsafe { let _ = CloseHandle(old); }
        }

        self.set_connected(true);
        println!("Roblox attached: PID={pid}, base={base:#x}");
        true
    }

    fn detach(&self) {
        let handle = {
            let mut state = self.state.lock().unwrap();
            let h = state.handle.take();
            state.base_address = None;
            state.pid = None;
            h
        };

        if let Some(h) = handle {
            unsafe { let _ = CloseHandle(h); }
        }

        self.set_connected(false);
    }

    fn set_connected(&self, connected: bool) {
        let changed = {
            let mut state = self.state.lock().unwrap();
            if state.is_connected == connected {
                false
            } else {
                state.is_connected = connected;
                true
            }
        };
        if changed {
            self.emit(InjectorEvent::ConnectionChanged(connected));
        }
    }

    fn emit(&self, event: InjectorEvent) {
        if let Some(tx) = self.event_tx.lock().unwrap().as_ref() {
            let _ = tx.send(event);
        }
    }

    fn monitor_loop(&self) {
        while self.running.load(Ordering::SeqCst) {
            let found = find_pid_and_base();
            let (pid, base) = match found {
                Some((pid, base)) => (Some(pid), base),
                None => (None, None),
            };

            let (connected, current_pid) = {
                let state = self.state.lock().unwrap();
                (state.is_connected, state.pid)
            };

            match pid {
                Some(pid) if !connected || current_pid != Some(pid) => {
                    self.attach(pid, base);
                }
                None if connected => {
                    self.detach();
                }
                _ => {}
            }

            thread::sleep(Duration::from_millis(settings::POLL_INTERVAL_MS));
        }
    }
}

fn strip_flag_prefix(name: &str) -> &str {
    for prefix in settings::FLAG_PREFIXES {
        if let Some(stripped) = name.strip_prefix(prefix) {
            return stripped;
        }
    }
    name
}

fn parse_flag_value(value: &str) -> Option<u32> {
    let s = value.trim().to_ascii_lowercase();
    match s.as_str() {
        "true" => return Some(1),
        "false" => return Some(0),
        _ => {}
    }

    if let Some(hex) = s.strip_prefix("0x") {
        if let Ok(v) = u32::from_str_radix(hex, 16) {
            return Some(v);
        }
    }
    if let Ok(v) = s.parse::<i64>() {
        return Some(v as u32);
    }
    if let Ok(f) = s.parse::<f32>() {
        return Some(f.to_bits());
    }
    None
}

fn write_u32(handle: HANDLE, address: usize, value: u32) -> bool {
    use windows::Win32::System::Diagnostics::Debug::WriteProcessMemory;

    let buf = value.to_ne_bytes();
    let mut written: usize = 0;

    let ok = unsafe {
        WriteProcessMemory(
            handle,
            address as *const _,
            buf.as_ptr() as *const _,
            buf.len(),
            Some(&mut written),
        )
    };

    ok.is_ok() && written == buf.len()
}

fn open_process(pid: u32) -> Option<HANDLE> {
    let access = PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE;
    unsafe { OpenProcess(access, windows::Win32::Foundation::BOOL(0), pid).ok() }
}

fn find_pid_and_base() -> Option<(u32, Option<usize>)> {
    let pid = find_pid_by_name(settings::TARGET_PROCESS)?;
    let base = find_module_base(pid, settings::TARGET_PROCESS);
    Some((pid, base))
}

fn find_pid_by_name(target: &str) -> Option<u32> {
    let target_lower = target.to_ascii_lowercase();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).ok()?;
        let _guard = HandleGuard(snap);

        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;

        if Process32FirstW(snap, &mut entry).is_err() {
            return None;
        }
        loop {
            let name = wchar_to_string(&entry.szExeFile);
            if name.to_ascii_lowercase() == target_lower {
                return Some(entry.th32ProcessID);
            }
            if Process32NextW(snap, &mut entry).is_err() {
                return None;
            }
        }
    }
}

fn find_module_base(pid: u32, module_name: &str) -> Option<usize> {
    let target_lower = module_name.to_ascii_lowercase();
    unsafe {
        let snap =
            CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid).ok()?;
        let _guard = HandleGuard(snap);

        let mut entry: MODULEENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<MODULEENTRY32W>() as u32;

        if Module32FirstW(snap, &mut entry).is_err() {
            return None;
        }
        loop {
            let name = wchar_to_string(&entry.szModule);
            if name.to_ascii_lowercase() == target_lower {
                return Some(entry.modBaseAddr as usize);
            }
            if Module32NextW(snap, &mut entry).is_err() {
                return None;
            }
        }
    }
}

struct HandleGuard(HANDLE);
impl Drop for HandleGuard {
    fn drop(&mut self) {
        unsafe { let _ = CloseHandle(self.0); }
    }
}

fn wchar_to_string(buf: &[u16]) -> String {
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf16_lossy(&buf[..len])
}

fn load_flags_from_disk() -> HashMap<String, String> {
    let path = &*settings::FFS_FILE;
    if !path.exists() {
        return HashMap::new();
    }
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(e) => {
            eprintln!("Warning: could not read {}: {e}", path.display());
            HashMap::new()
        }
    }
}