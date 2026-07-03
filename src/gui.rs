use std::collections::HashMap;
use std::sync::mpsc::{Receiver, Sender};

use eframe::egui;

use crate::injector::{InjectorEvent, InjectorService};

enum FlagStatus {
    Pending,
    Success,
    Fail,
}

pub struct InjectorApp {
    service: &'static InjectorService,
    event_rx: Receiver<InjectorEvent>,

    is_connected: bool,
    flag_statuses: HashMap<String, FlagStatus>,
    apply_in_progress: bool,
    apply_summary: Option<(usize, usize)>, // (success, fail)

    search_text: String,
    show_add_dialog: bool,
    add_dialog_text: String,
    error_message: Option<String>,
}

impl InjectorApp {
    pub fn new(service: &'static InjectorService) -> Self {
        let (tx, rx): (Sender<InjectorEvent>, Receiver<InjectorEvent>) =
            std::sync::mpsc::channel();

        {
            let service_for_offsets = service;
            std::thread::spawn(move || service_for_offsets.fetch_offsets());
        }
        service.start_monitor(tx);

        Self {
            service,
            event_rx: rx,
            is_connected: false,
            flag_statuses: HashMap::new(),
            apply_in_progress: false,
            apply_summary: None,
            search_text: String::new(),
            show_add_dialog: false,
            add_dialog_text: String::new(),
            error_message: None,
        }
    }

    fn drain_events(&mut self) {
        while let Ok(event) = self.event_rx.try_recv() {
            match event {
                InjectorEvent::ConnectionChanged(connected) => {
                    self.is_connected = connected;
                }
                InjectorEvent::ApplyResult(status_map) => {
                    self.apply_in_progress = false;
                    let success = status_map.values().filter(|v| **v).count();
                    let fail = status_map.len() - success;
                    self.apply_summary = Some((success, fail));

                    self.flag_statuses = status_map
                        .into_iter()
                        .map(|(k, ok)| {
                            (k, if ok { FlagStatus::Success } else { FlagStatus::Fail })
                        })
                        .collect();
                }
            }
        }
    }

    fn run_apply_all(&mut self) {
        self.apply_in_progress = true;
        self.apply_summary = None;
        self.service.run_apply_all();
    }
}

impl eframe::App for InjectorApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.drain_events();
        apply_dark_theme(ctx);

        egui::CentralPanel::default().show(ctx, |ui| {
            ui.horizontal(|ui| {
                let (text, color) = if self.is_connected {
                    ("Status: connected", egui::Color32::from_rgb(0x44, 0xFF, 0x44))
                } else {
                    ("Status: waiting for Roblox...", egui::Color32::from_rgb(0xFF, 0x44, 0x44))
                };
                ui.colored_label(color, text);
            });

            let flag_count = self.service.added_flags.lock().unwrap().len();
            ui.label(
                egui::RichText::new(format!("Modified FFlags: {flag_count}"))
                    .color(egui::Color32::GRAY)
                    .size(11.0),
            );

            ui.horizontal(|ui| {
                if ui.button("Add FFlag").clicked() {
                    self.show_add_dialog = true;
                    self.add_dialog_text.clear();
                }
                if ui.button("Export").clicked() {
                    if let Some(path) = rfd_save_dialog() {
                        if let Err(e) = self.service.export_to_file(&path) {
                            self.error_message = Some(format!("Export failed: {e}"));
                        }
                    }
                }
            });

            ui.add(
                egui::TextEdit::singleline(&mut self.search_text)
                    .hint_text("Search FFlags..."),
            );

            ui.separator();

            egui::ScrollArea::vertical().show(ui, |ui| {
                let flags: Vec<(String, String)> = {
                    let guard = self.service.added_flags.lock().unwrap();
                    guard
                        .iter()
                        .filter(|(name, _)| {
                            self.search_text.is_empty()
                                || name.to_lowercase().contains(&self.search_text.to_lowercase())
                        })
                        .map(|(k, v)| (k.clone(), v.clone()))
                        .collect()
                };

                let mut to_remove: Option<String> = None;

                egui::Grid::new("flags_grid")
                    .num_columns(4)
                    .striped(true)
                    .show(ui, |ui| {
                        ui.strong("Name");
                        ui.strong("Value");
                        ui.strong("Status");
                        ui.strong("");
                        ui.end_row();

                        for (name, value) in &flags {
                            ui.label(name);
                            ui.label(value);

                            let (text, color) = match self.flag_statuses.get(name) {
                                Some(FlagStatus::Success) => {
                                    ("Success", egui::Color32::from_rgb(0x44, 0xFF, 0x44))
                                }
                                Some(FlagStatus::Fail) => {
                                    ("Fail", egui::Color32::from_rgb(0xFF, 0x44, 0x44))
                                }
                                _ => ("Pending", egui::Color32::GRAY),
                            };
                            ui.colored_label(color, text);

                            if ui.small_button("x").clicked() {
                                to_remove = Some(name.clone());
                            }
                            ui.end_row();
                        }
                    });

                if let Some(name) = to_remove {
                    self.service.added_flags.lock().unwrap().remove(&name);
                    self.service.save_data();
                }
            });

            ui.separator();

            if let Some((success, fail)) = self.apply_summary {
                let color = if fail == 0 {
                    egui::Color32::from_rgb(0x44, 0xFF, 0x44)
                } else {
                    egui::Color32::from_rgb(0xFF, 0x44, 0x44)
                };
                ui.colored_label(color, format!("Apply result: {success} success, {fail} fail"));
            }

            ui.horizontal(|ui| {
                let can_apply = self.is_connected
                    && !self.apply_in_progress
                    && flag_count > 0;

                let label = if self.apply_in_progress { "Applying..." } else { "Apply All" };
                if ui.add_enabled(can_apply, egui::Button::new(label)).clicked() {
                    self.run_apply_all();
                }
            });

            if let Some(err) = self.error_message.clone() {
                egui::Window::new("Error").show(ctx, |ui| {
                    ui.label(&err);
                    if ui.button("OK").clicked() {
                        self.error_message = None;
                    }
                });
            }
        });

        if self.show_add_dialog {
            let mut open = true;
            let mut submitted = false;

            egui::Window::new("Add FFlags")
                .open(&mut open)
                .collapsible(false)
                .resizable(true)
                .show(ctx, |ui| {
                    ui.label("Paste JSON here:");
                    ui.add(
                        egui::TextEdit::multiline(&mut self.add_dialog_text)
                            .desired_rows(12)
                            .code_editor(),
                    );
                    ui.horizontal(|ui| {
                        if ui.button("ADD").clicked() {
                            submitted = true;
                        }
                        if ui.button("CANCEL").clicked() {
                            self.show_add_dialog = false;
                        }
                        if ui.button("IMPORT FROM FILE").clicked() {
                            if let Some(path) = rfd_open_dialog() {
                                if let Ok(text) = std::fs::read_to_string(path) {
                                    self.add_dialog_text = text;
                                }
                            }
                        }
                    });
                });

            if !open {
                self.show_add_dialog = false;
            }

            if submitted {
                match parse_flags_json(&self.add_dialog_text) {
                    Ok(map) => {
                        let mut flags = self.service.added_flags.lock().unwrap();
                        for (k, v) in map {
                            flags.insert(k, v);
                        }
                        drop(flags);
                        self.service.save_data();
                        self.show_add_dialog = false;
                    }
                    Err(e) => {
                        self.error_message = Some(format!("Invalid JSON: {e}"));
                    }
                }
            }
        }

        ctx.request_repaint_after(std::time::Duration::from_millis(200));
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.service.stop_monitor();
    }
}

fn parse_flags_json(text: &str) -> Result<HashMap<String, String>, String> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err("No JSON provided.".into());
    }

    let normalized = strip_trailing_commas(trimmed);
    let value: serde_json::Value =
        serde_json::from_str(&normalized).map_err(|e| e.to_string())?;

    let obj = value.as_object().ok_or("JSON must be a key/value object.")?;

    let mut out = HashMap::new();
    for (k, v) in obj {
        let s = match v {
            serde_json::Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        out.insert(k.clone(), s);
    }
    Ok(out)
}

fn strip_trailing_commas(text: &str) -> String {
    let re = regex::Regex::new(r",\s*([\]}])").unwrap();
    re.replace_all(text, "$1").into_owned()
}

fn rfd_open_dialog() -> Option<std::path::PathBuf> {
    rfd::FileDialog::new().add_filter("JSON", &["json"]).pick_file()
}

fn rfd_save_dialog() -> Option<std::path::PathBuf> {
    rfd::FileDialog::new()
        .add_filter("JSON", &["json"])
        .set_file_name("fflags.json")
        .save_file()
}

fn apply_dark_theme(ctx: &egui::Context) {
    let mut visuals = egui::Visuals::dark();
    visuals.window_fill = egui::Color32::from_rgb(0x0F, 0x0F, 0x11);
    visuals.panel_fill = egui::Color32::from_rgb(0x0F, 0x0F, 0x11);
    visuals.widgets.inactive.bg_fill = egui::Color32::from_rgb(0x1E, 0x1E, 0x21);
    visuals.widgets.hovered.bg_fill = egui::Color32::from_rgb(0x3A, 0x00, 0x00);
    visuals.selection.bg_fill = egui::Color32::from_rgb(0x2A, 0x00, 0x00);
    ctx.set_visuals(visuals);
}