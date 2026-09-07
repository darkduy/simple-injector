use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{LazyLock, Mutex, MutexGuard};
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

/// Matches C++-style offset declarations, e.g.:
///   inline constexpr uintptr_t kSomeFlag = 0x1A2B3C;
/// Compiled once and reused across every `fetch_offsets` call.
static OFFSET_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\b(?:static\s+)?inline\s+constexpr\s+(?:const\s+)?(?:uintptr_t|auto)\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)",
    )
    .expect("OFFSET_PATTERN regex is a compile-time constant and must be valid")
});

/// Locks a mutex, recovering the inner value even if a prior holder panicked.
///
/// A poisoned `std::sync::Mutex` normally panics on every subsequent `.lock()`,
/// which would let one panicking thread take down the whole service. Since our
/// shared state (flags, offsets, process handle) has no invariant that a panic
/// mid-update could violate in a way we care about, recovering is safe here.
fn lock_safe<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub enum InjectorEvent {
    ConnectionChanged(bool),
    ApplyResult(HashMap<String, bool>),
}

/// Why a flag value string could not be turned into the u32 written to memory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FlagValueError {
    /// The string didn't match bool, hex, integer, or float syntax.
    Unrecognized,
}

impl std::fmt::Display for FlagValueError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unrecognized => write!(f, "value is not a bool, hex, integer, or float"),
        }
    }
}

#[derive(Default)]
struct ProcessState {
    handle: Option<HANDLE>,
    base_address: Option<usize>,
    pid: Option<u32>,
    is_connected: bool,
}

unsafe impl Send for ProcessState {}

pub struct InjectorService {
    pub added_flags: Mutex<HashMap<String, String>>,
    offsets: Mutex<HashMap<String, usize>>,

    state: Mutex<ProcessState>,
    apply_lock: Mutex<()>,
    apply_pending: AtomicBool,

    running: AtomicBool,
    event_tx: Mutex<Option<Sender<InjectorEvent>>>,
}

impl InjectorService {
    pub fn new() -> Self {
        Self {
            added_flags: Mutex::new(load_flags_from_disk()),
            offsets: Mutex::new(HashMap::new()),
            state: Mutex::new(ProcessState::default()),
            apply_lock: Mutex::new(()),
            apply_pending: AtomicBool::new(false),
            running: AtomicBool::new(false),
            event_tx: Mutex::new(None),
        }
    }

    pub fn is_connected(&self) -> bool {
        lock_safe(&self.state).is_connected
    }

    pub fn save_data(&self) -> Result<(), String> {
        let json = {
            let flags = lock_safe(&self.added_flags);
            serde_json::to_string_pretty(&*flags)
                .map_err(|e| format!("failed to serialize flags: {e}"))?
        };

        std::fs::write(&*settings::FFS_FILE, json)
            .map_err(|e| format!("could not write {}: {e}", settings::FFS_FILE.display()))
    }

    pub fn export_to_file(&self, path: &std::path::Path) -> std::io::Result<()> {
        let json = {
            let flags = lock_safe(&self.added_flags);
            serde_json::to_string_pretty(&*flags).unwrap_or_default()
        };
        std::fs::write(path, json)
    }

    pub fn fetch_offsets(&self) {
        let text = match ureq::get(settings::OFFSETS_URL)
            .timeout(Duration::from_secs(5))
            .call()
            .map_err(|e| e.to_string())
            .and_then(|resp| resp.into_string().map_err(|e| e.to_string()))
        {
            Ok(t) => t,
            Err(e) => {
                eprintln!("Warning: fetch_offsets failed: {e}");
                return;
            }
        };

        let new_offsets: HashMap<String, usize> = OFFSET_PATTERN
            .captures_iter(&text)
            .filter_map(|cap| {
                let name = strip_flag_prefix(&cap[1]).to_string();
                let value = usize::from_str_radix(cap[2].trim_start_matches("0x"), 16).ok()?;
                Some((name, value))
            })
            .collect();

        if new_offsets.is_empty() {
            eprintln!("Warning: no offsets parsed; retaining previous offsets");
            return;
        }

        let count = new_offsets.len();
        *lock_safe(&self.offsets) = new_offsets;
        println!("Offsets loaded: {count}");
    }

    pub fn start_monitor(self: &'static Self, event_tx: Sender<InjectorEvent>) {
        if self.running.swap(true, Ordering::SeqCst) {
            return;
        }
        *lock_safe(&self.event_tx) = Some(event_tx);

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

        let Some(offset) = lock_safe(&self.offsets).get(clean_name).copied() else {
            return false;
        };

        let val = match parse_flag_value(value) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("inject: invalid value for '{clean_name}': {value:?} ({e})");
                return false;
            }
        };

        let (handle, address) = {
            let state = lock_safe(&self.state);
            match (state.handle, state.base_address) {
                (Some(h), Some(base)) => (h, base + offset),
                _ => return false,
            }
        };

        write_u32(handle, address, val)
    }

    pub fn run_apply_all(self: &'static Self) {
        if !self.is_connected() {
            return;
        }

        if self
            .apply_pending
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return;
        }

        let items: Vec<(String, String)> = lock_safe(&self.added_flags)
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();

        if items.is_empty() {
            self.apply_pending.store(false, Ordering::SeqCst);
            return;
        }

        thread::Builder::new()
            .name("ApplyBatch".into())
            .spawn(move || {
                let _guard = lock_safe(&self.apply_lock);
                self.apply_batch(&items);
                self.apply_pending.store(false, Ordering::SeqCst);
            })
            .expect("failed to spawn apply thread");
    }

    /// Retries unapplied flags up to `settings::RETRY_COUNT` times, stopping
    /// early once every flag succeeds or the connection drops.
    ///
    /// `status` is the single source of truth for what's left to do; the
    /// remaining count is derived from it each round instead of tracked
    /// separately, so the two can never drift out of sync.
    fn apply_batch(&self, items: &[(String, String)]) {
        let mut status: HashMap<String, bool> =
            items.iter().map(|(name, _)| (name.clone(), false)).collect();

        for attempt in 0..settings::RETRY_COUNT {
            if !self.is_connected() {
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
                }
            }

            let all_applied = status.values().all(|&applied| applied);
            if all_applied || attempt == settings::RETRY_COUNT - 1 {
                break;
            }
            thread::sleep(Duration::from_millis(settings::RETRY_DELAY_MS));
        }

        let applied = status.values().filter(|&&v| v).count();
        println!("Applied FFlags: {applied}/{}", status.len());

        self.emit(InjectorEvent::ApplyResult(status));
    }

    fn attach(&self, pid: u32, base: Option<usize>) -> bool {
        let Some(handle) = open_process(pid) else {
            return false;
        };

        let Some(base) = base.or_else(|| find_pid_and_base().and_then(|(_, b)| b)) else {
            unsafe {
                let _ = CloseHandle(handle);
            }
            return false;
        };

        let old_handle = {
            let mut state = lock_safe(&self.state);
            let old = state.handle.take();
            state.handle = Some(handle);
            state.base_address = Some(base);
            state.pid = Some(pid);
            old
        };

        if let Some(old) = old_handle {
            unsafe {
                let _ = CloseHandle(old);
            }
        }

        self.set_connected(true);
        println!("Roblox attached: PID={pid}, base={base:#x}");
        true
    }

    fn detach(&self) {
        let handle = {
            let mut state = lock_safe(&self.state);
            let h = state.handle.take();
            state.base_address = None;
            state.pid = None;
            h
        };

        if let Some(h) = handle {
            unsafe {
                let _ = CloseHandle(h);
            }
        }

        self.set_connected(false);
    }

    fn set_connected(&self, connected: bool) {
        let changed = {
            let mut state = lock_safe(&self.state);
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
        if let Some(tx) = lock_safe(&self.event_tx).as_ref() {
            let _ = tx.send(event);
        }
    }

    fn monitor_loop(&self) {
        while self.running.load(Ordering::SeqCst) {
            let (pid, base) = match find_pid_and_base() {
                Some((pid, base)) => (Some(pid), base),
                None => (None, None),
            };

            let (connected, current_pid) = {
                let state = lock_safe(&self.state);
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
    settings::FLAG_PREFIXES
        .iter()
        .find_map(|prefix| name.strip_prefix(prefix))
        .unwrap_or(name)
}

/// Parses a flag's textual value into the u32 that gets written to memory.
/// Accepts (in order): "true"/"false", "0x"-prefixed hex, decimal integers,
/// and floats (encoded via IEEE-754 bit pattern).
fn parse_flag_value(value: &str) -> Result<u32, FlagValueError> {
    let s = value.trim().to_ascii_lowercase();

    match s.as_str() {
        "true" => return Ok(1),
        "false" => return Ok(0),
        _ => {}
    }

    if let Some(hex) = s.strip_prefix("0x") {
        if let Ok(v) = u32::from_str_radix(hex, 16) {
            return Ok(v);
        }
    }
    if let Ok(v) = s.parse::<i64>() {
        return Ok(v as u32);
    }
    if let Ok(f) = s.parse::<f32>() {
        return Ok(f.to_bits());
    }

    Err(FlagValueError::Unrecognized)
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
    let base = find_module_bounds(pid, settings::TARGET_PROCESS).map(|(base, _)| base);
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

fn find_module_bounds(pid: u32, module_name: &str) -> Option<(usize, usize)> {
    let target_lower = module_name.to_ascii_lowercase();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid).ok()?;
        let _guard = HandleGuard(snap);

        let mut entry: MODULEENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<MODULEENTRY32W>() as u32;

        if Module32FirstW(snap, &mut entry).is_err() {
            return None;
        }
        loop {
            let name = wchar_to_string(&entry.szModule);
            if name.to_ascii_lowercase() == target_lower {
                return Some((entry.modBaseAddr as usize, entry.modBaseSize as usize));
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
        unsafe {
            let _ = CloseHandle(self.0);
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_bool_values() {
        assert_eq!(parse_flag_value("true"), Ok(1));
        assert_eq!(parse_flag_value("FALSE"), Ok(0));
    }

    #[test]
    fn parses_hex_values() {
        assert_eq!(parse_flag_value("0x1A"), Ok(0x1A));
    }

    #[test]
    fn parses_integer_and_float_values() {
        assert_eq!(parse_flag_value("42"), Ok(42));
        assert_eq!(parse_flag_value("-1"), Ok(u32::MAX)); // i64 -> u32 wraparound
        assert_eq!(parse_flag_value("1.5"), Ok(1.5f32.to_bits()));
    }

    #[test]
    fn rejects_garbage_values() {
        assert_eq!(parse_flag_value("not_a_number"), Err(FlagValueError::Unrecognized));
    }

    #[test]
    fn strip_flag_prefix_removes_known_prefixes() {
        // Adjust this test if settings::FLAG_PREFIXES changes.
        for prefix in settings::FLAG_PREFIXES {
            let name = format!("{prefix}SomeFlag");
            assert_eq!(strip_flag_prefix(&name), "SomeFlag");
        }
    }
}