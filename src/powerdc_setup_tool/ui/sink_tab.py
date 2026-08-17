"""Sinks tab -- owned by chunk 5 (design §C "Sinks tab").

Columns: Use | Net | Component | Ground | Nominal V | Current (A) | Model |
PFMode | PinEqualCurrent | Pos pins | Neg pins | Name. Model/PFMode/
PinEqualCurrent are advanced spin cells hidden behind a "Show advanced"
checkbox (defaults 2/2/1, spec §A3).
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from powerdc_setup_tool.core.session import Session


class SinkTab(QWidget):
    """Sink table + row add/remove."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        raise NotImplementedError

    def _add_row(self) -> None:
        raise NotImplementedError

    def _remove_selected_rows(self) -> None:
        raise NotImplementedError
