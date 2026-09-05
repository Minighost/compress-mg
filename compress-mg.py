import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from compressor import CancelToken, CompressionCancelled, CompressionError, compress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("compress-mg")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

SETTINGS_FILE = Path(__file__).resolve().parent / "settings.ini"

COL_NAME = 0
COL_STATUS = 1
COL_PROGRESS = 2


class WorkerSignals(QObject):
    progress = Signal(float)
    status = Signal(str)
    finished = Signal(str)  # output path
    error = Signal(str)
    cancelled = Signal()


class CompressWorker(QRunnable):
    def __init__(
        self,
        input_path: str,
        output_path: str,
        settings: dict,
        cancel_token: CancelToken,
    ):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.settings = settings
        self.cancel_token = cancel_token
        self.signals = WorkerSignals()

    def run(self):
        log.info("Starting compression: %s", self.input_path)
        try:
            compress(
                self.input_path,
                self.output_path,
                self.settings["target_mb"],
                self.settings["margin"],
                self.settings["max_height"],
                self.settings["framerate"],
                self.settings["merge_audio"],
                self.settings["normalize_audio"],
                on_progress=self.signals.progress.emit,
                on_status=self.signals.status.emit,
                cancel_token=self.cancel_token,
            )
        except CompressionCancelled:
            log.info("Cancelled: %s", self.input_path)
            self.signals.cancelled.emit()
            return
        except CompressionError as e:
            log.error("Compression error on %s: %s", self.input_path, e)
            self.signals.error.emit(str(e))
            return
        except FileNotFoundError as e:
            log.error("Required tool not found on PATH: %s", e.filename)
            self.signals.error.emit(f"Required tool not found on PATH: {e.filename}")
            return
        except Exception as e:  # subprocess failures, unexpected errors
            log.error("Unexpected error on %s: %s", self.input_path, e)
            self.signals.error.emit(str(e))
            return
        log.info("Finished compression: %s -> %s", self.input_path, self.output_path)
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

        self.running = False
        self.stop_requested = False
        self.current_cancel_token: CancelToken | None = None
        self.current_path: str | None = None

        self._build_ui()
        self._load_settings()
        log.info("Main window initialized")

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

        self.start_stop_button = QPushButton("Start")
        self.start_stop_button.setEnabled(False)
        self.start_stop_button.clicked.connect(self._toggle_start_stop)
        header_layout.addWidget(self.start_stop_button)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_queue)
        header_layout.addWidget(clear_button)

        master_layout.addLayout(header_layout)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Progress"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        master_layout.addWidget(self.table)

        settings_box = QGroupBox("Settings")
        settings_layout = QVBoxLayout(settings_box)

        # settings row 0
        settings_row_layout0 = QHBoxLayout()
        settings_row_layout0.addWidget(QLabel("Target size (MB):"))
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.5, 1000.0)
        self.size_spin.setValue(7.0)
        settings_row_layout0.addWidget(self.size_spin)

        settings_row_layout0.addWidget(QLabel("Margin:"))
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.5, 1.0)
        self.margin_spin.setSingleStep(0.01)
        self.margin_spin.setValue(0.99)
        settings_row_layout0.addWidget(self.margin_spin)

        settings_row_layout0.addWidget(QLabel("Max height (0 = no cap):"))
        self.max_height_spin = QSpinBox()
        self.max_height_spin.setRange(0, 8192)
        self.max_height_spin.setValue(720)
        settings_row_layout0.addWidget(self.max_height_spin)

        # settings row 1
        settings_row_layout1 = QHBoxLayout()
        settings_row_layout1.addWidget(QLabel("Framerate:"))
        self.framerate_combo = QComboBox()
        self.framerate_combo.setEditable(True)
        self.framerate_combo.addItems(
            ["Same as source", "60", "50", "30", "25", "24", "23.976"]
        )
        self.framerate_combo.setCurrentText("Same as source")
        settings_row_layout1.addWidget(self.framerate_combo)

        self.merge_audio_check = QCheckBox("Merge audio tracks")
        self.merge_audio_check.toggled.connect(self._on_merge_audio_toggled)
        settings_row_layout1.addWidget(self.merge_audio_check)

        self.normalize_audio_check = QCheckBox("Normalize audio")
        self.normalize_audio_check.setEnabled(False)
        settings_row_layout1.addWidget(self.normalize_audio_check)

        # compile settings rows
        settings_layout.addLayout(settings_row_layout0)
        settings_layout.setAlignment(settings_row_layout0, Qt.AlignLeft)
        settings_layout.addLayout(settings_row_layout1)
        settings_layout.setAlignment(settings_row_layout1, Qt.AlignLeft)

        master_layout.addWidget(settings_box)

        output_layout = QHBoxLayout()
        browse_button = QPushButton("Change")
        browse_button.clicked.connect(self._choose_output_dir)
        output_layout.addWidget(browse_button)
        open_button = QPushButton("Open")
        open_button.clicked.connect(self._open_output_dir)
        output_layout.addWidget(open_button)
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
        self.max_height_spin.setValue(int(self.settings.value("max_height", 720)))
        self.framerate_combo.setCurrentText(
            str(self.settings.value("framerate", "Same as source"))
        )
        self.merge_audio_check.setChecked(
            self.settings.value("merge_audio", False, type=bool)
        )
        self.normalize_audio_check.setChecked(
            self.settings.value("normalize_audio", False, type=bool)
        )
        log.info("Loaded settings from %s", SETTINGS_FILE)

    def _save_settings(self):
        self.settings.setValue("output_dir", self.output_dir)
        self.settings.setValue("target_mb", self.size_spin.value())
        self.settings.setValue("margin", self.margin_spin.value())
        self.settings.setValue("max_height", self.max_height_spin.value())
        self.settings.setValue("framerate", self.framerate_combo.currentText())
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

    def _open_output_dir(self):
        os.makedirs(self.output_dir, exist_ok=True)
        os.startfile(self.output_dir)

    def closeEvent(self, event):
        log.info("Closing main window, shutting down")
        if self.current_cancel_token is not None:
            self.current_cancel_token.cancel()
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
        skipped = len(paths) - len(video_paths)
        if skipped:
            log.info("Ignored %d non-video file(s)", skipped)
        for path in video_paths:
            self._enqueue(path)
        log.info("Added %d file(s) to queue", len(video_paths))
        self._process_next()
        self._update_start_stop_button()

    # ---------- queue processing ----------

    def _enqueue(self, path: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(os.path.basename(path))
        name_item.setData(Qt.ItemDataRole.UserRole, path)
        self.table.setItem(row, COL_NAME, name_item)
        self.table.setItem(row, COL_STATUS, QTableWidgetItem("Queued"))
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        self.table.setCellWidget(row, COL_PROGRESS, progress_bar)
        self.rows[path] = row
        self.queue.append(path)

    def _process_next(self):
        if self.busy or not self.running:
            return
        if not self.queue:
            self.running = False
            self._update_start_stop_button()
            return
        self.busy = True
        input_path = self.queue.pop(0)
        self.current_path = input_path
        log.info(
            "Dequeued next job: %s (%d remaining in queue)", input_path, len(self.queue)
        )

        os.makedirs(self.output_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(input_path))
        output_path = os.path.join(self.output_dir, f"{base}_compressed{ext}")

        framerate_text = self.framerate_combo.currentText().strip()
        job_settings = {
            "target_mb": self.size_spin.value(),
            "margin": self.margin_spin.value(),
            "max_height": self.max_height_spin.value() or None,
            "framerate": (
                None if framerate_text == "Same as source" else framerate_text
            ),
            "merge_audio": self.merge_audio_check.isChecked(),
            "normalize_audio": self.normalize_audio_check.isChecked(),
        }

        cancel_token = CancelToken()
        self.current_cancel_token = cancel_token
        worker = CompressWorker(input_path, output_path, job_settings, cancel_token)
        worker.signals.status.connect(
            lambda msg, p=input_path: self._set_status(p, msg)
        )
        worker.signals.progress.connect(
            lambda pct, p=input_path: self._set_progress(p, pct)
        )
        worker.signals.finished.connect(
            lambda out, p=input_path: self._on_finished(p, out)
        )
        worker.signals.error.connect(lambda msg, p=input_path: self._on_error(p, msg))
        worker.signals.cancelled.connect(lambda p=input_path: self._on_cancelled(p))
        self.thread_pool.start(worker)

    def _set_status(self, path: str, msg: str):
        log.info("%s: %s", os.path.basename(path), msg)
        self.table.item(self.rows[path], COL_STATUS).setText(msg)

    def _set_progress(self, path: str, pct: float):
        bar = self.table.cellWidget(self.rows[path], COL_PROGRESS)
        bar.setValue(int(pct))

    def _on_finished(self, path: str, output_path: str):
        log.info("Done: %s -> %s", os.path.basename(path), output_path)
        row = self.rows[path]
        self.table.item(row, COL_STATUS).setText("Done")
        self.table.cellWidget(row, COL_PROGRESS).setValue(100)
        self._advance_or_halt()

    def _on_error(self, path: str, message: str):
        log.error("Failed: %s (%s)", os.path.basename(path), message)
        self.table.item(self.rows[path], COL_STATUS).setText(f"Error: {message}")
        self._advance_or_halt()

    def _on_cancelled(self, path: str):
        log.info("Stopped: %s", os.path.basename(path))
        self.table.item(self.rows[path], COL_STATUS).setText("Stopped")
        self._advance_or_halt()

    def _advance_or_halt(self):
        self.busy = False
        self.current_cancel_token = None
        self.current_path = None
        if self.stop_requested:
            self.stop_requested = False
            self.running = False
            self._update_start_stop_button()
            return
        if self.running:
            self._process_next()
        else:
            self._update_start_stop_button()

    # ---------- start / stop / clear ----------

    def _toggle_start_stop(self):
        if self.running:
            self._stop_queue()
        else:
            self._start_queue()

    def _start_queue(self):
        if not self.queue or self.running:
            return
        log.info("Queue started (%d file(s) pending)", len(self.queue))
        self.running = True
        self._update_start_stop_button()
        self._process_next()

    def _stop_queue(self):
        if not self.running or self.stop_requested:
            return
        log.info("Stop requested, cancelling current job and halting queue")
        self.stop_requested = True
        self._update_start_stop_button()
        if self.current_cancel_token is not None:
            self.current_cancel_token.cancel()

    def _update_start_stop_button(self):
        if self.stop_requested:
            self.start_stop_button.setText("Stopping...")
            self.start_stop_button.setEnabled(False)
        elif self.running:
            self.start_stop_button.setText("Stop")
            self.start_stop_button.setEnabled(True)
        else:
            self.start_stop_button.setText("Start")
            self.start_stop_button.setEnabled(bool(self.queue))

    def _clear_queue(self):
        log.info("Clearing queue")
        for row in reversed(range(self.table.rowCount())):
            path = self.table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
            if path == self.current_path and path is not None:
                continue
            self.table.removeRow(row)
        self.queue.clear()
        self._rebuild_row_index()
        self._update_start_stop_button()

    def _rebuild_row_index(self):
        self.rows = {
            self.table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole): row
            for row in range(self.table.rowCount())
        }

    # ---------- table context menu ----------

    def _show_table_context_menu(self, pos):
        selected_rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not selected_rows:
            return
        menu = QMenu(self)
        requeue_action = menu.addAction("Set to Queued")
        remove_action = menu.addAction("Remove")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == requeue_action:
            self._requeue_rows(selected_rows)
        elif chosen == remove_action:
            self._remove_rows(selected_rows)

    def _requeue_rows(self, rows: list[int]):
        log.info("Re-queuing %d row(s)", len(rows))
        for row in rows:
            path = self.table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
            if path is None or path == self.current_path:
                continue
            self.table.item(row, COL_STATUS).setText("Queued")
            bar = self.table.cellWidget(row, COL_PROGRESS)
            if bar is not None:
                bar.setValue(0)
            if path not in self.queue:
                self.queue.append(path)
        self._update_start_stop_button()
        self._process_next()

    def _remove_rows(self, rows: list[int]):
        log.info("Removing %d row(s)", len(rows))
        for row in sorted(rows, reverse=True):
            path = self.table.item(row, COL_NAME).data(Qt.ItemDataRole.UserRole)
            if path == self.current_path and path is not None:
                continue
            if path in self.queue:
                self.queue.remove(path)
            self.table.removeRow(row)
        self._rebuild_row_index()
        self._update_start_stop_button()


def main():
    log.info("Starting compress-mg...")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    log.info("=" * 60)
    log.info("compress-mg depends on this console window, do not close me!")
    log.info("=" * 60)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
