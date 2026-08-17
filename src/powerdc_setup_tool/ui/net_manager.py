"""Net Manager tab -- owned by chunk 5 (design §C "Net Manager tab").

Filter `QLineEdit` (Ctrl+F, space-separated AND terms, `*` glob,
case-insensitive) + class combo (All/Power/Ground/Unclassified/Selected/
Errors) + shown-count label. Buttons: Mark Power, Mark Ground, Clear class,
Set ground for selected..., Set voltage for selected.... Table columns 0-8:
Use | Net | Class | Paired GND | Voltage (V) | Die | Pins @VRM comp |
Pins @Sink comp | Source.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from powerdc_setup_tool.core.session import Session
from powerdc_setup_tool.ui.models import NetFilterProxy, NetTableModel, source_index
from powerdc_setup_tool.ui.table_view import BulkEditCommand, BulkEditTableView

__all__ = ["NetManagerTab"]

#: design §C export-dialog / net-table voltage bounds -- wide, never rejects a
#: real rail; mirrors `ui.delegates._NUM_MIN`/`_NUM_MAX` without importing it.
_VOLTAGE_MIN = -1.0e6
_VOLTAGE_MAX = 1.0e6


class NetManagerTab(QWidget):
    """Filter box + class/pairing toolbar + net table."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.model = NetTableModel(session)
        self.proxy = NetFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.view = BulkEditTableView(self)
        self.view.setModel(self.proxy)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter nets… (space = AND, * = glob)")
        self.filter_edit.setToolTip("Filter nets by name (Ctrl+F)")
        self.filter_edit.textChanged.connect(self._apply_filter)

        self._filter_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self._filter_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._filter_shortcut.activated.connect(self._focus_filter)

        self.class_combo = QComboBox()
        self.class_combo.addItems(list(NetFilterProxy.MODES))
        self.class_combo.setToolTip("Show only nets in this class/state")
        self.class_combo.currentTextChanged.connect(self.proxy.setClassFilter)

        self.shown_label = QLabel()

        self.mark_power_button = QPushButton("Mark Power")
        self.mark_power_button.clicked.connect(self._mark_power)
        self.mark_ground_button = QPushButton("Mark Ground")
        self.mark_ground_button.clicked.connect(self._mark_ground)
        self.clear_class_button = QPushButton("Clear class")
        self.clear_class_button.clicked.connect(self._clear_class)
        self.set_ground_button = QPushButton("Set ground for selected…")
        self.set_ground_button.clicked.connect(self._set_ground_for_selected)
        self.set_voltage_button = QPushButton("Set voltage for selected…")
        self.set_voltage_button.clicked.connect(self._set_voltage_for_selected)

        top_row = QHBoxLayout()
        top_row.addWidget(self.filter_edit, 1)
        top_row.addWidget(self.class_combo)
        top_row.addWidget(self.shown_label)

        button_row = QHBoxLayout()
        button_row.addWidget(self.mark_power_button)
        button_row.addWidget(self.mark_ground_button)
        button_row.addWidget(self.clear_class_button)
        button_row.addWidget(self.set_ground_button)
        button_row.addWidget(self.set_voltage_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addLayout(button_row)
        layout.addWidget(self.view)

        for signal in (
            self.proxy.rowsInserted,
            self.proxy.rowsRemoved,
            self.proxy.modelReset,
            self.proxy.layoutChanged,
        ):
            signal.connect(self._update_shown_label)
        self._update_shown_label()

    # ------------------------------------------------------------------ #
    # Filter box / class combo
    # ------------------------------------------------------------------ #

    def _focus_filter(self) -> None:
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def _apply_filter(self, text: str) -> None:
        self.proxy.setFilterExpression(text)
        self._update_shown_label()

    def _update_shown_label(self, *_args: object) -> None:
        self.shown_label.setText(f"{self.proxy.shownCount()} / {self.proxy.totalCount()} shown")

    # ------------------------------------------------------------------ #
    # Toolbar buttons (design §C)
    # ------------------------------------------------------------------ #

    def _selected_source_rows(self) -> list[int]:
        """Source-model rows touched by the view's (possibly proxied) selection."""
        selection = self.view.selectionModel()
        indexes = selection.selectedIndexes() if selection is not None else []
        rows = sorted({source_index(index).row() for index in indexes if index.isValid()})
        if not rows:
            current = self.view.currentIndex()
            if current.isValid():
                rows = [source_index(current).row()]
        return rows

    def _set_column_for_selected(self, key: str, value: object, text: str) -> int:
        """Write *value* into column *key* for every selected row (design §C
        "Mark Power"/"Mark Ground"/"Clear class"/"Set voltage for selected…")."""
        column = self.model.column_index(key)
        rows = self._selected_source_rows()
        if column < 0 or not rows:
            return 0
        changes: list[tuple[object, ...]] = []
        for row in rows:
            index = self.model.index(row, column)
            if not self.model.is_editable(index):
                continue
            ok, parsed = self.model.parse_value(index, value)
            if not ok:
                continue
            changes.append(
                (
                    index,
                    self.model.data(index, Qt.ItemDataRole.EditRole),
                    parsed,
                    self.model.is_override(index),
                )
            )
        if not changes:
            return 0
        command = BulkEditCommand(self.model, changes, text=text)
        if command.isEmpty():
            return 0
        self.view.undo_stack.push(command)
        return command.cellCount()

    def _mark_power(self) -> None:
        self._set_column_for_selected("net_class", "power", "Mark Power")

    def _mark_ground(self) -> None:
        self._set_column_for_selected("net_class", "ground", "Mark Ground")

    def _clear_class(self) -> None:
        self._set_column_for_selected("net_class", "none", "Clear class")

    def _set_ground_for_selected(self) -> None:
        grounds = self.session.ground_nets()
        if not grounds:
            QMessageBox.information(
                self, "Set ground for selected", "No net is classified as ground yet."
            )
            return
        gnet, ok = QInputDialog.getItem(
            self, "Set ground for selected", "Paired ground net:", grounds, 0, False
        )
        if ok and gnet:
            self.view.set_paired_ground_for_selection(gnet)

    def _set_voltage_for_selected(self) -> None:
        value, ok = QInputDialog.getDouble(
            self, "Set voltage for selected", "Voltage (V):", 1.0, _VOLTAGE_MIN, _VOLTAGE_MAX, 6
        )
        if ok:
            self._set_column_for_selected("voltage", value, "Set voltage")
