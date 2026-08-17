"""VRMs tab -- owned by chunk 5 (design §C "VRMs tab").

Columns: Use | Net | Component | Ground | Nominal V | Sense V | Output
Current (A) | Pos pins | Neg pins | Name (derived read-only,
`VRM_{comp}_{pnet}_{gnet}`).
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from powerdc_setup_tool.core.session import Session


class VrmTab(QWidget):
    """VRM table + row add/remove."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        raise NotImplementedError

    def _add_row(self) -> None:
        raise NotImplementedError

    def _remove_selected_rows(self) -> None:
        raise NotImplementedError
