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
    apply_summary: Option<(usize, usize)>,

    search_text: String,
    show_add_dialog: bool,
    add_dialog_text: String,
    show_delete_all_confirm: bool,
    error_message: Option<String>,

    // Inline editing state: name of the flag currently being edited, and
    // the draft value text (kept separate so partial edits don't hit disk
    // on every keystroke).
    editing_flag: Option<String>,
    edit_draft: String,
    rename_draft: String,
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
            show_delete_all_confirm: false,
            error_message: None,
            editing_flag: None,
            edit_draft: String::new(),
            rename_draft: String::new(),
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

    fn start_editing(&mut self, name: &str, value: &str) {
        self.editing_flag = Some(name.to_string());
        self.rename_draft = name.to_string();
        self.edit_draft = value.to_string();
    }

    fn cancel_editing(&mut self) {
        self.editing_flag = None;
        self.edit_draft.clear();
        self.rename_draft.clear();
    }

    fn commit_editing(&mut self) {
        let Some(original_name) = self.editing_flag.take() else { return };
        let new_name = self.rename_draft.trim().to_string();
        let new_value = self.edit_draft.clone();

        if new_name.is_empty() {
            self.error_message = Some("Flag name cannot be empty.".into());
            self.edit_draft.clear();
            self.rename_draft.clear();
            return;
        }

        let mut flags = self.service.added_flags.lock().unwrap();

        if new_name != original_name && flags.contains_key(&new_name) {
            drop(flags);
            self.error_message = Some(format!("Flag '{new_name}' already exists."));
            self.edit_draft.clear();
            self.rename_draft.clear();
            return;
        }

        flags.remove(&original_name);
        flags.insert(new_name, new_value);
        drop(flags);

        self.service.save_data();
        self.edit_draft.clear();
        self.rename_draft.clear();
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

            ui.label(
                egui::RichText::new(format!(
                    "Modified FFlags: {}",
                    self.service.added_flags.lock().unwrap().len()
                ))
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
                let delete_all_enabled = !self.service.added_flags.lock().unwrap().is_empty();
                if ui
                    .add_enabled(delete_all_enabled, egui::Button::new("Delete All"))
                    .clicked()
                {
                    self.show_delete_all_confirm = true;
                }
            });

            ui.add(
                egui::TextEdit::singleline(&mut self.search_text)
                    .hint_text("Search FFlags..."),
            );

            ui.separator();

            const ROW_HEIGHT: f32 = 24.0;

            egui::ScrollArea::vertical()
                .id_salt("flags_panel_outer")
                .max_height((ui.available_height() - 70.0).max(80.0))
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    let flags: Vec<(String, String)> = {
                        let guard = self.service.added_flags.lock().unwrap();
                        let mut v: Vec<(String, String)> = guard
                            .iter()
                            .filter(|(name, _)| {
                                self.search_text.is_empty()
                                    || name
                                        .to_lowercase()
                                        .contains(&self.search_text.to_lowercase())
                            })
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect();
                        v.sort_by(|a, b| a.0.cmp(&b.0));
                        v
                    };

                    let mut to_remove: Option<String> = None;
                    let mut to_start_edit: Option<(String, String)> = None;
                    let mut commit_requested = false;
                    let mut cancel_requested = false;

                    // Header row stays fixed, outside the virtualized area.
                    ui.horizontal(|ui| {
                        ui.add_sized([180.0, ROW_HEIGHT], egui::Label::new(
                            egui::RichText::new("Name").strong(),
                        ));
                        ui.add_sized([140.0, ROW_HEIGHT], egui::Label::new(
                            egui::RichText::new("Value").strong(),
                        ));
                        ui.add_sized([70.0, ROW_HEIGHT], egui::Label::new(
                            egui::RichText::new("Status").strong(),
                        ));
                        ui.label("");
                    });
                    ui.separator();

                    // Only the rows currently scrolled into view are built
                    // each frame, so the UI stays fast with large flag sets.
                    egui::ScrollArea::vertical()
                        .id_salt("flags_virtual_list")
                        .show_rows(ui, ROW_HEIGHT, flags.len(), |ui, row_range| {
                            for i in row_range {
                                let (name, value) = &flags[i];
                                let is_editing =
                                    self.editing_flag.as_deref() == Some(name.as_str());

                                ui.horizontal(|ui| {
                                    if is_editing {
                                        let name_resp = ui.add_sized(
                                            [180.0, ROW_HEIGHT],
                                            egui::TextEdit::singleline(&mut self.rename_draft),
                                        );
                                        let value_resp = ui.add_sized(
                                            [140.0, ROW_HEIGHT],
                                            egui::TextEdit::singleline(&mut self.edit_draft),
                                        );
                                        let enter_pressed =
                                            ui.input(|i| i.key_pressed(egui::Key::Enter));
                                        if enter_pressed
                                            && (name_resp.lost_focus()
                                                || value_resp.lost_focus())
                                        {
                                            commit_requested = true;
                                        }
                                        let escape_pressed =
                                            ui.input(|i| i.key_pressed(egui::Key::Escape));
                                        if escape_pressed
                                            && (name_resp.has_focus() || value_resp.has_focus())
                                        {
                                            cancel_requested = true;
                                        }
                                        ui.add_sized([70.0, ROW_HEIGHT], egui::Label::new("-"));
                                        if ui.small_button("Save").clicked() {
                                            commit_requested = true;
                                        }
                                        if ui.small_button("Cancel").clicked() {
                                            cancel_requested = true;
                                        }
                                    } else {
                                        ui.add_sized(
                                            [180.0, ROW_HEIGHT],
                                            egui::Label::new(name).truncate(),
                                        )
                                        .on_hover_text(name);
                                        ui.add_sized(
                                            [140.0, ROW_HEIGHT],
                                            egui::Label::new(value).truncate(),
                                        )
                                        .on_hover_text(value);

                                        let (text, color) = match self.flag_statuses.get(name) {
                                            Some(FlagStatus::Success) => (
                                                "Success",
                                                egui::Color32::from_rgb(0x44, 0xFF, 0x44),
                                            ),
                                            Some(FlagStatus::Fail) => (
                                                "Fail",
                                                egui::Color32::from_rgb(0xFF, 0x44, 0x44),
                                            ),
                                            _ => ("Pending", egui::Color32::GRAY),
                                        };
                                        ui.add_sized(
                                            [70.0, ROW_HEIGHT],
                                            egui::Label::new(
                                                egui::RichText::new(text).color(color),
                                            ),
                                        );

                                        if ui.small_button("Edit").clicked() {
                                            to_start_edit =
                                                Some((name.clone(), value.clone()));
                                        }
                                        if ui.small_button("x").clicked() {
                                            to_remove = Some(name.clone());
                                        }
                                    }
                                });
                            }
                        });

                    if let Some((name, value)) = to_start_edit {
                        self.start_editing(&name, &value);
                    }
                    if commit_requested {
                        self.commit_editing();
                    }
                    if cancel_requested {
                        self.cancel_editing();
                    }
                    if let Some(name) = to_remove {
                        self.service.added_flags.lock().unwrap().remove(&name);
                        self.service.save_data();
                        if self.editing_flag.as_deref() == Some(name.as_str()) {
                            self.cancel_editing();
                        }
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
                    && !self.service.added_flags.lock().unwrap().is_empty();

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

        if self.show_delete_all_confirm {
            let mut open = true;
            let mut confirmed = false;

            egui::Window::new("Confirm Delete All")
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .default_size([320.0, 120.0])
                .show(ctx, |ui| {
                    ui.label("Delete all modified FFlags? This cannot be undone.");
                    ui.horizontal(|ui| {
                        if ui
                            .button(egui::RichText::new("Delete All").color(egui::Color32::from_rgb(0xFF, 0x44, 0x44)))
                            .clicked()
                        {
                            confirmed = true;
                        }
                        if ui.button("Cancel").clicked() {
                            self.show_delete_all_confirm = false;
                        }
                    });
                });

            if !open {
                self.show_delete_all_confirm = false;
            }
            if confirmed {
                self.service.added_flags.lock().unwrap().clear();
                self.service.save_data();
                self.flag_statuses.clear();
                self.apply_summary = None;
                self.cancel_editing();
                self.show_delete_all_confirm = false;
            }
        }

        if self.show_add_dialog {
            let mut open = true;
            let mut submitted = false;

            egui::Window::new("Add FFlags")
                .open(&mut open)
                .collapsible(false)
                .resizable(true)
                .default_size([460.0, 380.0])
                .min_size([340.0, 260.0])
                .max_size([700.0, 600.0])
                .show(ctx, |ui| {
                    ui.label("Paste JSON here:");
                    egui::ScrollArea::vertical()
                        .id_salt("add_dialog_textarea")
                        .max_height(280.0)
                        .show(ui, |ui| {
                            ui.add(
                                egui::TextEdit::multiline(&mut self.add_dialog_text)
                                    .desired_rows(12)
                                    .desired_width(ui.available_width())
                                    .code_editor(),
                            );
                        });
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