"""Net Manager tab -- owned by chunk 5 (design §C "Net Manager tab").

Filter `QLineEdit` (Ctrl+F, space-separated AND terms, `*` glob,
case-insensitive) + class combo (All/Power/Ground/Unclassified/Selected/
Errors) + shown-count label. Table columns 0-8: Use | Net | Class |
Paired GND | Voltage (V) | Die | Pins @VRM comp | Pins @Sink comp | Source.

Classification UX (v0.1.1)
--------------------------
PowerSI itself classifies from the net list's own right-click menu
(*Classify > as PowerNets / as GroundNets / as Signal Nets*), so this tab does
the same instead of carrying its own Mark Power / Mark Ground / Clear class /
Set ground / Set voltage button row: the entries are injected at the top of the
shared `BulkEditTableView` context menu through `setExtraMenuBuilder`, which is
also what keeps them off the VRM/Sink tabs' menus.

Every entry applies to the **whole selection** (rows or cells, mapped back
through `NetFilterProxy`) and lands on the undo stack as **one**
`BulkEditCommand`, i.e. one Ctrl+Z restores every row it touched.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
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
    """Filter box + class-filter combo + net table with the *Classify* menu."""

    #: status-bar text for a context-menu operation (`MainWindow` displays it).
    statusMessage = Signal(str)

    #: v0.1.1 *Classify* submenu -- PowerSI's own labels, in its own order.
    CLASSIFY_ENTRIES: tuple[tuple[str, str], ...] = (
        ("as PowerNets", "power"),
        ("as GroundNets", "ground"),
        ("as Signal Nets", "none"),
    )

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.model = NetTableModel(session)
        self.proxy = NetFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.view = BulkEditTableView(self)
        self.view.setModel(self.proxy)
        self.view.enable_header_sorting()  # v0.1.2 click-to-sort
        self.view.setExtraMenuBuilder(self._build_extra_menu)
        # v0.1.2 "Check/Uncheck all (shown)" reports its row count this way.
        self.view.statusMessage.connect(self.statusMessage)

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

        top_row = QHBoxLayout()
        top_row.addWidget(self.filter_edit, 1)
        top_row.addWidget(self.class_combo)
        top_row.addWidget(self.shown_label)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
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
    # Context menu (v0.1.1: PowerSI's right-click Classify)
    # ------------------------------------------------------------------ #

    def _build_extra_menu(self, menu: QMenu) -> None:
        """Prepend *Classify* + the selection-wide voltage dialog.

        "Set paired ground…" is already a shared context-menu entry and it
        already writes the whole selection, so it is not duplicated here;
        "Set voltage…" is, because the shared "Set value…" fills only the
        column the current cell sits in.
        """
        # Built with an explicit parent, *then* added: the `QMenu` returned by
        # `QMenu.addMenu(title)` is owned by Python, so a submenu created that
        # way is destroyed as soon as this function returns and the entry is
        # left pointing at a dead menu.
        classify = QMenu("Classify", menu)
        menu.addMenu(classify)
        for label, net_class in self.CLASSIFY_ENTRIES:
            action = classify.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=net_class: self.classify_selection(value)
            )
        voltage_action = menu.addAction("Set voltage…")
        voltage_action.triggered.connect(self._set_voltage_for_selected)

    def classify_selection(self, net_class: str) -> int:
        """Set *net_class* on every selected row as one undoable command.

        Routed through the net table's Class column, i.e. `Session.set_class`;
        rows already in that class are dropped by `BulkEditCommand`, so
        re-classifying a mixed selection pushes exactly the rows that moved.
        Returns the number of rows actually classified.
        """
        name = _classify_name(net_class)
        count = self._set_column_for_selected("net_class", net_class, f"Classify {name}")
        if count:
            self.statusMessage.emit(
                f"Classified {count} net{'' if count == 1 else 's'} as {name}."
            )
        return count

    # ------------------------------------------------------------------ #
    # Selection helpers
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
        """Write *value* into column *key* for every selected row, as one command."""
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

    def _set_voltage_for_selected(self) -> None:
        value, ok = QInputDialog.getDouble(
            self, "Set voltage", "Voltage (V):", 1.0, _VOLTAGE_MIN, _VOLTAGE_MAX, 6
        )
        if ok:
            count = self._set_column_for_selected("voltage", value, "Set voltage")
            if count:
                self.statusMessage.emit(
                    f"Set {value:g} V on {count} net{'' if count == 1 else 's'}."
                )


def _classify_name(net_class: str) -> str:
    """``"power"`` -> ``"PowerNets"`` (the menu label minus its ``as`` prefix)."""
    for label, value in NetManagerTab.CLASSIFY_ENTRIES:
        if value == net_class:
            return label.removeprefix("as ")
    return str(net_class)
