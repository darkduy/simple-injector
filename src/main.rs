#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod gui;
mod injector;
mod settings;

use injector::InjectorService;

fn main() -> eframe::Result<()> {
    let service: &'static InjectorService = Box::leak(Box::new(InjectorService::new()));

    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_inner_size([560.0, 680.0])
            .with_resizable(true),
        ..Default::default()
    };

    eframe::run_native(
        "simple-injector",
        options,
        Box::new(|_cc| Ok(Box::new(gui::InjectorApp::new(service)))),
    )
}