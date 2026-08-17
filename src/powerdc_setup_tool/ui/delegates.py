"""Cell editors -- owned by chunk 4 (design §C editors + bulk-apply signal).

Each delegate commits to `currentIndex`, then emits
`bulkApplyRequested(value, index)`; `BulkEditTableView` applies that value to
every selected cell whose `ColumnSpec.kind` matches (design §C type-to-fill).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Signal
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QWidget


class BulkDelegateMixin:
    """Shared "commit, then broadcast to the rest of the selection" behavior.

    Qt signals must live on a QObject-derived class, so the concrete
    delegates below (already `QStyledItemDelegate`/`QObject` subclasses) each
    declare their own `bulkApplyRequested` signal; this mixin holds the
    signal-independent logic shared between them.
    """

    def _emit_bulk_apply(self, editor: QWidget, index: QModelIndex) -> None:
        """Called after the editor commits; emits `bulkApplyRequested(value, index)`."""
        raise NotImplementedError


class NumericDelegate(BulkDelegateMixin, QStyledItemDelegate):
    """Spin-box editor for voltage/current/int columns."""

    bulkApplyRequested = Signal(object, QModelIndex)

    def __init__(self, parent: QObject | None = None) -> None:
        raise NotImplementedError

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        raise NotImplementedError

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        raise NotImplementedError

    def setModelData(self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex) -> None:
        raise NotImplementedError


class ComboDelegate(BulkDelegateMixin, QStyledItemDelegate):
    """Combo-box editor for enum columns (Class, Paired GND, Component)."""

    bulkApplyRequested = Signal(object, QModelIndex)

    def __init__(self, parent: QObject | None = None) -> None:
        raise NotImplementedError

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        raise NotImplementedError

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        raise NotImplementedError

    def setModelData(self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex) -> None:
        raise NotImplementedError
