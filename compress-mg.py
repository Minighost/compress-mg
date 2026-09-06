import logging
import os
import sys
from dataclasses import dataclass
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


@dataclass
class Job:
    """A queued file, identified by job_id rather than by its path.

    Do not go back to keying rows by path. A path is not unique to a row -- the same file can
    occupy two rows (re-queue a finished file to redo it at different settings), and the old
    path-keyed dict silently collapsed those to one entry. That caused three separate bugs:

    - every status/progress update landed on whichever row was added last, so the other row sat
      frozen at "Queued" forever even though its job had actually run;
    - the Clear and Remove guards compared the row's path against the running job's path, so a
      *finished* row matched the running job and could never be removed -- this is the reported
      "Clear does not clear Done items";
    - re-queuing one of two identical rows silently did nothing, because the path was already
      in the pending list.

    job_ids come from a counter that only ever increases, so a removed row's id is never reused
    and a late signal can never be misrouted to a newer job.
    """

    job_id: int
    path: str
    # The name cell doubles as the row handle: table.row(item) always reports this job's current
    # row, and Qt keeps it correct as rows above are inserted or removed. That is what replaced
    # the old path -> row-index dict, which went stale on every removal and had to be rebuilt
    # by hand; there is deliberately no such cache to keep in sync any more.
    item: QTableWidgetItem


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
        self.thread_pool.setMaxThreadCount(1)  # one ffmpeg encode at a time

        # Everything below tracks jobs by id, never by path -- see the Job docstring for what
        # breaks otherwise. current_job_id in particular must stay an id: as a path it matched
        # every row holding that path, which is what made finished rows un-clearable.
        self.jobs: dict[int, Job] = {}
        self.queue: list[int] = []  # pending job ids, in order
        self.next_job_id = 0
        self.busy = False
        self._columns_sized = False

        self.running = False
        self.stop_requested = False
        self.current_cancel_token: CancelToken | None = None
        self.current_job_id: int | None = None

        # Workers are kept alive here for the reason spelled out in _process_next. Do not
        # replace this with a local variable, and do not "clean up" this list.
        self.workers: list[CompressWorker] = []

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
        # Drop files that are already waiting or encoding. Queueing the same file twice only
        # ever wasted an encode: both runs write the same <base>_compressed<ext> output, so the
        # second silently overwrote the first.
        #
        # The check is scoped to pending/running on purpose -- do not widen it to every row in
        # the table. A *finished* row keeps its path, and re-adding a file you already
        # compressed (to redo it at a different size or framerate) has to keep working; matching
        # against finished rows would block that. Re-running does overwrite the earlier output,
        # which is the intent when you deliberately ask for it.
        pending = {self.jobs[job_id].path for job_id in self.queue}
        if self.current_job_id is not None:
            pending.add(self.jobs[self.current_job_id].path)
        new_paths = []
        for path in video_paths:
            if path in pending:
                skipped += 1
            else:
                pending.add(path)
                new_paths.append(path)
        if skipped:
            log.info("Ignored %d non-video or already-queued file(s)", skipped)
        for path in new_paths:
            self._enqueue(path)
        log.info("Added %d file(s) to queue", len(new_paths))
        self._process_next()
        self._update_start_stop_button()

    # ---------- queue processing ----------

    def _enqueue(self, path: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(os.path.basename(path))
        self.table.setItem(row, COL_NAME, name_item)
        self.table.setItem(row, COL_STATUS, QTableWidgetItem("Queued"))
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        self.table.setCellWidget(row, COL_PROGRESS, progress_bar)

        job_id = self.next_job_id
        self.next_job_id += 1
        name_item.setData(Qt.ItemDataRole.UserRole, job_id)
        self.jobs[job_id] = Job(job_id, path, name_item)
        self.queue.append(job_id)

    def _job_id_at(self, row: int) -> int | None:
        item = self.table.item(row, COL_NAME)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _row_of(self, job_id: int) -> int:
        """Live row index for a job, or -1 if its row is gone.

        Every caller must handle -1 instead of assuming the row exists. A worker signal is
        delivered asynchronously and can land after its row was removed (cleared or deleted from
        the context menu mid-encode); the old code indexed a dict directly and would have raised
        KeyError there.
        """
        job = self.jobs.get(job_id)
        return -1 if job is None else self.table.row(job.item)

    def _process_next(self):
        if self.busy or not self.running:
            return
        if not self.queue:
            self.running = False
            self._update_start_stop_button()
            return
        self.busy = True
        job_id = self.queue.pop(0)
        input_path = self.jobs[job_id].path
        self.current_job_id = job_id
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
        worker.signals.status.connect(lambda msg, j=job_id: self._set_status(j, msg))
        worker.signals.progress.connect(
            lambda pct, j=job_id: self._set_progress(j, pct)
        )
        worker.signals.finished.connect(lambda out, j=job_id: self._on_finished(j, out))
        worker.signals.error.connect(lambda msg, j=job_id: self._on_error(j, msg))
        worker.signals.cancelled.connect(lambda j=job_id: self._on_cancelled(j))

        # Both lines below are load-bearing; removing either one breaks the app badly.
        #
        # QThreadPool destroys an auto-delete runnable the moment run() returns, which also
        # destroys the worker's WorkerSignals object. That object is the *sender* of the
        # cross-thread queued finished/error/cancelled calls, and Qt discards queued calls whose
        # sender died before the main thread dispatched them. Those signals are emitted on the
        # last line of run(), so they race the worker's own destruction and lose often.
        #
        # Losing one means _advance_or_halt never runs: busy/running stay True, current_job_id
        # is never cleared, the queue silently stalls, and Clear then refuses to remove that row
        # forever (it looks like the current job). The same use-after-free segfaults the process
        # outright. Measured on a 6-file queue before this fix: only 1 run in 9 completed
        # cleanly -- 6 segfaulted and 2 wedged. With it, 6 of 6 were clean.
        worker.setAutoDelete(False)  # ownership becomes Python's; C++ must not free it
        self.workers.append(
            worker
        )  # keep a strong reference (see _clear_queue for pruning)
        self.thread_pool.start(worker)

    def _job_name(self, job_id: int) -> str:
        job = self.jobs.get(job_id)
        return os.path.basename(job.path) if job else "<removed>"

    def _set_status(self, job_id: int, msg: str):
        log.info("%s: %s", self._job_name(job_id), msg)
        row = self._row_of(job_id)
        if row >= 0:
            self.table.item(row, COL_STATUS).setText(msg)

    def _set_progress(self, job_id: int, pct: float):
        row = self._row_of(job_id)
        if row >= 0:
            self.table.cellWidget(row, COL_PROGRESS).setValue(int(pct))

    def _on_finished(self, job_id: int, output_path: str):
        log.info("Done: %s -> %s", self._job_name(job_id), output_path)
        row = self._row_of(job_id)
        if row >= 0:
            self.table.item(row, COL_STATUS).setText("Done")
            self.table.cellWidget(row, COL_PROGRESS).setValue(100)
        self._advance_or_halt()

    def _on_error(self, job_id: int, message: str):
        log.error("Failed: %s (%s)", self._job_name(job_id), message)
        row = self._row_of(job_id)
        if row >= 0:
            self.table.item(row, COL_STATUS).setText(f"Error: {message}")
        self._advance_or_halt()

    def _on_cancelled(self, job_id: int):
        log.info("Stopped: %s", self._job_name(job_id))
        row = self._row_of(job_id)
        if row >= 0:
            self.table.item(row, COL_STATUS).setText("Stopped")
        self._advance_or_halt()

    def _advance_or_halt(self):
        self.busy = False
        self.current_cancel_token = None
        self.current_job_id = None
        # The finished worker is deliberately left in self.workers. Releasing it here would drop
        # the last reference while its thread may still be returning from run(), which is the
        # same crash this held reference exists to prevent. _clear_queue prunes it when idle.
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
            job_id = self._job_id_at(row)
            # Skip only the one row that is encoding right now. This compares ids, not paths:
            # the path form matched any row sharing that path, which is precisely why finished
            # rows used to survive Clear.
            if job_id is not None and job_id == self.current_job_id:
                continue
            self.table.removeRow(row)
            self.jobs.pop(job_id, None)
        self.queue.clear()
        if not self.busy:
            # Safe only while idle: with nothing running, every retained worker's run() has
            # returned and the pool has finished with it, so dropping these cannot race.
            self.workers.clear()
        self._update_start_stop_button()

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
            job_id = self._job_id_at(row)
            if job_id is None or job_id == self.current_job_id:
                continue
            self.table.item(row, COL_STATUS).setText("Queued")
            bar = self.table.cellWidget(row, COL_PROGRESS)
            if bar is not None:
                bar.setValue(0)
            # Membership test on the id, not the path: two rows can share a path, and testing
            # the path made re-queuing the second one silently do nothing.
            if job_id not in self.queue:
                self.queue.append(job_id)
        self._update_start_stop_button()
        self._process_next()

    def _remove_rows(self, rows: list[int]):
        log.info("Removing %d row(s)", len(rows))
        for row in sorted(rows, reverse=True):
            job_id = self._job_id_at(row)
            if job_id is not None and job_id == self.current_job_id:
                continue
            if job_id in self.queue:
                self.queue.remove(job_id)
            self.table.removeRow(row)
            self.jobs.pop(job_id, None)
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
