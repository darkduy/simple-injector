import ast
import json
import re
import threading
from pathlib import Path

from PyQt6 import QtWidgets, QtCore, QtGui

from injector import InjectorService

STYLE = '''
QWidget#MainContainer {
    background-color: #0F0F11;
    border: 1px solid #222;
    border-radius: 12px;
}
QLabel { color: #E0E0E0; font-family: 'Segoe UI'; }
QLineEdit { background-color: #0A0A0A; border: 1px solid #2A2A2C; border-radius: 4px; color: #E0E0E0; padding: 6px; }
QPushButton { background-color: #1E1E21; border: 1px solid #2A2A2C; border-radius: 4px; color: #E0E0E0; font-weight: 500; }
QPushButton:hover { background-color: #3A0000; }
QPushButton#ApplyBtn { background-color: #1A0000; font-size: 12px; font-weight: bold; }
QTableWidget { background-color: #000000; border: none; gridline-color: #1A1A1D; outline: none; color: #BBB; }
QTableWidget::item:selected { background-color: #2A0000; color: #FF0000; }
QHeaderView::section { background-color: #0F0F11; color: #555; padding: 6px; border: none; font-weight: bold; font-size: 10px; }
QScrollBar:vertical { border: none; background: #0F0F11; width: 6px; }
QScrollBar::handle:vertical { background: #2A2A2C; min-height: 20px; border-radius: 3px; }
QCheckBox { color: #777; font-size: 11px; }
'''


class AddFlagDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Dialog)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(500, 420)

        layout = QtWidgets.QVBoxLayout(self)
        self.container = QtWidgets.QWidget()
        self.container.setObjectName('MainContainer')
        self.container.setStyleSheet(STYLE)
        layout.addWidget(self.container)

        c_layout = QtWidgets.QVBoxLayout(self.container)
        title = QtWidgets.QLabel('ADD FFLAGS')
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet('color: #FF3333; font-weight: bold; font-size: 16px; letter-spacing: 2px; border:none;')
        c_layout.addWidget(title)

        self.json_input = QtWidgets.QPlainTextEdit()
        self.json_input.setPlaceholderText('Paste JSON here...')
        self.json_input.setStyleSheet('background-color: #121214; border: 1px solid #220000; color: #888; font-family: \'Consolas\';')
        c_layout.addWidget(self.json_input)

        btn_lay = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton('ADD')
        self.cancel_btn = QtWidgets.QPushButton('CANCEL')
        self.add_btn.setFixedHeight(32)
        self.cancel_btn.setFixedHeight(32)
        btn_lay.addWidget(self.add_btn)
        btn_lay.addWidget(self.cancel_btn)
        c_layout.addLayout(btn_lay)

        self.import_btn = QtWidgets.QPushButton('IMPORT FROM FILES')
        self.import_btn.setFixedHeight(35)
        self.import_btn.clicked.connect(self.import_file)
        c_layout.addWidget(self.import_btn)

        self.add_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def import_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Select JSON', '', 'JSON Files (*.json)')
        if path:
            try:
                self.json_input.setPlainText(Path(path).read_text(encoding='utf-8'))
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, 'Import Error', f'Unable to read file:\n{e}')


class InjectorApp(QtWidgets.QMainWindow):
    status_signal = QtCore.pyqtSignal(bool)
    apply_result_signal = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(560, 680)

        self.service = InjectorService()
        self.added_flags = self.service.added_flags
        self.flag_statuses = {}

        self.container = QtWidgets.QWidget()
        self.container.setObjectName('MainContainer')
        self.container.setStyleSheet(STYLE)
        self.setCentralWidget(self.container)

        self.main_layout = QtWidgets.QVBoxLayout(self.container)
        self.main_layout.setContentsMargins(1, 1, 1, 1)
        self.setup_title_bar()

        self.content_layout = QtWidgets.QVBoxLayout()
        self.content_layout.setContentsMargins(20, 5, 20, 20)
        self.main_layout.addLayout(self.content_layout)

        self.init_ui()
        threading.Thread(target=self.service.fetch_offsets, daemon=True).start()

        self.status_signal.connect(self.update_ui_status)
        self.apply_result_signal.connect(self.handle_apply_result)
        self.service.start_monitor(
            status_callback=self.status_signal.emit,
            apply_result_callback=self.apply_result_signal.emit,
        )

    def update_ui_status(self, connected: bool):
        if connected:
            self.status_lbl.setText('Status: connected')
            self.status_lbl.setStyleSheet('color: #44FF44; font-weight: bold; font-size: 11px; border:none;')
        else:
            self.status_lbl.setText('Status: waiting for Roblox...')
            self.status_lbl.setStyleSheet('color: #FF4444; font-weight: bold; font-size: 11px; border:none;')
        self.update_apply_button_state()

    def update_apply_button_state(self):
        self.apply_all_btn.setEnabled(self.service.is_connected and bool(self.added_flags))

    def setup_title_bar(self):
        title_bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(title_bar)

        lbl = QtWidgets.QLabel('simple-injector')
        min_btn = QtWidgets.QPushButton('-')
        close_btn = QtWidgets.QPushButton('✕')

        for button in (min_btn, close_btn):
            button.setFixedSize(30, 30)
            button.setStyleSheet('QPushButton { background: transparent; border: none; font-size: 14px; } QPushButton:hover { color: #FF3333; }')

        close_btn.clicked.connect(self.close)
        min_btn.clicked.connect(self.showMinimized)

        lay.addWidget(lbl)
        lay.addStretch()
        lay.addWidget(min_btn)
        lay.addWidget(close_btn)
        self.main_layout.addWidget(title_bar)

    def init_ui(self):
        self.status_lbl = QtWidgets.QLabel('Status: waiting...')
        self.status_lbl.setStyleSheet('color: #FF4444; font-weight: bold; font-size: 11px; border:none;')
        self.content_layout.addWidget(self.status_lbl)

        self.count_lbl = QtWidgets.QLabel(f'Modified FFlags: {len(self.added_flags)}')
        self.count_lbl.setStyleSheet('color: #666; font-size: 10px; border:none;')
        self.content_layout.addWidget(self.count_lbl)

        btns = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton('Add FFlag')
        self.remove_btn = QtWidgets.QPushButton('Remove')
        self.export_btn = QtWidgets.QPushButton('Export')
        for button in (self.add_btn, self.remove_btn, self.export_btn):
            button.setFixedHeight(32)
            btns.addWidget(button)
        self.content_layout.addLayout(btns)

        self.search_bar = QtWidgets.QLineEdit()
        self.search_bar.setPlaceholderText('Search FFlags...')
        self.search_bar.textChanged.connect(self.refresh_table)
        self.content_layout.addWidget(self.search_bar)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Name', 'Value', 'Status'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.content_layout.addWidget(self.table)

        bottom = QtWidgets.QHBoxLayout()

        self.apply_all_btn = QtWidgets.QPushButton('Apply All')
        self.apply_all_btn.setObjectName('ApplyBtn')
        self.apply_all_btn.setFixedSize(140, 30)
        self.apply_all_btn.clicked.connect(self.run_apply_all)
        self.apply_all_btn.setToolTip('Apply all modified FFlags when Roblox is connected')
        self.apply_all_btn.setEnabled(False)

        self.apply_summary_lbl = QtWidgets.QLabel('')
        self.apply_summary_lbl.setStyleSheet('color: #999; font-size: 10px; border:none;')
        self.content_layout.addWidget(self.apply_summary_lbl, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        bottom.addStretch()
        bottom.addWidget(self.apply_all_btn)
        self.content_layout.addLayout(bottom)

        self.add_btn.clicked.connect(self.show_add_dialog)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.export_btn.clicked.connect(self.export_to_file)
        QtGui.QShortcut(QtGui.QKeySequence('Ctrl+C'), self).activated.connect(self.copy_to_clipboard)

        self.refresh_table()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if hasattr(self, 'drag_pos'):
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        if hasattr(self, 'drag_pos'):
            del self.drag_pos

    def refresh_table(self):
        self.table.setRowCount(0)
        search = self.search_bar.text().lower()
        for name, value in self.added_flags.items():
            if search in name.lower():
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
                self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))

                status = self.flag_statuses.get(name)
                if status is True:
                    status_text = 'Success'
                    color = QtGui.QColor('#44FF44')
                elif status is False:
                    status_text = 'Fail'
                    color = QtGui.QColor('#FF4444')
                else:
                    status_text = 'Pending'
                    color = QtGui.QColor('#999999')

                status_item = QtWidgets.QTableWidgetItem(status_text)
                status_item.setForeground(QtGui.QBrush(color))
                self.table.setItem(row, 2, status_item)
        self.count_lbl.setText(f'Modified FFlags: {len(self.added_flags)}')
        self.update_apply_button_state()

    def normalize_json_text(self, text: str) -> str:
        text = text.strip()
        # Allow trailing commas in objects and arrays
        text = re.sub(r',\s*([\]}])', r'\1', text)
        return text

    def show_add_dialog(self):
        dialog = AddFlagDialog(self)
        if dialog.exec():
            try:
                text = dialog.json_input.toPlainText().strip()
                if not text:
                    raise ValueError('No JSON input provided.')

                normalized_text = self.normalize_json_text(text)
                try:
                    data = json.loads(normalized_text)
                except json.JSONDecodeError:
                    data = ast.literal_eval(normalized_text)

                if not isinstance(data, dict):
                    raise ValueError('JSON must be an object with key/value pairs.')

                for key, value in data.items():
                    self.added_flags[key] = str(value)
                self.refresh_table()
                self.service.save_data()
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, 'Invalid JSON', f'Unable to add flags:\n{e}')
                return None

    def remove_selected(self):
        rows = set()
        for selection in self.table.selectedRanges():
            rows.update(range(selection.topRow(), selection.bottomRow() + 1))
        for row in sorted(rows, reverse=True):
            name_item = self.table.item(row, 0)
            if name_item:
                name = name_item.text()
                if name in self.added_flags:
                    del self.added_flags[name]
        self.refresh_table()
        self.service.save_data()

    def run_apply_all(self):
        self.apply_all_btn.setEnabled(False)
        self.apply_all_btn.setText('Applying...')
        self.apply_summary_lbl.setText('')
        self.service.run_apply_all()

    def handle_apply_result(self, status_map):
        self.apply_all_btn.setText('Apply All')
        self.update_apply_button_state()
        self.flag_statuses = {name: bool(status) for name, status in status_map.items()}
        success_count = sum(1 for status in self.flag_statuses.values() if status)
        total = len(status_map)
        fail_count = total - success_count
        if fail_count > 0:
            self.apply_summary_lbl.setText(f'Apply result: {success_count} success, {fail_count} fail')
            self.apply_summary_lbl.setStyleSheet('color: #FF4444; font-size: 10px; border:none;')
        else:
            self.apply_summary_lbl.setText(f'Apply result: {success_count} success, {fail_count} fail')
            self.apply_summary_lbl.setStyleSheet('color: #44FF44; font-size: 10px; border:none;')
        self.refresh_table()

    def closeEvent(self, event):
        try:
            self.service.stop_monitor()
        except Exception:
            pass
        super().closeEvent(event)

    def copy_to_clipboard(self):
        to_copy = {}
        for selection in self.table.selectedRanges():
            for row in range(selection.topRow(), selection.bottomRow() + 1):
                name_item = self.table.item(row, 0)
                if name_item:
                    name = name_item.text()
                    value = self.added_flags.get(name)
                    if value is not None:
                        to_copy[name] = value
        if to_copy:
            QtWidgets.QApplication.clipboard().setText(json.dumps(to_copy, indent=4))

    def export_to_file(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export', 'fflags.json', 'JSON Files (*.json)')
        if path:
            self.service.export_to_file(Path(path))
