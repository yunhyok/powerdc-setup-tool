"""Cell editors -- owned by chunk 4 (design §C editors + bulk-apply signal).

Each delegate commits to `currentIndex`, then emits
`bulkApplyRequested(value, index)`; `BulkEditTableView` applies that value to
every selected cell whose `ColumnSpec.kind` matches (design §C type-to-fill).

`NumericDelegate` is deliberately spin-box-free: a `QDoubleSpinBox` rounds to
its `decimals` and appends its `suffix` to the committed text, which loses
precision on values like ``0.055`` and breaks TSV round-tripping. A plain
`QLineEdit` guarded by a `QDoubleValidator` types exactly what the user means,
and the unit stays in the column header (design §C: "Voltage (V)").
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractItemModel, QLocale, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QLineEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from powerdc_setup_tool.ui.models import (
    KIND_ENUM,
    KIND_INT,
    NUMERIC_KINDS,
    ColumnSpec,
    config_model,
    format_number,
    source_index,
)

__all__ = ["BulkDelegateMixin", "NumericDelegate", "ComboDelegate", "delegate_for_kind"]

#: `QDoubleValidator` bounds -- wide enough to never reject a real rail/current.
_NUM_MIN = -1.0e12
_NUM_MAX = 1.0e12
_NUM_DECIMALS = 12


class BulkDelegateMixin:
    """Shared "commit, then broadcast to the rest of the selection" behavior.

    Qt signals must live on a QObject-derived class, so the concrete
    delegates below (already `QStyledItemDelegate`/`QObject` subclasses) each
    declare their own `bulkApplyRequested` signal; this mixin holds the
    signal-independent logic shared between them.
    """

    #: set by `setModelData` just before the commit, for the view's undo record.
    last_commit: tuple[int, int, Any] | None = None

    # -- helpers concrete delegates share --------------------------------- #

    @staticmethod
    def column_spec(index: QModelIndex) -> ColumnSpec | None:
        """The `ColumnSpec` behind *index* (unwrapping any proxy chain).

        Proxies in this app never reorder columns, so the proxy column index is
        also the source column index; `source_index` keeps that honest anyway.
        """
        model = config_model(index.model())
        if model is None:
            return None
        return model.column_spec(source_index(index).column())

    def editor_value(self, editor: QWidget) -> Any:
        """The value *editor* currently holds (overridden per editor type)."""
        raise NotImplementedError

    def _emit_bulk_apply(self, editor: QWidget, index: QModelIndex) -> None:
        """Called after the editor commits; emits `bulkApplyRequested(value, index)`."""
        if not index.isValid():
            return
        value = self.editor_value(editor)
        self.last_commit = (index.row(), index.column(), value)
        self.bulkApplyRequested.emit(value, index)  # type: ignore[attr-defined]


class NumericDelegate(BulkDelegateMixin, QStyledItemDelegate):
    """`QLineEdit` + `QDoubleValidator` editor for voltage/current/int columns."""

    bulkApplyRequested = Signal(object, QModelIndex)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        editor = QLineEdit(parent)
        editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        spec = self.column_spec(index)
        decimals = 0 if spec is not None and spec.kind == KIND_INT else _NUM_DECIMALS
        validator = QDoubleValidator(_NUM_MIN, _NUM_MAX, decimals, editor)
        # "0.7" must parse regardless of the machine's locale (a German locale
        # would otherwise demand "0,7" and silently reject the typed value).
        validator.setLocale(QLocale.c())
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        editor.setValidator(validator)
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        if isinstance(editor, QLineEdit):
            editor.setText(format_number(index.data(Qt.ItemDataRole.EditRole)))
            editor.selectAll()
            return
        super().setEditorData(editor, index)

    def editor_value(self, editor: QWidget) -> Any:
        return editor.text().strip() if isinstance(editor, QLineEdit) else None

    def setModelData(
        self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex
    ) -> None:
        text = self.editor_value(editor)
        if not text:
            return
        model.setData(index, text, Qt.ItemDataRole.EditRole)
        self._emit_bulk_apply(editor, index)


class ComboDelegate(BulkDelegateMixin, QStyledItemDelegate):
    """Combo-box editor for enum columns (Class, Paired GND, Component).

    Editable + `NoInsert` so a 92-entry ground list stays type-ahead friendly
    while still rejecting names that are not real choices (a typo'd ground net
    would otherwise reach `Session` and create a phantom net).
    """

    bulkApplyRequested = Signal(object, QModelIndex)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        editor = QComboBox(parent)
        editor.addItems(self.choices(index))
        editor.setEditable(True)
        editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = editor.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return editor

    @staticmethod
    def choices(index: QModelIndex) -> list[str]:
        """The `ColumnSpec` choices provider's list for *index*'s row."""
        model = config_model(index.model())
        if model is None:
            return []
        return model.choices_for(source_index(index))

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        if isinstance(editor, QComboBox):
            text = str(index.data(Qt.ItemDataRole.EditRole) or "")
            position = editor.findText(text, Qt.MatchFlag.MatchFixedString)
            if position >= 0:
                editor.setCurrentIndex(position)
            else:
                editor.setCurrentText(text)
            return
        super().setEditorData(editor, index)

    def editor_value(self, editor: QWidget) -> Any:
        return editor.currentText().strip() if isinstance(editor, QComboBox) else None

    def setModelData(
        self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex
    ) -> None:
        text = self.editor_value(editor)
        if text is None:
            return
        if not model.setData(index, text, Qt.ItemDataRole.EditRole):
            return  # not a valid choice -- leave the cell (and the selection) alone
        self._emit_bulk_apply(editor, index)


def delegate_for_kind(kind: str, parent: QObject | None = None):
    """The delegate design §C wants for a `ColumnSpec.kind` (``None`` for bool/text)."""
    if kind in NUMERIC_KINDS:
        return NumericDelegate(parent)
    if kind == KIND_ENUM:
        return ComboDelegate(parent)
    return None
