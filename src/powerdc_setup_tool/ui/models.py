"""Qt table models -- owned by chunk 4 (design §A/§C).

Column metadata (`kind`, editable, parser, formatter); `setData` writes
through to `Session`; override styling per design §B's propagation rule
(italic + accent foreground + bold left border for user-set cells).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt

from powerdc_setup_tool.core.session import Session


@dataclass(frozen=True)
class ColumnSpec:
    """One table column's metadata (design §C column tables)."""

    key: str
    header: str
    kind: str  # e.g. "bool" | "text" | "enum" | "voltage" | "current" | "int"
    editable: bool = False
    parser: Callable[[str], Any] | None = None
    formatter: Callable[[Any], str] | None = None


class BaseConfigModel(QAbstractTableModel):
    """Shared plumbing for the three config tables; `setData` writes through to `Session`."""

    def __init__(
        self,
        session: Session,
        columns: tuple[ColumnSpec, ...],
        parent: QObject | None = None,
    ) -> None:
        raise NotImplementedError

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        raise NotImplementedError

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        raise NotImplementedError

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        raise NotImplementedError

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        raise NotImplementedError

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        raise NotImplementedError

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        raise NotImplementedError


class NetTableModel(BaseConfigModel):
    """design §C Net Manager table (columns 0-8: Use ... Source)."""


class VrmTableModel(BaseConfigModel):
    """design §C VRMs table."""


class SinkTableModel(BaseConfigModel):
    """design §C Sinks table."""


class NetFilterProxy(QSortFilterProxyModel):
    """Space-separated AND terms, `*` glob, case-insensitive (design §C filter box)."""

    def setFilterExpression(self, text: str) -> None:
        raise NotImplementedError

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        raise NotImplementedError
