"""Multi-cell bulk edit table view + undo command -- owned by chunk 4
(design §C "Bulk-edit semantics").

Applies to all three config tables (`ExtendedSelection`, `SelectItems`,
row-select via vertical header): type-to-fill, fill-down/right (Ctrl+D/R),
TSV copy/paste (Ctrl+C/V), space-toggle, context menu, one `BulkEditCommand`
on the undo stack per operation.

The context menu takes an injectable per-tab prefix (`setExtraMenuBuilder`):
v0.1.1's *Classify* submenu belongs to the Net Manager alone, and the VRM/Sink
tabs share this class, so the owning tab supplies those entries.

Undo fidelity
-------------
A `Session` cell is either *auto-derived* or *user-set* (design §B), so
restoring a value is not enough: writing the old number back would leave the
cell flagged as an override it never had. `BulkEditCommand` therefore captures
``(old value, was-overridden)`` per cell and undoes a cell that used to be auto
via `Session.reset_auto` instead of `setData` -- which also makes the undo of a
whole propagation cascade land on the right auto values, because the derived
cells are restored *before* the upstream net voltage they follow.

The one ordering subtlety: design §C has the delegate commit to `currentIndex`
*before* it emits `bulkApplyRequested`, so by the time the view builds the
command the anchor cell's old value is already gone. `edit()` snapshots it on
the way in (`note_pending_edit`), which is the only pre-commit hook Qt gives us.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, Qt, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QContextMenuEvent,
    QGuiApplication,
    QKeyEvent,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QInputDialog,
    QMenu,
    QTableView,
    QWidget,
)

from powerdc_setup_tool.ui.delegates import delegate_for_kind
from powerdc_setup_tool.ui.models import (
    KIND_BOOL,
    BaseConfigModel,
    ColumnSpec,
    config_model,
    source_index,
)

__all__ = ["BulkEditTableView", "BulkEditCommand", "RESET_TO_AUTO"]


class _ResetToAuto:
    """Sentinel new-value meaning "clear the override" (design §B `reset_auto`)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "RESET_TO_AUTO"


#: `BulkEditCommand` new-value sentinel used by the *Reset to auto* menu entry.
RESET_TO_AUTO = _ResetToAuto()


@dataclass(frozen=True)
class _CellChange:
    """One cell touched by a bulk operation, addressed by stable `Session` key.

    `QModelIndex` is not stored: a `Session` mutator can bump
    `structure_version` and make every model index stale, while
    ``(row_key, column)`` survives the reset.
    """

    row_key: str
    column: int
    old: Any
    old_override: bool
    new: Any
    resettable: bool

    @property
    def is_reset(self) -> bool:
        return self.new is RESET_TO_AUTO


class BulkEditCommand(QUndoCommand):
    """One undo-stack entry covering every cell touched by a single bulk operation."""

    def __init__(
        self,
        model: BaseConfigModel,
        changes: Sequence[tuple[Any, ...]],
        parent: QUndoCommand | None = None,
        *,
        text: str = "Bulk edit",
    ) -> None:
        super().__init__(text, parent)
        self._model = config_model(model) or model
        self._changes: list[_CellChange] = []
        for entry in changes:
            change = self._build(entry)
            if change is not None:
                self._changes.append(change)

    def _build(self, entry: Sequence[Any]) -> _CellChange | None:
        """``(index, old, new)`` or ``(index, old, new, was_overridden)``."""
        index, old, new = entry[0], entry[1], entry[2]
        if not index.isValid():
            return None
        local = source_index(index)
        spec: ColumnSpec | None = self._model.column_spec(local.column())
        row_key = self._model.row_key(local.row())
        if spec is None or not row_key:
            return None
        old_override = bool(entry[3]) if len(entry) > 3 else self._model.is_override(local)
        if new is RESET_TO_AUTO:
            if not spec.resettable or not old_override:
                return None  # already auto -- nothing to reset
        elif old == new and (old_override or not spec.resettable):
            return None  # no value change and no override flag to raise
        return _CellChange(
            row_key=row_key,
            column=local.column(),
            old=old,
            old_override=old_override,
            new=new,
            resettable=spec.resettable,
        )

    # -- introspection (used by the view and by tests) -------------------- #

    def cells(self) -> list[_CellChange]:
        return list(self._changes)

    def cellCount(self) -> int:
        return len(self._changes)

    def isEmpty(self) -> bool:
        return not self._changes

    # -- QUndoCommand ----------------------------------------------------- #

    def redo(self) -> None:
        resets: list[QModelIndex] = []
        for change in self._changes:
            index = self._model.index_for_key(change.row_key, change.column)
            if not index.isValid():
                continue
            if change.is_reset:
                resets.append(index)
            else:
                self._model.setData(index, change.new, Qt.ItemDataRole.EditRole)
        if resets:
            self._model.reset_auto(resets)

    def undo(self) -> None:
        resets: list[QModelIndex] = []
        for change in reversed(self._changes):
            index = self._model.index_for_key(change.row_key, change.column)
            if not index.isValid():
                continue
            if change.resettable and not change.old_override:
                resets.append(index)
            else:
                self._model.setData(index, change.old, Qt.ItemDataRole.EditRole)
        if resets:
            self._model.reset_auto(resets)


class BulkEditTableView(QTableView):
    """Table view implementing design §C's full bulk-edit semantics."""

    #: context menu "Go to net" -- `MainWindow` jumps the other tabs to this net.
    goToNetRequested = Signal(str)
    #: emitted after "Set paired ground…" applied *gnet* to the selection.
    pairedGroundRequested = Signal(str)
    #: cell count of the bulk operation that was just pushed (status bar).
    bulkApplied = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(True)
        # design §C: "row-select via vertical header" -- QTableView's own header
        # handling already does Ctrl/Shift-aware row selection once clickable.
        self.verticalHeader().setSectionsClickable(True)
        self.horizontalHeader().setStretchLastSection(True)

        self._undo_stack = QUndoStack(self)
        self._owns_undo_stack = True
        self._pending_edit: tuple[tuple[int, int], Any, bool] | None = None
        self._auto_delegates = True
        self._delegates: list[Any] = []
        self._delegate_columns: list[int] = []
        self._extra_menu_builder: Callable[[QMenu], None] | None = None

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #

    @property
    def undo_stack(self) -> QUndoStack:
        """The stack every bulk operation is pushed onto (design §C Ctrl+Z/Y)."""
        return self._undo_stack

    @undo_stack.setter
    def undo_stack(self, stack: QUndoStack) -> None:
        self.setUndoStack(stack)

    def setUndoStack(self, stack: QUndoStack) -> None:
        """Share `MainWindow`'s stack so Undo spans all three tabs."""
        if stack is None or stack is self._undo_stack:
            return
        self._undo_stack = stack
        self._owns_undo_stack = False

    def setModel(self, model: Any) -> None:
        super().setModel(model)
        self._pending_edit = None
        if self._owns_undo_stack:
            # Commands address cells by `Session` row key; a new model means a new
            # session, so the old history could not be replayed anyway. A *shared*
            # stack is left alone -- clearing it would wipe the other tabs' history.
            self._undo_stack.clear()
        if self._auto_delegates:
            self.install_default_delegates()

    def setExtraMenuBuilder(self, builder: Callable[[QMenu], None] | None) -> None:
        """Install a hook that prepends tab-specific entries to the context menu.

        This view is shared by all three tabs, but only the Net Manager gets the
        v0.1.1 *Classify* submenu (PowerSI's own right-click wording), so the
        owning tab injects its entries instead of `build_context_menu` growing a
        per-tab special case. The builder is called with the fresh `QMenu`
        *before* the shared entries, and a separator is added after it if it
        contributed anything.
        """
        self._extra_menu_builder = builder

    def extraMenuBuilder(self) -> Callable[[QMenu], None] | None:
        return self._extra_menu_builder

    def setAutoDelegates(self, enabled: bool) -> None:
        """Disable before `setModel` when chunk 5 wants to install its own set."""
        self._auto_delegates = bool(enabled)

    def install_default_delegates(self) -> None:
        """A `NumericDelegate`/`ComboDelegate` per column kind, wired to `apply_bulk`."""
        for column in self._delegate_columns:
            self.setItemDelegateForColumn(column, None)
        self._delegates = []
        self._delegate_columns = []
        model = self.config_model()
        if model is None:
            return
        for column, spec in enumerate(model.columns):
            delegate = delegate_for_kind(spec.kind, self) if spec.editable else None
            if delegate is None:
                continue
            delegate.bulkApplyRequested.connect(self.apply_bulk)
            self.setItemDelegateForColumn(column, delegate)
            self._delegates.append(delegate)
            self._delegate_columns.append(column)

    def config_model(self) -> BaseConfigModel | None:
        """The `BaseConfigModel` under this view's (possibly proxied) model."""
        return config_model(self.model())

    # ------------------------------------------------------------------ #
    # Small accessors over the (possibly proxied) model
    # ------------------------------------------------------------------ #

    def _spec(self, column: int) -> ColumnSpec | None:
        model = self.config_model()
        return None if model is None else model.column_spec(column)

    def _is_editable(self, index: QModelIndex) -> bool:
        model = self.config_model()
        return bool(model is not None and index.isValid() and model.is_editable(index))

    def _is_override(self, index: QModelIndex) -> bool:
        model = self.config_model()
        return bool(model is not None and model.is_override(index))

    @staticmethod
    def _read(index: QModelIndex) -> Any:
        return index.data(Qt.ItemDataRole.EditRole)

    def _selected_indexes(self) -> list[QModelIndex]:
        """The selection in (row, column) order; the current cell when empty."""
        selection = self.selectionModel()
        indexes = [i for i in selection.selectedIndexes() if i.isValid()] if selection else []
        if not indexes:
            current = self.currentIndex()
            if current.isValid():
                indexes = [current]
        indexes.sort(key=lambda index: (index.row(), index.column()))
        return indexes

    def _index(self, row: int, column: int) -> QModelIndex:
        model = self.model()
        if model is None:
            return QModelIndex()
        if not (0 <= row < model.rowCount() and 0 <= column < model.columnCount()):
            return QModelIndex()
        return model.index(row, column)

    def _push(self, changes: Sequence[tuple[Any, ...]], text: str) -> int:
        """One `QUndoCommand` per bulk operation (design §C)."""
        if not changes:
            return 0
        command = BulkEditCommand(self.model(), changes, text=text)
        if command.isEmpty():
            return 0
        count = command.cellCount()
        self._undo_stack.push(command)
        self.bulkApplied.emit(count)
        return count

    # ------------------------------------------------------------------ #
    # Type-to-fill (design §C)
    # ------------------------------------------------------------------ #

    def note_pending_edit(self, index: QModelIndex) -> None:
        """Snapshot a cell's pre-edit value/override before its editor opens.

        Called from `edit()`; the delegate commits to the model before it emits
        `bulkApplyRequested`, so this is where the anchor cell's undo state
        comes from.
        """
        if not index.isValid() or self.config_model() is None:
            self._pending_edit = None
            return
        self._pending_edit = (
            (index.row(), index.column()),
            self._read(index),
            self._is_override(index),
        )

    @Slot(object, QModelIndex)
    def apply_bulk(self, value: Any, index: QModelIndex) -> int:
        """Write *value* into every selected cell whose `ColumnSpec.kind` matches
        *index*'s column (design §C type-to-fill).

        A selection spanning *Nominal V* + *Sense V* fills both; non-matching
        kinds, read-only cells and cells the value will not parse for are
        skipped silently.
        """
        model = self.config_model()
        spec = self._spec(index.column()) if index.isValid() else None
        if model is None or spec is None:
            return 0

        targets: list[QModelIndex] = []
        seen: set[tuple[int, int]] = set()
        for candidate in self._selected_indexes():
            other = self._spec(candidate.column())
            if other is None or other.kind != spec.kind:
                continue
            if not self._is_editable(candidate):
                continue
            cell = (candidate.row(), candidate.column())
            if cell not in seen:
                seen.add(cell)
                targets.append(candidate)
        anchor = (index.row(), index.column())
        if anchor not in seen and self._is_editable(index):
            targets.append(index)

        pending = self._pending_edit
        self._pending_edit = None

        changes: list[tuple[Any, ...]] = []
        for target in targets:
            ok, parsed = model.parse_value(target, value)
            if not ok:
                continue
            if pending is not None and pending[0] == (target.row(), target.column()):
                old, was_override = pending[1], pending[2]
            else:
                old, was_override = self._read(target), self._is_override(target)
            changes.append((target, old, parsed, was_override))
        return self._push(changes, f"Set {spec.title}")

    def _apply_bulk(self, value: Any, index: QModelIndex) -> int:
        """The stub published this private name; `apply_bulk` is the public slot."""
        return self.apply_bulk(value, index)

    # ------------------------------------------------------------------ #
    # Fill / copy / paste / toggle
    # ------------------------------------------------------------------ #

    def _fill_down(self) -> int:
        """Ctrl+D: per selected column, topmost selected cell's value -> lower selected cells."""
        by_column: dict[int, list[QModelIndex]] = {}
        for index in self._selected_indexes():
            by_column.setdefault(index.column(), []).append(index)
        changes: list[tuple[Any, ...]] = []
        model = self.config_model()
        if model is None:
            return 0
        for column in sorted(by_column):
            cells = sorted(by_column[column], key=lambda index: index.row())
            if len(cells) < 2:
                continue
            value = self._read(cells[0])
            for target in cells[1:]:
                if not self._is_editable(target):
                    continue
                ok, parsed = model.parse_value(target, value)
                if not ok:
                    continue
                changes.append((target, self._read(target), parsed, self._is_override(target)))
        return self._push(changes, "Fill down")

    def _fill_right(self) -> int:
        """Ctrl+R: leftmost selected cell -> other selected kind-compatible cells in row."""
        by_row: dict[int, list[QModelIndex]] = {}
        for index in self._selected_indexes():
            by_row.setdefault(index.row(), []).append(index)
        changes: list[tuple[Any, ...]] = []
        model = self.config_model()
        if model is None:
            return 0
        for row in sorted(by_row):
            cells = sorted(by_row[row], key=lambda index: index.column())
            if len(cells) < 2:
                continue
            source_spec = self._spec(cells[0].column())
            if source_spec is None:
                continue
            value = self._read(cells[0])
            for target in cells[1:]:
                spec = self._spec(target.column())
                if spec is None or spec.kind != source_spec.kind:
                    continue
                if not self._is_editable(target):
                    continue
                ok, parsed = model.parse_value(target, value)
                if not ok:
                    continue
                changes.append((target, self._read(target), parsed, self._is_override(target)))
        return self._push(changes, "Fill right")

    def selection_as_tsv(self) -> str:
        """TSV of the selection's bounding rect; unselected cells inside it stay blank."""
        model = self.config_model()
        indexes = self._selected_indexes()
        if model is None or not indexes:
            return ""
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        selected = {(index.row(), index.column()) for index in indexes}
        lines: list[str] = []
        for row in range(rows[0], rows[-1] + 1):
            cells: list[str] = []
            for column in range(columns[0], columns[-1] + 1):
                index = self._index(row, column)
                if (row, column) in selected and index.isValid():
                    cells.append(model.cell_text(index))
                else:
                    cells.append("")
            lines.append("\t".join(cells))
        return "\n".join(lines)

    def _copy_selection(self) -> None:
        """Ctrl+C: TSV of the selection bounding rect to the clipboard."""
        text = self.selection_as_tsv()
        if text:
            QGuiApplication.clipboard().setText(text)

    @staticmethod
    def parse_clipboard_grid(text: str) -> list[list[str]]:
        """Clipboard text -> grid: split on ``\\n``, then ``\\t`` (fallback ``,``)."""
        payload = text.replace("\r\n", "\n").replace("\r", "\n")
        while payload.endswith("\n"):
            payload = payload[:-1]
        if not payload:
            return []
        separator = "\t" if "\t" in payload else ","
        return [line.split(separator) for line in payload.split("\n")]

    def paste_grid(self, grid: Sequence[Sequence[str]]) -> int:
        """design §C paste rules: 1x1 broadcast, 1xN/Nx1 broadcast along the other
        axis of the selection, else a rectangular write anchored at `currentIndex`
        and clipped to the model's bounds."""
        model = self.config_model()
        if model is None or not grid:
            return 0
        indexes = self._selected_indexes()
        if not indexes:
            return 0
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        selected = {(index.row(), index.column()) for index in indexes}

        height = len(grid)
        width = max(len(line) for line in grid)
        writes: list[tuple[int, int, str]] = []

        if height == 1 and width == 1:
            value = grid[0][0]
            writes = [(row, column, value) for row, column in sorted(selected)]
        elif height == 1:
            # broadcast down every selected row, anchored at the selection's left
            for row in rows:
                for offset, value in enumerate(grid[0]):
                    writes.append((row, columns[0] + offset, value))
        elif width == 1:
            # broadcast across every selected column, anchored at the selection's top
            for column in columns:
                for offset, line in enumerate(grid):
                    if line:
                        writes.append((rows[0] + offset, column, line[0]))
        else:
            current = self.currentIndex()
            top = current.row() if current.isValid() else rows[0]
            left = current.column() if current.isValid() else columns[0]
            for row_offset, line in enumerate(grid):
                for column_offset, value in enumerate(line):
                    writes.append((top + row_offset, left + column_offset, value))

        changes: list[tuple[Any, ...]] = []
        for row, column, raw in writes:
            index = self._index(row, column)  # clipped to bounds
            if not index.isValid() or not self._is_editable(index):
                continue
            ok, parsed = model.parse_value(index, raw)
            if not ok:
                continue
            changes.append((index, self._read(index), parsed, self._is_override(index)))
        return self._push(changes, "Paste")

    def _paste_clipboard(self) -> int:
        """Ctrl+V: clipboard split on ``\\n`` then ``\\t`` (fallback ``,``); 1x1 broadcast,
        1xN/Nx1 broadcast along the other axis, else rectangular write anchored at
        `currentIndex`, clipped to bounds."""
        return self.paste_grid(self.parse_clipboard_grid(QGuiApplication.clipboard().text()))

    def toggle_cells(self, indexes: Iterable[QModelIndex], checked: bool | None = None) -> int:
        """Set every editable bool cell in *indexes* to *checked* (or flip each)."""
        changes: list[tuple[Any, ...]] = []
        for index in indexes:
            spec = self._spec(index.column())
            if spec is None or spec.kind != KIND_BOOL or not self._is_editable(index):
                continue
            old = bool(self._read(index))
            changes.append((index, old, (not old) if checked is None else bool(checked), False))
        return self._push(changes, "Toggle")

    def _toggle_selected_bools(self) -> int:
        """Space: toggle all selected bool cells to the inverse of the anchor cell."""
        bools = [
            index
            for index in self._selected_indexes()
            if (spec := self._spec(index.column())) is not None
            and spec.kind == KIND_BOOL
            and self._is_editable(index)
        ]
        if not bools:
            return 0
        current = self.currentIndex()
        anchor = next(
            (
                index
                for index in bools
                if current.isValid()
                and index.row() == current.row()
                and index.column() == current.column()
            ),
            bools[0],
        )
        return self.toggle_cells(bools, checked=not bool(self._read(anchor)))

    # ------------------------------------------------------------------ #
    # Context-menu operations (public so chunk 5 can reuse them on toolbars)
    # ------------------------------------------------------------------ #

    def set_value_for_selection(self, text: str) -> int:
        """"Set value…": fill the selection from typed text, kind-matched."""
        current = self.currentIndex()
        if not current.isValid():
            return 0
        return self.apply_bulk(text, current)

    def reset_selection_to_auto(self) -> int:
        """"Reset to auto": clear the override of every selected resettable cell."""
        changes: list[tuple[Any, ...]] = []
        for index in self._selected_indexes():
            spec = self._spec(index.column())
            if spec is None or not spec.resettable or not self._is_editable(index):
                continue
            changes.append((index, self._read(index), RESET_TO_AUTO, self._is_override(index)))
        return self._push(changes, "Reset to auto")

    def check_selection(self, checked: bool) -> int:
        """"Check/Uncheck selected": the selected bool cells, or the Use column."""
        indexes = [
            index
            for index in self._selected_indexes()
            if (spec := self._spec(index.column())) is not None and spec.kind == KIND_BOOL
        ]
        if not indexes:
            column = self._bool_column()
            if column < 0:
                return 0
            indexes = [
                self._index(row, column)
                for row in sorted({i.row() for i in self._selected_indexes()})
            ]
        return self.toggle_cells(indexes, checked=checked)

    def set_paired_ground_for_selection(self, gnet: str) -> int:
        """"Set paired ground…": write *gnet* into the ground column of every
        selected row (``paired_gnd`` on the Net tab, ``gnet`` on VRM/Sink)."""
        model = self.config_model()
        if model is None:
            return 0
        column = model.column_index("paired_gnd")
        if column < 0:
            column = model.column_index("gnet")
        if column < 0:
            return 0
        changes: list[tuple[Any, ...]] = []
        for row in sorted({index.row() for index in self._selected_indexes()}):
            index = self._index(row, column)
            if not index.isValid() or not self._is_editable(index):
                continue
            ok, parsed = model.parse_value(index, gnet)
            if not ok:
                continue
            changes.append((index, self._read(index), parsed, self._is_override(index)))
        count = self._push(changes, "Set paired ground")
        if count:
            self.pairedGroundRequested.emit(str(gnet))
        return count

    def current_net(self) -> str:
        model = self.config_model()
        current = self.currentIndex()
        if model is None or not current.isValid():
            return ""
        return model.net_for_row(source_index(current).row())

    def go_to_net(self) -> None:
        """"Go to net": `MainWindow` jumps the other tabs to the current row's net."""
        net = self.current_net()
        if net:
            self.goToNetRequested.emit(net)

    def _bool_column(self) -> int:
        model = self.config_model()
        if model is None:
            return -1
        for column, spec in enumerate(model.columns):
            if spec.kind == KIND_BOOL and spec.editable:
                return column
        return -1

    # -- dialogs ---------------------------------------------------------- #

    def _prompt_set_value(self) -> None:
        current = self.currentIndex()
        spec = self._spec(current.column()) if current.isValid() else None
        if spec is None:
            return
        text, ok = QInputDialog.getText(
            self, "Set value", f"{spec.title}:", text=str(self._read(current) or "")
        )
        if ok and text.strip():
            self.set_value_for_selection(text.strip())

    def _prompt_paired_ground(self) -> None:
        model = self.config_model()
        if model is None:
            return
        grounds = list(model.session.ground_nets())
        if not grounds:
            return
        gnet, ok = QInputDialog.getItem(self, "Set paired ground", "Ground net:", grounds, 0, False)
        if ok and gnet:
            self.set_paired_ground_for_selection(gnet)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        plain = not (
            modifiers
            & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
                | Qt.KeyboardModifier.ShiftModifier
            )
        )
        if control and key == Qt.Key.Key_C:
            self._copy_selection()
            event.accept()
            return
        if control and key == Qt.Key.Key_V:
            self._paste_clipboard()
            event.accept()
            return
        if control and key == Qt.Key.Key_D:
            self._fill_down()
            event.accept()
            return
        if control and key == Qt.Key.Key_R:
            self._fill_right()
            event.accept()
            return
        if plain and key == Qt.Key.Key_Space and self._toggle_selected_bools():
            event.accept()
            return
        super().keyPressEvent(event)

    def edit(  # type: ignore[override]
        self, index: QModelIndex, trigger: Any = None, event: QEvent | None = None
    ) -> Any:
        # `QAbstractItemView.edit(index)` (public slot) and the
        # `edit(index, trigger, event)` virtual both land here.
        if trigger is None:
            self.note_pending_edit(index)
            return super().edit(index)
        if self._intercept_check_toggle(index, event):
            return True
        self.note_pending_edit(index)
        return super().edit(index, trigger, event)

    def _intercept_check_toggle(self, index: QModelIndex, event: QEvent | None) -> bool:
        """Route a checkbox mouse click through the undo stack.

        Qt toggles a check state inside `QStyledItemDelegate.editorEvent`, which
        this virtual dispatches to; taking the click here keeps *every* value
        change in the table undoable (design §C Ctrl+Z).
        """
        if event is None or event.type() != QEvent.Type.MouseButtonRelease:
            return False
        button = getattr(event, "button", None)
        if button is None or button() != Qt.MouseButton.LeftButton:
            return False
        spec = self._spec(index.column())
        if spec is None or spec.kind != KIND_BOOL or not self._is_editable(index):
            return False
        self.toggle_cells([index])
        self.update(index)
        return True

    def build_context_menu(self) -> QMenu:
        """design §C context menu (built separately so it stays testable).

        A `setExtraMenuBuilder` hook -- the Net Manager's *Classify* submenu --
        goes in first, so tab-specific entries sit at the top of the menu.
        """
        menu = QMenu(self)
        if self._extra_menu_builder is not None:
            self._extra_menu_builder(menu)
            if not menu.isEmpty():
                menu.addSeparator()
        entries: tuple[tuple[str, Any], ...] = (
            ("Set value…", self._prompt_set_value),
            ("Fill Down", self._fill_down),
            ("Fill Right", self._fill_right),
            (None, None),
            ("Copy", self._copy_selection),
            ("Paste", self._paste_clipboard),
            (None, None),
            ("Reset to auto", self.reset_selection_to_auto),
            ("Check selected", lambda: self.check_selection(True)),
            ("Uncheck selected", lambda: self.check_selection(False)),
            (None, None),
            ("Set paired ground…", self._prompt_paired_ground),
            ("Go to net", self.go_to_net),
        )
        for title, slot in entries:
            if title is None:
                menu.addSeparator()
                continue
            action = QAction(title, menu)
            action.triggered.connect(slot)
            menu.addAction(action)
        return menu

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """Set value... / Fill Down / Fill Right / Copy / Paste / Reset to auto /
        Check-Uncheck selected / Set paired ground... / Go to net."""
        index = self.indexAt(event.pos())
        selection = self.selectionModel()
        if index.isValid() and selection is not None and not selection.isSelected(index):
            self.setCurrentIndex(index)
            selection.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.build_context_menu().exec(event.globalPos())
