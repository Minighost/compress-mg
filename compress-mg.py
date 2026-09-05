#!/usr/bin/env python3
"""
Drag-and-drop Qt6 GUI for compress_to_size's compression logic.

Usage:
    python compress_gui.py
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from compressor import CompressionError, compress

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

SETTINGS_FILE = Path(__file__).resolve().parent / "settings.ini"

COL_NAME, COL_STATUS, COL_PROGRESS = range(3)


class WorkerSignals(QObject):
    progress = Signal(float)
    status = Signal(str)
    finished = Signal(str)  # output path
    error = Signal(str)


class CompressWorker(QRunnable):
    def __init__(self, input_path: str, output_path: str, settings: dict):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.settings = settings
        self.signals = WorkerSignals()

    def run(self):
        try:
            compress(
                self.input_path,
                self.output_path,
                self.settings["target_mb"],
                self.settings["margin"],
                self.settings["max_height"],
                self.settings["merge_audio"],
                self.settings["normalize_audio"],
                on_progress=self.signals.progress.emit,
                on_status=self.signals.status.emit,
            )
        except CompressionError as e:
            self.signals.error.emit(str(e))
            return
        except FileNotFoundError as e:
            self.signals.error.emit(f"Required tool not found on PATH: {e.filename}")
            return
        except Exception as e:  # subprocess failures, unexpected errors
            self.signals.error.emit(str(e))
            return
        self.signals.finished.emit(self.output_path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("compress-mg")
        self.setAcceptDrops(True)
        self.resize(640, 480)

        self.settings = QSettings(str(SETTINGS_FILE), QSettings.IniFormat)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(1)  # one HandBrake encode at a time

        self.queue: list[str] = []
        self.busy = False
        self.rows: dict[str, int] = {}
        self._columns_sized = False

        self._build_ui()
        self._load_settings()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._columns_sized:
            self._size_columns()
            self._columns_sized = True

    def _size_columns(self):
        total = self.table.viewport().width()
        self.table.setColumnWidth(COL_NAME, int(total * 0.8))
        self.table.setColumnWidth(COL_STATUS, int(total * 0.1))

    # ---------- UI construction ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        master_layout = QVBoxLayout(central)

        header_layout = QHBoxLayout()
        add_video_button = QPushButton("+ Add Videos")
        add_video_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        add_video_button.clicked.connect(self._browse_for_files)
        header_layout.addWidget(add_video_button)
        drop_label = QLabel("...Or drag and drop video files on the window")
        drop_label.setStyleSheet("font-style: italic;")
        header_layout.addWidget(drop_label)
        header_layout.addStretch(1)

        master_layout.addLayout(header_layout)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Progress"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        master_layout.addWidget(self.table)

        settings_box = QGroupBox("Settings")
        settings_layout = QHBoxLayout(settings_box)

        settings_layout.addWidget(QLabel("Target size (MB):"))
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.5, 1000.0)
        self.size_spin.setValue(7.0)
        settings_layout.addWidget(self.size_spin)

        settings_layout.addWidget(QLabel("Margin:"))
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.5, 1.0)
        self.margin_spin.setSingleStep(0.01)
        self.margin_spin.setValue(0.99)
        settings_layout.addWidget(self.margin_spin)

        settings_layout.addWidget(QLabel("Max height (0 = no cap):"))
        self.max_height_spin = QSpinBox()
        self.max_height_spin.setRange(0, 8192)
        self.max_height_spin.setValue(0)
        settings_layout.addWidget(self.max_height_spin)

        self.merge_audio_check = QCheckBox("Merge audio tracks")
        self.merge_audio_check.toggled.connect(self._on_merge_audio_toggled)
        settings_layout.addWidget(self.merge_audio_check)

        self.normalize_audio_check = QCheckBox("Normalize audio")
        self.normalize_audio_check.setEnabled(False)
        settings_layout.addWidget(self.normalize_audio_check)

        master_layout.addWidget(settings_box)

        output_layout = QHBoxLayout()
        browse_button = QPushButton("Change output folder")
        browse_button.clicked.connect(self._choose_output_dir)
        output_layout.addWidget(browse_button)
        output_layout.addWidget(QLabel("Output folder:"))
        self.output_dir_label = QLabel()
        self.output_dir_label.setStyleSheet("font-style: italic;")
        output_layout.addWidget(self.output_dir_label, stretch=1)
        master_layout.addLayout(output_layout)

    def _on_merge_audio_toggled(self, checked: bool):
        self.normalize_audio_check.setEnabled(checked)
        if not checked:
            self.normalize_audio_check.setChecked(False)

    # ---------- settings persistence ----------

    def _load_settings(self):
        default_output = str(Path.home() / "Videos" / "Compressed")
        self.output_dir = self.settings.value("output_dir", default_output)
        self.output_dir_label.setText(self.output_dir)

        self.size_spin.setValue(float(self.settings.value("target_mb", 7.0)))
        self.margin_spin.setValue(float(self.settings.value("margin", 0.99)))
        self.max_height_spin.setValue(int(self.settings.value("max_height", 0)))
        self.merge_audio_check.setChecked(
            self.settings.value("merge_audio", False, type=bool)
        )
        self.normalize_audio_check.setChecked(
            self.settings.value("normalize_audio", False, type=bool)
        )

    def _save_settings(self):
        self.settings.setValue("output_dir", self.output_dir)
        self.settings.setValue("target_mb", self.size_spin.value())
        self.settings.setValue("margin", self.margin_spin.value())
        self.settings.setValue("max_height", self.max_height_spin.value())
        self.settings.setValue("merge_audio", self.merge_audio_check.isChecked())
        self.settings.setValue(
            "normalize_audio", self.normalize_audio_check.isChecked()
        )

    def _choose_output_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.output_dir
        )
        if chosen:
            self.output_dir = chosen
            self.output_dir_label.setText(chosen)
            self._save_settings()

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

    # ---------- drag and drop ----------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        self._add_paths(paths)

    # ---------- adding files ----------

    def _browse_for_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose videos to compress",
            "",
            "Video files (*" + " *".join(sorted(VIDEO_EXTENSIONS)) + ")",
        )
        self._add_paths(paths)

    def _add_paths(self, paths: list[str]):
        video_paths = [
            p for p in paths if os.path.splitext(p)[1].lower() in VIDEO_EXTENSIONS
        ]
        for path in video_paths:
            self._enqueue(path)
        self._process_next()

    # ---------- queue processing ----------

    def _enqueue(self, path: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, COL_NAME, QTableWidgetItem(os.path.basename(path)))
        self.table.setItem(row, COL_STATUS, QTableWidgetItem("Queued"))
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        self.table.setCellWidget(row, COL_PROGRESS, progress_bar)
        self.rows[path] = row
        self.queue.append(path)

    def _process_next(self):
        if self.busy or not self.queue:
            return
        self.busy = True
        input_path = self.queue.pop(0)

        os.makedirs(self.output_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(input_path))
        output_path = os.path.join(self.output_dir, f"{base}_compressed{ext}")

        job_settings = {
            "target_mb": self.size_spin.value(),
            "margin": self.margin_spin.value(),
            "max_height": self.max_height_spin.value() or None,
            "merge_audio": self.merge_audio_check.isChecked(),
            "normalize_audio": self.normalize_audio_check.isChecked(),
        }

        worker = CompressWorker(input_path, output_path, job_settings)
        row = self.rows[input_path]
        worker.signals.status.connect(lambda msg, r=row: self._set_status(r, msg))
        worker.signals.progress.connect(lambda pct, r=row: self._set_progress(r, pct))
        worker.signals.finished.connect(lambda out, r=row: self._on_finished(r, out))
        worker.signals.error.connect(lambda msg, r=row: self._on_error(r, msg))
        self.thread_pool.start(worker)

    def _set_status(self, row: int, msg: str):
        self.table.item(row, COL_STATUS).setText(msg)

    def _set_progress(self, row: int, pct: float):
        bar = self.table.cellWidget(row, COL_PROGRESS)
        bar.setValue(int(pct))

    def _on_finished(self, row: int, output_path: str):
        self.table.item(row, COL_STATUS).setText("Done")
        self.table.cellWidget(row, COL_PROGRESS).setValue(100)
        self.busy = False
        self._process_next()

    def _on_error(self, row: int, message: str):
        self.table.item(row, COL_STATUS).setText(f"Error: {message}")
        self.busy = False
        self._process_next()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
