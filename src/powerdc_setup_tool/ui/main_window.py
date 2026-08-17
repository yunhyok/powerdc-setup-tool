"""Main application window -- owned by chunk 5 (design §C).

1400x860 (min 1100x700). Toolbar: Open SPD (Ctrl+O) / Rescan (F5) /
Auto-classify / Export DC SPD (Ctrl+E) / Save Config (Ctrl+S) / Load Config
(Ctrl+L) / Undo/Redo (Ctrl+Z/Y). `QTabWidget`: Net Manager | VRMs | Sinks.
Status bar: message label / counts label / `QProgressBar`.
"""

from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QWidget

from powerdc_setup_tool.core.session import Session


class MainWindow(QMainWindow):
    """Top-level window: toolbar, 3-tab `QTabWidget`, status bar, worker lifecycle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        raise NotImplementedError

    def _set_busy(self, busy: bool) -> None:
        """Disable toolbar actions while a worker is running."""
        raise NotImplementedError

    def _open_spd(self) -> None:
        raise NotImplementedError

    def _rescan(self) -> None:
        raise NotImplementedError

    def _auto_classify(self) -> None:
        raise NotImplementedError

    def _export_dc_spd(self) -> None:
        """design §C export flow: validate -> file dialog -> options -> `ExportWorker`."""
        raise NotImplementedError

    def _save_config(self) -> None:
        """`Session.to_json()` to a user-chosen path."""
        raise NotImplementedError

    def _load_config(self) -> None:
        """`Session.from_json()` from a user-chosen path."""
        raise NotImplementedError

    def closeEvent(self, event: QCloseEvent) -> None:
        """Quit + wait(5000ms) any running worker threads before accepting."""
        raise NotImplementedError
