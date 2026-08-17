"""Main application window -- owned by chunk 5 (design §C).

1400x860 (min 1100x700). Toolbar: Open SPD (Ctrl+O) / Rescan (F5) /
Auto-classify / Export DC SPD (Ctrl+E) / Save Config (Ctrl+S) / Load Config
(Ctrl+L) / Undo/Redo (Ctrl+Z/Y). `QTabWidget`: Net Manager | VRMs | Sinks.
Status bar: message label / counts label / `QProgressBar`.

Worker lifecycle follows the sibling `spd-model-injector` app's pattern
verbatim-in-spirit (see `old_repo_brief.md`): the `QThread`/worker pair is
kept as a strong attribute on `self` (`self._scan_worker`, `self._export_worker`)
because an unreferenced `QObject` worker can be garbage-collected by PySide
before -- or while -- the thread runs it; the slot that clears those refs once
the thread winds down guards with ``if self.sender() is self._scan_thread``
so a second scan started before the first's cleanup lands cannot be wiped out
from under it. Errors surfaced from a worker use ``QMessageBox.open()``
(non-blocking), not ``exec()``, since the calling slot runs off a worker
signal and a nested modal event loop there would freeze the app.

`closeEvent` never lets a running `QThread` be destroyed -- Qt aborts the
process (``QThread: Destroyed while thread is still running`` -> ``SIGABRT``)
when that happens. Both workers therefore own a `threading.Event` cancel hook
(`scan_spd`'s and `write_spd`'s `cancel` parameter); closing sets both, quits
and waits, and if a thread *still* has not wound down it ignores the close
event and re-issues it from the thread's own `finished` signal.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QToolBar,
)

from powerdc_setup_tool.core.model import ScanResult, ValidationIssue
from powerdc_setup_tool.core.session import Session
from powerdc_setup_tool.core.writer import WritePlan
from powerdc_setup_tool.ui.net_manager import NetManagerTab
from powerdc_setup_tool.ui.sink_tab import SinkTab
from powerdc_setup_tool.ui.vrm_tab import VrmTab
from powerdc_setup_tool.ui.workers import ExportWorker, ScanWorker

__all__ = ["MainWindow", "APP_TITLE"]

APP_TITLE = "SPD Manipulator for PowerDC"

#: How long `closeEvent` blocks on each worker thread before giving up on a
#: synchronous close and retrying from the thread's `finished` signal instead.
CLOSE_WAIT_MS = 5000


class MainWindow(QMainWindow):
    """Top-level window: toolbar, 3-tab `QTabWidget`, status bar, worker lifecycle."""

    def __init__(self, parent: QWidget | None = None, *, initial_path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 860)
        self.setMinimumSize(1100, 700)

        self.session = Session()
        self.spd_path: Path | None = None
        self._pending_spd_path: Path | None = None
        self._busy = False
        self._export_cancel_requested = False
        self._scan_cancel_requested = False
        self._closing = False

        # Worker/thread refs: kept strong on `self` (see module docstring).
        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        # Threads whose `finished` already re-triggers the deferred close.
        self._close_watched: list[QThread] = []

        self.undo_stack = QUndoStack(self)

        self.net_tab = NetManagerTab(self.session)
        self.vrm_tab = VrmTab(self.session)
        self.sink_tab = SinkTab(self.session)
        for tab in (self.net_tab, self.vrm_tab, self.sink_tab):
            # A shared stack, so Ctrl+Z/Y spans all three tabs (design §C).
            tab.view.undo_stack = self.undo_stack

        self.tabs = QTabWidget()
        self.tabs.addTab(self.net_tab, "Net Manager")
        self.tabs.addTab(self.vrm_tab, "VRMs")
        self.tabs.addTab(self.sink_tab, "Sinks")
        self.setCentralWidget(self.tabs)

        self._build_toolbar()
        self._build_status_bar()
        self._wire_cross_tab_navigation()
        self._wire_status_updates()
        self._update_status_counts()

        if initial_path is not None:
            self._load_spd(Path(initial_path))

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.open_action = QAction("Open SPD", self)
        self.open_action.setShortcut(QKeySequence("Ctrl+O"))
        self.open_action.setToolTip("Open a .spd file and scan it (Ctrl+O)")
        self.open_action.triggered.connect(self._open_spd)
        toolbar.addAction(self.open_action)

        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setShortcut(QKeySequence("F5"))
        self.rescan_action.setToolTip("Re-scan the current .spd file from disk (F5)")
        self.rescan_action.triggered.connect(self._rescan)
        toolbar.addAction(self.rescan_action)

        self.auto_classify_action = QAction("Auto-classify", self)
        self.auto_classify_action.setToolTip("Re-run automatic ground pairing for every power net")
        self.auto_classify_action.triggered.connect(self._auto_classify)
        toolbar.addAction(self.auto_classify_action)

        toolbar.addSeparator()

        self.export_action = QAction("Export DC SPD", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.setToolTip("Validate and export a PowerDC-ready .spd (Ctrl+E)")
        self.export_action.triggered.connect(self._export_dc_spd)
        toolbar.addAction(self.export_action)

        toolbar.addSeparator()

        self.save_config_action = QAction("Save Config", self)
        self.save_config_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_config_action.setToolTip("Save the current classification/config as JSON (Ctrl+S)")
        self.save_config_action.triggered.connect(self._save_config)
        toolbar.addAction(self.save_config_action)

        self.load_config_action = QAction("Load Config", self)
        self.load_config_action.setShortcut(QKeySequence("Ctrl+L"))
        self.load_config_action.setToolTip("Load a previously saved config JSON (Ctrl+L)")
        self.load_config_action.triggered.connect(self._load_config)
        toolbar.addAction(self.load_config_action)

        toolbar.addSeparator()

        self.undo_action = self.undo_stack.createUndoAction(self, "Undo")
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.redo_action = self.undo_stack.createRedoAction(self, "Redo")
        self.redo_action.setShortcut(QKeySequence("Ctrl+Y"))
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)

        self.status_label = QLabel("Open a .spd file to begin.")
        self.counts_label = QLabel("")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Cancel the running export")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_export)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)

        status.addWidget(self.status_label, 1)
        status.addWidget(self.counts_label)
        status.addPermanentWidget(self.cancel_button)
        status.addPermanentWidget(self.progress_bar)

    def _wire_cross_tab_navigation(self) -> None:
        # design §C context menu: "Go to net (jumps other tabs to same net)".
        self.net_tab.view.goToNetRequested.connect(self._go_to_net)
        self.vrm_tab.view.goToNetRequested.connect(self._go_to_net)
        self.sink_tab.view.goToNetRequested.connect(self._go_to_net)

    def _wire_status_updates(self) -> None:
        for tab in (self.net_tab, self.vrm_tab, self.sink_tab):
            tab.model.dataChanged.connect(self._on_model_changed)
            tab.model.modelReset.connect(self._on_model_changed)
        self.undo_stack.indexChanged.connect(self._on_model_changed)

    def _on_model_changed(self, *_args: object) -> None:
        self._update_status_counts()

    # ------------------------------------------------------------------ #
    # Busy state / thread lifecycle (old app pattern)
    # ------------------------------------------------------------------ #

    def _set_busy(self, busy: bool) -> None:
        """Disable toolbar actions while a worker is running."""
        self._busy = busy
        for action in (
            self.open_action,
            self.rescan_action,
            self.auto_classify_action,
            self.export_action,
            self.save_config_action,
            self.load_config_action,
        ):
            action.setEnabled(not busy)
        self.tabs.setEnabled(not busy)

    def _clear_scan_refs(self) -> None:
        # Guard on sender(): if a new scan started before this queued slot
        # ran, the refs already point at the new thread/worker.
        if self.sender() is self._scan_thread:
            self._scan_thread = None
            self._scan_worker = None

    def _clear_export_refs(self) -> None:
        if self.sender() is self._export_thread:
            self._export_thread = None
            self._export_worker = None

    def _cancel_running_workers(self) -> None:
        """Set both workers' cancel events, so a long run unwinds promptly.

        A scan of the real 1.4 GB input takes tens of seconds; without a cancel
        hook the `wait()` below would expire with the thread still inside
        `scan_spd`. The `*_cancel_requested` flags mark the resulting `failed`
        signal as "the user asked for this", which suppresses the error dialog.
        """
        for worker, flag in (
            (self._scan_worker, "_scan_cancel_requested"),
            (self._export_worker, "_export_cancel_requested"),
        ):
            if worker is None:
                continue
            setattr(self, flag, True)
            try:
                worker.cancel()
            except RuntimeError:
                # The worker's C++ side can already be torn down here: it is
                # deleted (via deleteLater, on thread-finish) as soon as the
                # worker thread's `finished` fires, which can race ahead of
                # the queued slot that clears the ref to None.
                pass

    def _running_worker_threads(self) -> list[QThread]:
        return [
            thread
            for thread in (self._scan_thread, self._export_thread)
            if thread is not None and thread.isRunning()
        ]

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel + quit + wait any running worker thread before accepting.

        Destroying a `QThread` that is still running is a fatal Qt error, so the
        close is *never* allowed through while one is alive: if the wait expires
        the event is ignored and re-issued from the thread's own `finished`
        signal (see the module docstring), leaving the window up but on its way
        out rather than aborting the process.
        """
        self._closing = True
        self._cancel_running_workers()
        for thread in self._running_worker_threads():
            thread.quit()
            thread.wait(CLOSE_WAIT_MS)

        pending = self._running_worker_threads()
        if pending:
            self.status_label.setText("Finishing background work before closing…")
            for thread in pending:
                if thread not in self._close_watched:
                    self._close_watched.append(thread)
                    thread.finished.connect(self._retry_close)
            event.ignore()
            return

        self._close_watched.clear()
        super().closeEvent(event)

    def _retry_close(self) -> None:
        """Re-issue the deferred close once the last worker thread has wound down."""
        if not self._closing or self._running_worker_threads():
            return
        self.close()

    # ------------------------------------------------------------------ #
    # Scan flow (Open SPD / Rescan)
    # ------------------------------------------------------------------ #

    def _open_spd(self) -> None:
        if self._busy:
            return
        start_dir = str(self.spd_path.parent) if self.spd_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SPD", start_dir, "SPD files (*.spd);;All files (*)"
        )
        if path:
            self._load_spd(Path(path))

    def _rescan(self) -> None:
        if self._busy:
            return
        if self.spd_path is None:
            QMessageBox.information(self, APP_TITLE, "Open a .spd file first.")
            return
        self._load_spd(self.spd_path)

    def _load_spd(self, path: Path) -> None:
        if self._busy:
            return
        self._scan_cancel_requested = False
        self._pending_spd_path = Path(path)
        self.status_label.setText(f"Scanning {self._pending_spd_path.name}…")
        self.progress_bar.setRange(0, 0)  # indeterminate until the first callback
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._set_busy(True)

        self._scan_thread = QThread(self)
        # Strong reference required: an unreferenced QObject worker can be
        # garbage-collected by PySide before/while the thread runs it.
        self._scan_worker = ScanWorker(self._pending_spd_path)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.failed.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._clear_scan_refs)
        self._scan_thread.start()

    def _on_scan_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.spd_path = self._pending_spd_path
        self._pending_spd_path = None
        self.session.load(result)
        self.session.autopair()
        # The stack's commands hold row/column keys into the *previous* session;
        # redoing one after a fresh scan would write a stale edit onto a net
        # that may not even exist any more (design §C: one shared stack).
        self.undo_stack.clear()
        self._refresh_all_models()
        self.progress_bar.setVisible(False)
        self._set_busy(False)
        self._update_status_counts()
        name = self.spd_path.name if self.spd_path else ""
        self.status_label.setText(f"Loaded {name} — {len(self.session.nets)} nets.")

    def _on_scan_failed(self, message: str) -> None:
        # The scan never landed: leave spd_path untouched (a Rescan keeps
        # pointing at the last file that loaded successfully).
        self._pending_spd_path = None
        self.progress_bar.setVisible(False)
        self._set_busy(False)
        cancelled = self._scan_cancel_requested
        self._scan_cancel_requested = False
        if cancelled:
            # `ScanCancelled` arrives on the failure path, but the user asked
            # for it (window close) -- no error dialog, mirroring the export.
            self.status_label.setText("Scan cancelled.")
            return
        self.status_label.setText("Scan failed.")
        self._show_error("Open SPD", message)

    def _auto_classify(self) -> None:
        if self._busy:
            return
        if self.session.scan is None:
            QMessageBox.information(self, APP_TITLE, "Open a .spd file first.")
            return
        self.session.autopair(force=True)
        self._refresh_all_models()
        self._update_status_counts()
        self.status_label.setText("Auto-classification re-applied.")

    def _refresh_all_models(self) -> None:
        for tab in (self.net_tab, self.vrm_tab, self.sink_tab):
            tab.model.refresh_from_session()

    # ------------------------------------------------------------------ #
    # Export flow (design §C)
    # ------------------------------------------------------------------ #

    def _export_dc_spd(self) -> None:
        """design §C export flow: validate -> file dialog -> options -> `ExportWorker`."""
        if self._busy:
            return
        if self.session.scan is None or self.spd_path is None:
            QMessageBox.information(self, APP_TITLE, "Open a .spd file first.")
            return

        issues = self.session.validate()
        if issues and not self._confirm_validation_issues(issues):
            self.status_label.setText("Export cancelled.")
            return

        default_path = self.spd_path.with_name(f"{self.spd_path.stem}_DC.spd")
        prompted = self._prompt_export_destination(default_path)
        if prompted is None:
            return
        output_path, options = prompted

        plan = self.session.build_plan(options)
        self._start_export(output_path, plan)

    def _confirm_validation_issues(self, issues: list[ValidationIssue]) -> bool:
        """design §C: one dialog for blocking issues + warnings, with a per-issue
        "Uncheck offending rows" action; returns whether to proceed with export."""
        current = list(issues)
        while True:
            blocking = [issue for issue in current if issue.blocking]
            box = QMessageBox(self)
            box.setWindowTitle("Export DC SPD")
            box.setIcon(QMessageBox.Icon.Critical if blocking else QMessageBox.Icon.Warning)
            box.setText(_format_issues(current))

            uncheck_button = None
            if any(issue.nets for issue in current):
                uncheck_button = box.addButton(
                    "Uncheck offending rows", QMessageBox.ButtonRole.ActionRole
                )
            if blocking:
                box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            else:
                box.addButton("Continue Export", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

            box.exec()
            clicked = box.clickedButton()

            if uncheck_button is not None and clicked is uncheck_button:
                self._uncheck_offending_rows(current)
                current = self.session.validate()
                if not current:
                    return True
                continue
            return box.buttonRole(clicked) == QMessageBox.ButtonRole.AcceptRole

    def _uncheck_offending_rows(self, issues: Iterable[ValidationIssue]) -> None:
        nets = {net for issue in issues if issue.blocking for net in issue.nets}
        for net in nets:
            if net in self.session.nets:
                self.session.set_selected(net, False)
        self._refresh_all_models()
        self._update_status_counts()

    def _prompt_export_destination(
        self, default_path: Path
    ) -> tuple[Path, dict[str, bool]] | None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export DC SPD", str(default_path), "SPD files (*.spd);;All files (*)"
        )
        if not path_str:
            return None
        output_path = Path(path_str)
        if self.spd_path is not None and _same_file(output_path, self.spd_path):
            QMessageBox.warning(
                self,
                "Export DC SPD",
                "The output path must differ from the source .spd file. "
                "Choose a different filename.",
            )
            return None

        dialog = _ExportOptionsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return output_path, dialog.options()

    def _start_export(self, output_path: Path, plan: WritePlan) -> None:
        assert self.session.scan is not None
        self._export_cancel_requested = False
        self.status_label.setText(f"Exporting to {output_path.name}…")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self._set_busy(True)

        self._export_thread = QThread(self)
        # Strong reference required: see `_load_spd`.
        self._export_worker = ExportWorker(self.session.scan, plan, output_path)
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.failed.connect(self._export_thread.quit)
        self._export_thread.finished.connect(self._export_worker.deleteLater)
        self._export_thread.finished.connect(self._clear_export_refs)
        self._export_thread.start()

    def _cancel_export(self) -> None:
        if self._export_worker is not None:
            self._export_cancel_requested = True
            self._export_worker.cancel()
            self.status_label.setText("Cancelling export…")
            self.cancel_button.setEnabled(False)

    def _on_export_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _on_export_finished(self, path: str, elapsed_s: float) -> None:
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self._set_busy(False)
        output_path = Path(path)
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0
        self.status_label.setText(
            f"Exported to {output_path.name} in {elapsed_s:.1f}s ({_format_size(size)})."
        )
        self._show_export_complete(output_path, elapsed_s, size)

    def _on_export_failed(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self._set_busy(False)
        cancelled = self._export_cancel_requested
        self._export_cancel_requested = False
        if cancelled:
            self.status_label.setText("Export cancelled.")
            return
        self.status_label.setText("Export failed.")
        self._show_error("Export DC SPD", message)

    def _show_export_complete(self, output_path: Path, elapsed_s: float, size: int) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "Export complete",
            f"Wrote {output_path.name}\n\n{_format_size(size)} in {elapsed_s:.1f}s.",
            QMessageBox.StandardButton.Ok,
            self,
        )
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()  # non-modal completion box (design §C); open(), not exec()

    # ------------------------------------------------------------------ #
    # Save / Load config (design §C)
    # ------------------------------------------------------------------ #

    def _save_config(self) -> None:
        """`Session.to_json()` to a user-chosen path."""
        if self._busy:
            return
        if self.session.scan is None:
            QMessageBox.information(self, APP_TITLE, "Open a .spd file first.")
            return
        default = str(self.spd_path.with_suffix(".json")) if self.spd_path else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config", default, "Config JSON (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self.session.to_json(), encoding="utf-8")
        except OSError as exc:
            self._show_error("Save Config", f"Could not save {path}: {exc}")
            return
        self.status_label.setText(f"Saved config to {Path(path).name}")

    def _load_config(self) -> None:
        """`Session.from_json()` from a user-chosen path."""
        if self._busy:
            return
        start_dir = str(self.spd_path.parent) if self.spd_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Config", start_dir, "Config JSON (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            data = Path(path).read_text(encoding="utf-8")
            self.session.apply_json(data)
        except (OSError, ValueError) as exc:
            self._show_error("Load Config", f"Could not load {path}: {exc}")
            return
        # `apply_json` replaced the config wholesale; the undo stack's commands
        # still describe edits against the config it replaced (see
        # `_on_scan_finished`).
        self.undo_stack.clear()
        self._refresh_all_models()
        self._update_status_counts()
        self.status_label.setText(f"Loaded config from {Path(path).name}")

    # ------------------------------------------------------------------ #
    # Cross-tab navigation / status bar
    # ------------------------------------------------------------------ #

    def _go_to_net(self, net: str) -> None:
        for tab in (self.net_tab, self.vrm_tab, self.sink_tab):
            _select_net_in_tab(tab, net)
        self.tabs.setCurrentWidget(self.net_tab)

    def _update_status_counts(self) -> None:
        counts = self.session.counts()
        self.counts_label.setText(
            f"Nets {counts['nets']} · Power {counts['power']} · "
            f"Ground {counts['ground']} · VRM {counts['vrm']} · Sink {counts['sink']}"
        )

    def _show_error(self, title: str, message: str) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Critical, title, message, QMessageBox.StandardButton.Ok, self
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()


# --------------------------------------------------------------------------- #
# Export options dialog (design §C: 3 checkboxes, all default ON)
# --------------------------------------------------------------------------- #


class _ExportOptionsDialog(QDialog):
    """"Emit .OtherCircuit" / "Patch WorkflowKey" / "Rewrite NetList" (design §C)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Options")

        self.other_circuits_check = QCheckBox("Emit .OtherCircuit blocks")
        self.other_circuits_check.setChecked(True)
        self.patch_workflow_key_check = QCheckBox("Patch WorkflowKey → 0x1000000067")
        self.patch_workflow_key_check.setChecked(True)
        self.rewrite_netlist_check = QCheckBox("Rewrite NetList classification")
        self.rewrite_netlist_check.setChecked(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.other_circuits_check)
        layout.addWidget(self.patch_workflow_key_check)
        layout.addWidget(self.rewrite_netlist_check)
        layout.addWidget(buttons)

    def options(self) -> dict[str, bool]:
        return {
            "other_circuits": self.other_circuits_check.isChecked(),
            "patch_workflow_key": self.patch_workflow_key_check.isChecked(),
            "rewrite_netlist": self.rewrite_netlist_check.isChecked(),
        }


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def _format_issues(issues: Iterable[ValidationIssue]) -> str:
    issues = list(issues)
    blocking = [issue for issue in issues if issue.blocking]
    warnings = [issue for issue in issues if not issue.blocking]
    lines: list[str] = []
    if blocking:
        lines.append("Blocking issues (must be resolved before export):")
        lines.extend(f"• {issue.message}" for issue in blocking)
    if warnings:
        if lines:
            lines.append("")
        lines.append("Warnings:")
        lines.extend(f"• {issue.message}" for issue in warnings)
    return "\n".join(lines)


def _format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def _same_file(a: Path, b: Path) -> bool:
    import os

    try:
        if a.exists() and b.exists() and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _select_net_in_tab(tab: QWidget, net: str) -> None:
    """Select + scroll to *net*'s row in *tab*'s (possibly proxied) view."""
    model = tab.model
    view = tab.view
    row = -1
    for candidate in range(model.rowCount()):
        if model.net_for_row(candidate) == net:
            row = candidate
            break
    if row < 0:
        return
    index = model.index(row, 0)
    proxy = view.model()
    if proxy is not model and hasattr(proxy, "mapFromSource"):
        index = proxy.mapFromSource(index)
        if not index.isValid():
            return
    view.setCurrentIndex(index)
    view.selectRow(index.row())
    view.scrollTo(index)
