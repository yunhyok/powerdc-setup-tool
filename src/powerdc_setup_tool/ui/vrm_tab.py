"""VRMs tab -- owned by chunk 5 (design §C "VRMs tab").

Columns: Use | Net | Component | Ground | Nominal V | Sense V | Output
Current (A) | Pos pins | Neg pins | Name (derived read-only,
`VRM_{comp}_{pnet}_{gnet}`).
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from powerdc_setup_tool.core.session import Session
from powerdc_setup_tool.ui.models import VrmTableModel
from powerdc_setup_tool.ui.table_view import BulkEditTableView

__all__ = ["VrmTab"]


class VrmTab(QWidget):
    """VRM table + row add/remove."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.model = VrmTableModel(session)
        self.view = BulkEditTableView(self)
        self.view.setModel(self.model)

        self.add_button = QPushButton("Add VRM row")
        self.add_button.setToolTip("Add a second/third .VRM row for the current net")
        self.add_button.clicked.connect(self._add_row)

        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setToolTip(
            "Remove the selected VRM row(s) -- the last row of a net is always kept"
        )
        self.remove_button.clicked.connect(self._remove_selected_rows)

        button_row = QHBoxLayout()
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(button_row)
        layout.addWidget(self.view)

    def _add_row(self) -> None:
        net = self.view.current_net()
        if not net and self.model.rowCount():
            net = self.model.net_for_row(0)
        if not net:
            return
        self.session.add_vrm_row(net)
        self.model.refresh_from_session()

    def _remove_selected_rows(self) -> None:
        row_keys = _selected_row_keys(self.view, self.model)
        removed = False
        for row_key in row_keys:
            if self.session.remove_row(row_key):
                removed = True
        if removed:
            self.model.refresh_from_session()


def _selected_row_keys(view: BulkEditTableView, model: VrmTableModel) -> list[str]:
    """Stable `Session` row keys for every row touched by *view*'s selection."""
    selection = view.selectionModel()
    indexes = selection.selectedIndexes() if selection is not None else []
    rows = sorted({index.row() for index in indexes if index.isValid()})
    if not rows:
        current = view.currentIndex()
        if current.isValid():
            rows = [current.row()]
    return [key for row in rows if (key := model.row_key(row))]
