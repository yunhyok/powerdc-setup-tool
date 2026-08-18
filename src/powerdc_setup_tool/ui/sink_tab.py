"""Sinks tab -- owned by chunk 5 (design §C "Sinks tab").

Columns: Use | Net | Component | Ground | Nominal V | Current (A) | Model |
PFMode | PinEqualCurrent | Pos pins | Neg pins | Name. Model/PFMode/
PinEqualCurrent are advanced spin cells hidden behind a "Show advanced"
checkbox (defaults 2/2/1, spec §A3).

v0.1.2 puts a `ConfigSortProxy` between the model and the view for header
sorting; the proxy never reorders *columns*, so `ADVANCED_COLUMNS` still
addresses the right three.
"""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from powerdc_setup_tool.core.session import Session
from powerdc_setup_tool.ui.models import ConfigSortProxy, SinkTableModel, source_index
from powerdc_setup_tool.ui.table_view import BulkEditTableView

__all__ = ["SinkTab"]


class SinkTab(QWidget):
    """Sink table + row add/remove."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.model = SinkTableModel(session)
        self.proxy = ConfigSortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = BulkEditTableView(self)
        self.view.setModel(self.proxy)
        self.view.enable_header_sorting()  # v0.1.2

        self.add_button = QPushButton("Add Sink row")
        self.add_button.setToolTip("Add a second/third .Sink row for the current net")
        self.add_button.clicked.connect(self._add_row)

        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setToolTip(
            "Remove the selected Sink row(s) -- the last row of a net is always kept"
        )
        self.remove_button.clicked.connect(self._remove_selected_rows)

        self.advanced_checkbox = QCheckBox("Show advanced")
        self.advanced_checkbox.setToolTip("Show Model / PFMode / PinEqualCurrent (spec §A3)")
        self.advanced_checkbox.toggled.connect(self._set_advanced_visible)

        button_row = QHBoxLayout()
        button_row.addWidget(self.add_button)
        button_row.addWidget(self.remove_button)
        button_row.addStretch(1)
        button_row.addWidget(self.advanced_checkbox)

        layout = QVBoxLayout(self)
        layout.addLayout(button_row)
        layout.addWidget(self.view)

        # design §C: the advanced trio starts hidden behind the checkbox.
        self._set_advanced_visible(self.advanced_checkbox.isChecked())

    def _add_row(self) -> None:
        net = self.view.current_net()
        if not net and self.model.rowCount():
            net = self.model.net_for_row(0)
        if not net:
            return
        self.session.add_sink_row(net)
        self.model.refresh_from_session()

    def _remove_selected_rows(self) -> None:
        row_keys = _selected_row_keys(self.view, self.model)
        removed = False
        for row_key in row_keys:
            if self.session.remove_row(row_key):
                removed = True
        if removed:
            self.model.refresh_from_session()

    def _set_advanced_visible(self, visible: bool) -> None:
        for column in SinkTableModel.ADVANCED_COLUMNS:
            self.view.setColumnHidden(column, not visible)


def _selected_row_keys(view: BulkEditTableView, model: SinkTableModel) -> list[str]:
    """Stable `Session` row keys for every row touched by *view*'s selection.

    Mapped down through the v0.1.2 sort proxy -- a view row is a sorted
    position, not a model row (see `ui/vrm_tab.py`).
    """
    selection = view.selectionModel()
    indexes = selection.selectedIndexes() if selection is not None else []
    rows = sorted({source_index(index).row() for index in indexes if index.isValid()})
    if not rows:
        current = view.currentIndex()
        if current.isValid():
            rows = [source_index(current).row()]
    return [key for row in rows if (key := model.row_key(row))]
