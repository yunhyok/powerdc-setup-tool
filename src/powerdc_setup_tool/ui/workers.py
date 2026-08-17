"""QObject workers moved to QThread -- owned by chunk 5 (design §A/§C export flow).

Must be kept as a strong attribute (e.g. `self._scan_worker`) by whatever
owns the QThread, or PySide GCs it and the thread hangs; ref-clearing slots
must guard with ``if self.sender() is self._scan_thread`` (regression noted
in `old_repo_brief.md`, re-tested by `test_ui_smoke.py`).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from powerdc_setup_tool.core.model import ScanResult
from powerdc_setup_tool.core.writer import WritePlan


class ScanWorker(QObject):
    """Runs `core.spd_scan.scan_spd` off the UI thread."""

    progress = Signal(int, int)
    finished = Signal(object)  # ScanResult
    failed = Signal(str)

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError


class ExportWorker(QObject):
    """Runs `core.writer.write_spd` off the UI thread."""

    progress = Signal(int, int)
    finished = Signal(str)  # output path
    failed = Signal(str)

    def __init__(
        self,
        scan: ScanResult,
        plan: WritePlan,
        output: Path,
        parent: QObject | None = None,
    ) -> None:
        raise NotImplementedError

    def run(self) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        raise NotImplementedError
