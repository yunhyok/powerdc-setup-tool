"""QObject workers moved to QThread -- owned by chunk 5 (design §A/§C export flow).

Must be kept as a strong attribute (e.g. `self._scan_worker`) by whatever
owns the QThread, or PySide GCs it and the thread hangs; ref-clearing slots
must guard with ``if self.sender() is self._scan_thread`` (regression noted
in `old_repo_brief.md`, re-tested by `test_ui_smoke.py`).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from powerdc_setup_tool.core.model import ScanResult
from powerdc_setup_tool.core.spd_scan import scan_spd
from powerdc_setup_tool.core.writer import WritePlan, write_spd

__all__ = ["ScanWorker", "ExportWorker"]


class ScanWorker(QObject):
    """Runs `core.spd_scan.scan_spd` off the UI thread.

    *cancel_event* is the exact `ExportWorker` pattern below: a plain
    `threading.Event`, owned by this worker, polled by `scan_spd` per batch of
    lines; `cancel()` sets it and the scan unwinds with `ScanCancelled`. Scanning
    the real 1.4 GB input takes tens of seconds, so without this a window close
    would either block for that long or destroy a running `QThread` (a fatal Qt
    error). Created up front, not lazily, so `MainWindow.closeEvent` can set it
    at any point in the worker's life.

    `ScanCancelled` reaches the UI through `failed`, like every other exception;
    `MainWindow` distinguishes it from a real failure with its own
    ``_scan_cancel_requested`` flag rather than by exception type, so a cancel
    that lands as some other error still does not raise a dialog on shutdown.
    """

    progress = Signal(int, int)
    finished = Signal(object)  # ScanResult
    failed = Signal(str)

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = scan_spd(
                self.path, progress=self.progress.emit, cancel=self.cancel_event
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def cancel(self) -> None:
        """Ask `scan_spd` to stop; it raises `ScanCancelled` at its next poll."""
        self.cancel_event.set()


class ExportWorker(QObject):
    """Runs `core.writer.write_spd` off the UI thread.

    *cancel_event* is a plain `threading.Event`, owned by this worker and
    polled by `write_spd` per chunk/block; `cancel()` sets it (design §C
    "Cancel button wired to the Event"). It is created up front (not lazily)
    so a caller can wire the Cancel button before `run()` ever starts.
    """

    progress = Signal(int, int)
    finished = Signal(str, float)  # output path, elapsed seconds
    failed = Signal(str)

    def __init__(
        self,
        scan: ScanResult,
        plan: WritePlan,
        output: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.scan = scan
        self.plan = plan
        self.output = Path(output)
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        started = time.monotonic()
        try:
            write_spd(
                self.scan,
                self.plan,
                self.output,
                progress=self.progress.emit,
                cancel=self.cancel_event,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(str(exc))
            return
        self.finished.emit(str(self.output), time.monotonic() - started)

    def cancel(self) -> None:
        """Ask `write_spd` to stop; the partial `.part` file is deleted (design §D)."""
        self.cancel_event.set()
