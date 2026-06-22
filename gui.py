import ast
import json
import re
import threading
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui

from injector import InjectorService

STYLE = """
QWidget#MainContainer {
    background-color: #0F0F11;
    border: 1px solid #222;
    border-radius: 12px;
}
QLabel {
    color: #E0E0E0;
    font-family: 'Segoe UI';
}
QLineEdit {
    background-color: #0A0A0A;
    border: 1px solid #2A2A2C;
    border-radius: 4px;
    color: #E0E0E0;
    padding: 6px;
}
QPushButton {
    background-color: #1E1E21;
    border: 1px solid #2A2A2C;
    border-radius: 4px;
    color: #E0E0E0;
    font-weight: 500;
}
QPushButton:hover { background-color: #3A0000; }
QPushButton:disabled { color: #444; border-color: #222; }
QPushButton#ApplyBtn {
    background-color: #1A0000;
    font-size: 12px;
    font-weight: bold;
}
QTableWidget {
    background-color: #000;
    border: none;
    gridline-color: #1A1A1D;
    outline: none;
    color: #BBB;
}
QTableWidget::item:selected { background-color: #2A0000; color: #FF0000; }
QHeaderView::section {
    background-color: #0F0F11;
    color: #555;
    padding: 6px;
    border: none;
    font-weight: bold;
    font-size: 10px;
}
QScrollBar:vertical {
    border: none;
    background: #0F0F11;
    width: 6px;
}
QScrollBar::handle:vertical {
    background: #2A2A2C;
    min-height: 20px;
    border-radius: 3px;
}
QCheckBox { color: #777; font-size: 11px; }
"""

_STATUS_STYLE = "font-weight: bold; font-size: 11px; border: none;"
_LABEL_STYLE = "font-size: 10px; border: none;"


def _make_btn(text: str, height: int = 32) -> QtWidgets.QPushButton:
    btn = QtWidgets.QPushButton(text)
    btn.setFixedHeight(height)
    return btn


class AddFlagDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Dialog
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(500, 420)

        outer = QtWidgets.QVBoxLayout(self)
        self.container = QtWidgets.QWidget()
        self.container.setObjectName("MainContainer")
        self.container.setStyleSheet(STYLE)
        outer.addWidget(self.container)

        lay = QtWidgets.QVBoxLayout(self.container)

        title = QtWidgets.QLabel("ADD FFLAGS")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            "color: #FF3333; font-weight: bold; font-size: 16px;"
            " letter-spacing: 2px; border: none;"
        )
        lay.addWidget(title)

        self.json_input = QtWidgets.QPlainTextEdit()
        self.json_input.setPlaceholderText("Paste JSON here...")
        self.json_input.setStyleSheet(
            "background-color: #121214; border: 1px solid #220000;"
            " color: #888; font-family: 'Consolas';"
        )
        lay.addWidget(self.json_input)

        btn_row = QtWidgets.QHBoxLayout()
        self.add_btn = _make_btn("ADD")
        self.cancel_btn = _make_btn("CANCEL")
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self.import_btn = _make_btn("IMPORT FROM FILE", height=35)
        lay.addWidget(self.import_btn)

        self.add_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.import_btn.clicked.connect(self._import_file)

    def _import_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select JSON", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            self.json_input.setPlainText(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Import Error", f"Cannot read file:\n{exc}")


class InjectorApp(QtWidgets.QMainWindow):
    _status_signal = QtCore.pyqtSignal(bool)
    _apply_result_signal = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(560, 680)

        self.service = InjectorService()
        self.added_flags = self.service.added_flags
        self.flag_statuses: dict[str, bool] = {}

        self.container = QtWidgets.QWidget()
        self.container.setObjectName("MainContainer")
        self.container.setStyleSheet(STYLE)
        self.setCentralWidget(self.container)

        root = QtWidgets.QVBoxLayout(self.container)
        root.setContentsMargins(1, 1, 1, 1)
        self._build_title_bar(root)

        content = QtWidgets.QVBoxLayout()
        content.setContentsMargins(20, 5, 20, 20)
        root.addLayout(content)
        self._build_content(content)

        self._status_signal.connect(self._on_status)
        self._apply_result_signal.connect(self._on_apply_result)

        threading.Thread(target=self.service.fetch_offsets, daemon=True).start()
        self.service.start_monitor(
            status_callback=self._status_signal.emit,
            apply_result_callback=self._apply_result_signal.emit,
        )

    def _build_title_bar(self, parent_layout: QtWidgets.QVBoxLayout):
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.addWidget(QtWidgets.QLabel("simple-injector"))
        lay.addStretch()

        _btn_style = (
            "QPushButton { background: transparent; border: none; font-size: 14px; }"
            "QPushButton:hover { color: #FF3333; }"
        )
        for text, slot in (("-", self.showMinimized), ("✕", self.close)):
            btn = QtWidgets.QPushButton(text)
            btn.setFixedSize(30, 30)
            btn.setStyleSheet(_btn_style)
            btn.clicked.connect(slot)
            lay.addWidget(btn)

        parent_layout.addWidget(bar)

    def _build_content(self, lay: QtWidgets.QVBoxLayout):
        self.status_lbl = QtWidgets.QLabel("Status: waiting...")
        self.status_lbl.setStyleSheet(f"color: #FF4444; {_STATUS_STYLE}")
        lay.addWidget(self.status_lbl)

        self.count_lbl = QtWidgets.QLabel(f"Modified FFlags: {len(self.added_flags)}")
        self.count_lbl.setStyleSheet(f"color: #666; {_LABEL_STYLE}")
        lay.addWidget(self.count_lbl)

        btn_row = QtWidgets.QHBoxLayout()
        self.add_btn = _make_btn("Add FFlag")
        self.remove_btn = _make_btn("Remove")
        self.export_btn = _make_btn("Export")
        for btn in (self.add_btn, self.remove_btn, self.export_btn):
            btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self.search_bar = QtWidgets.QLineEdit()
        self.search_bar.setPlaceholderText("Search FFlags...")
        self.search_bar.textChanged.connect(self._refresh_table)
        lay.addWidget(self.search_bar)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Value", "Status"])
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table)

        self.apply_summary_lbl = QtWidgets.QLabel("")
        self.apply_summary_lbl.setStyleSheet(f"color: #999; {_LABEL_STYLE}")
        lay.addWidget(
            self.apply_summary_lbl, alignment=QtCore.Qt.AlignmentFlag.AlignRight
        )

        self.apply_all_btn = QtWidgets.QPushButton("Apply All")
        self.apply_all_btn.setObjectName("ApplyBtn")
        self.apply_all_btn.setFixedSize(140, 30)
        self.apply_all_btn.setToolTip("Apply all flags when Roblox is connected")
        self.apply_all_btn.setEnabled(False)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(self.apply_all_btn)
        lay.addLayout(bottom)

        self.add_btn.clicked.connect(self._show_add_dialog)
        self.remove_btn.clicked.connect(self._remove_selected)
        self.export_btn.clicked.connect(self._export_to_file)
        self.apply_all_btn.clicked.connect(self._run_apply_all)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+C"), self).activated.connect(
            self._copy_to_clipboard
        )

        self._refresh_table()

    def _update_apply_btn(self):
        self.apply_all_btn.setEnabled(
            self.service.is_connected and bool(self.added_flags)
        )

    def _on_status(self, connected: bool):
        if connected:
            self.status_lbl.setText("Status: connected")
            self.status_lbl.setStyleSheet(f"color: #44FF44; {_STATUS_STYLE}")
        else:
            self.status_lbl.setText("Status: waiting for Roblox...")
            self.status_lbl.setStyleSheet(f"color: #FF4444; {_STATUS_STYLE}")
        self._update_apply_btn()

    def _on_apply_result(self, status_map: dict):
        self.apply_all_btn.setText("Apply All")
        self._update_apply_btn()

        self.flag_statuses = {k: bool(v) for k, v in status_map.items()}
        success = sum(self.flag_statuses.values())
        fail = len(status_map) - success

        color = "#44FF44" if fail == 0 else "#FF4444"
        self.apply_summary_lbl.setText(
            f"Apply result: {success} success, {fail} fail"
        )
        self.apply_summary_lbl.setStyleSheet(f"color: {color}; {_LABEL_STYLE}")
        self._refresh_table()

    def _refresh_table(self):
        self.table.setRowCount(0)
        search = self.search_bar.text().lower()

        _status_cfg = {
            True:  ("Success", QtGui.QColor("#44FF44")),
            False: ("Fail",    QtGui.QColor("#FF4444")),
            None:  ("Pending", QtGui.QColor("#999999")),
        }

        for name, value in self.added_flags.items():
            if search and search not in name.lower():
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))

            status_key = self.flag_statuses.get(name)
            text, color = _status_cfg[status_key]
            item = QtWidgets.QTableWidgetItem(text)
            item.setForeground(QtGui.QBrush(color))
            self.table.setItem(row, 2, item)

        self.count_lbl.setText(f"Modified FFlags: {len(self.added_flags)}")
        self._update_apply_btn()

    @staticmethod
    def _normalize_json(text: str) -> str:
        return re.sub(r",\s*([\]}])", r"\1", text.strip())

    def _show_add_dialog(self):
        dialog = AddFlagDialog(self)
        if not dialog.exec():
            return

        text = dialog.json_input.toPlainText().strip()
        if not text:
            QtWidgets.QMessageBox.warning(self, "Empty Input", "No JSON provided.")
            return

        try:
            normalized = self._normalize_json(text)
            try:
                data = json.loads(normalized)
            except json.JSONDecodeError:
                data = ast.literal_eval(normalized)

            if not isinstance(data, dict):
                raise ValueError("JSON must be a key/value object.")

            for key, value in data.items():
                self.added_flags[key] = str(value)
            self._refresh_table()
            self.service.save_data()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid JSON", f"Cannot add flags:\n{exc}")

    def _remove_selected(self):
        rows = {
            row
            for sel in self.table.selectedRanges()
            for row in range(sel.topRow(), sel.bottomRow() + 1)
        }
        for row in sorted(rows, reverse=True):
            item = self.table.item(row, 0)
            if item:
                self.added_flags.pop(item.text(), None)
        self._refresh_table()
        self.service.save_data()

    def _run_apply_all(self):
        self.apply_all_btn.setEnabled(False)
        self.apply_all_btn.setText("Applying...")
        self.apply_summary_lbl.setText("")
        self.service.run_apply_all()

    def _copy_to_clipboard(self):
        to_copy = {
            item.text(): self.added_flags[item.text()]
            for sel in self.table.selectedRanges()
            for row in range(sel.topRow(), sel.bottomRow() + 1)
            if (item := self.table.item(row, 0)) and item.text() in self.added_flags
        }
        if to_copy:
            QtWidgets.QApplication.clipboard().setText(
                json.dumps(to_copy, indent=4)
            )

    def _export_to_file(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export", "fflags.json", "JSON Files (*.json)"
        )
        if path:
            self.service.export_to_file(Path(path))

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if hasattr(self, "_drag_pos"):
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.__dict__.pop("_drag_pos", None)

    def closeEvent(self, event):
        try:
            self.service.stop_monitor()
        except Exception:
            pass
        super().closeEvent(event)
