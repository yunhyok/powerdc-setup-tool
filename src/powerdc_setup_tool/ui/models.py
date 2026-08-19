"""Qt table models -- owned by chunk 4 (design §A/§C).

Column metadata (`kind`, editable, getter/setter, enum choices, override-flag
accessor); `setData` writes through to `Session`; override styling per design
§B's propagation rule (italic + accent foreground + tooltip carrying the auto
value).

Design §B says *"`Session` mutators return the list of changed ``(row, col)``
keys so `BaseConfigModel` can emit one batched `dataChanged` per bulk
operation"*. `Session` returns stable ``(row_key, field)`` pairs instead (it
cannot know table geometry); `BaseConfigModel.apply_changed_keys` is the
translator: it resolves each key to ``(row, column)`` and emits **one**
`dataChanged` per touched row spanning that row's changed column range.

A `Session` edit can cascade across tabs (a net-voltage edit moves every
non-overridden `VrmConfig.nominal_voltage`), so every model built on the same
`Session` is registered in a weak per-session registry and receives the changed
keys of every other model's write. No `MainWindow` wiring is needed for the
cascade to repaint; chunk 5 can still call `apply_changed_keys` by hand after
mutating the session directly.

A mutator that adds or removes rows bumps `Session.structure_version`; seeing
it move -- or the row count move, the belt-and-braces check kept for sessions
mutated outside the mutator API -- forces a full `refresh_from_session` reset
instead of a cell patch.

v0.1.4
------
* **`VrmTableModel`/`SinkTableModel` columns are built from `core/columns.py`**
  (`_specs`): key, title, kind and editability come from the Qt-free manifest
  that `core/xlsx_io.py` writes the spreadsheet from, so the GUI table and the
  Excel round trip cannot drift apart. Everything Qt-only -- the derived
  columns' getters, the combo choices, the ``*_override`` wiring -- is layered
  on here. The kind constants are re-exported from that module too.

v0.1.2
------
* **Header-click sorting** (`ConfigSortProxy`, which `NetFilterProxy` now
  subclasses). It sorts on `SORT_ROLE`, i.e. the raw `ColumnSpec` value, so
  *Voltage (V)* / *Die* / *Pins @…* order numerically rather than as text.
* **Paired GND / Ground choices** come from `_ground_choices`, which always
  carries a blank entry plus the row's own value, so the combo is never the
  empty box v0.1.1 showed on a session with nothing classified ground yet.
* **`ColumnSpec.tooltip`** -- static header/cell help text; the *Source* column
  uses it, and it now reports `Session.class_source` (``input``/``auto``/
  ``user``/``""``) instead of the old ``input``/``new`` split of `from_input`.
"""

from __future__ import annotations

import fnmatch
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractProxyModel,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QFont

from powerdc_setup_tool.core import naming
from powerdc_setup_tool.core.columns import (
    KIND_BOOL,
    KIND_CURRENT,
    KIND_ENUM,
    KIND_INT,
    KIND_TEXT,
    KIND_VOLTAGE,
    NUMERIC_KINDS,
    SINK_COLUMNS,
    VRM_COLUMNS,
    ColumnDef,
)
from powerdc_setup_tool.core.model import PowerNetConfig, SinkConfig, VrmConfig
from powerdc_setup_tool.core.session import (
    DEFAULT_CURRENT,
    DEFAULT_VOLTAGE,
    ChangedKey,
    Session,
    net_key,
    sink_key,
    split_key,
    vrm_key,
)

__all__ = [
    "ColumnSpec",
    "BaseConfigModel",
    "NetTableModel",
    "VrmTableModel",
    "SinkTableModel",
    "ConfigSortProxy",
    "NetFilterProxy",
    "SORT_ROLE",
    "KIND_BOOL",
    "KIND_TEXT",
    "KIND_ENUM",
    "KIND_VOLTAGE",
    "KIND_CURRENT",
    "KIND_INT",
    "NUMERIC_KINDS",
    "OVERRIDE_COLOR",
    "ERROR_BACKGROUND",
    "config_model",
    "source_index",
    "format_number",
]

# --------------------------------------------------------------------------- #
# Column kinds (design §C "kind" column of the three table specs)
# --------------------------------------------------------------------------- #

# v0.1.4: the kinds and the VRM/Sink column manifest live in `core/columns.py`
# so the Excel round trip writes the very columns these tables show. Re-exported
# here because `ui.models.KIND_*` is what the delegates and tests import.

#: Unit shown in the override tooltip ("auto value: 0.75 V"). Cell text itself
#: stays unit-free -- the header carries the unit and TSV copy/paste round-trips.
_UNITS = {KIND_VOLTAGE: " V", KIND_CURRENT: " A"}

#: design §B "overridden cell = italic + accent foreground".
OVERRIDE_COLOR = QColor(0x0B, 0x57, 0xD0)
#: design §G.4 "row shows 0 pins, red background".
ERROR_BACKGROUND = QColor(0xFF, 0xE1, 0xE1)

_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "x", "checked", "use"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "", "-", "unchecked"})

#: `setData`/`parse_value` rejection sentinel.
_INVALID = object()

_NET_CLASSES: tuple[str, ...] = ("power", "ground", "none")

#: v0.1.2 header-click sorting: the role `ConfigSortProxy` sorts on. It hands
#: back the **raw** `ColumnSpec` value -- a `float`/`int`/`bool`, not the
#: `DisplayRole` text -- so *Voltage (V)* orders 0.7 < 1.2 < 10 instead of
#: "0.7" < "1.2" < "10" landing "10" between "1.2" and "2". `EditRole` would
#: do for the editable columns, but the derived read-only ones (*Pins @VRM
#: comp*, *Die*, *Name*, *Source*) are exactly the ones a user sorts by.
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def format_number(value: Any) -> str:
    """``0.7`` -> ``"0.7"``, ``1.0`` -> ``"1"``, ``None`` -> ``""`` (spec §9 style)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def source_index(index: QModelIndex) -> QModelIndex:
    """Map *index* down through any chain of proxies to its source model."""
    model = index.model()
    while isinstance(model, QAbstractProxyModel):
        index = model.mapToSource(index)
        model = index.model()
    return index


def config_model(model: Any) -> BaseConfigModel | None:
    """The `BaseConfigModel` underneath *model* (which may be a proxy chain)."""
    while isinstance(model, QAbstractProxyModel):
        model = model.sourceModel()
    return model if isinstance(model, BaseConfigModel) else None


def _circuit_names(session: Session) -> list[str]:
    """Every scanned circuit -- the Component combo's model (design §G.3).

    `Session.circuit_names()` since v0.1.4, so the combo and the Excel import's
    Component check share one definition of "is that a real circuit?".
    """
    return session.circuit_names()


def _ground_choices(field: str) -> Callable[[Session, Any], list[str]]:
    """Choices provider for the Paired GND / Ground combos (v0.1.2 bug fix).

    `ComboDelegate` calls this **when it builds the editor**, so the list is
    whatever `Session.ground_nets()` says at that instant: a net classified
    ground a moment ago (right-click *Classify > as GroundNets*) is offered on
    the very next editor open, with no model rebuild.

    v0.1.1 handed `session.ground_nets()` straight to `QComboBox.addItems`,
    which made the editor *literally empty* whenever nothing was classified
    ground yet -- the state every freshly scanned, unclassified `.spd` used to
    land in, since nothing classified anything at load time. Two guards keep
    that from ever being a dead end again:

    * a leading blank entry, so the pairing can always be cleared, and so the
      combo has a selectable row even on a session with no grounds at all;
    * the row's own current value, so a ground that is no longer classified
      (a hand-edited config, a `.VRM` block naming a net the `.NetList` never
      grouped) still shows what the cell holds instead of reading as empty.
    """

    def choices(session: Session, row: Any) -> list[str]:
        grounds = list(session.ground_nets())
        current = str(getattr(row, field, "") or "")
        if current and current not in grounds:
            grounds.append(current)
        return ["", *grounds]

    return choices


def _auto_net_voltage(session: Session, cfg: PowerNetConfig) -> float:
    """The voltage `Session` would derive for *cfg* if the override were cleared."""
    derive = getattr(session, "_auto_net_voltage", None)
    if callable(derive):
        return float(derive(cfg))
    if cfg.net_class == "ground":
        return 0.0
    guessed = naming.guess_voltage(cfg.net)
    return DEFAULT_VOLTAGE if guessed is None else guessed


def _net_voltage(session: Session, row: VrmConfig | SinkConfig) -> float:
    """Effective net voltage backing this row's auto nominal (`Session._propagate`)."""
    cfg = session.nets.get(row.net)
    return cfg.voltage if cfg is not None else DEFAULT_VOLTAGE


def _auto_gnet(session: Session, row: VrmConfig | SinkConfig) -> str:
    cfg = session.nets.get(row.net)
    return cfg.paired_gnd if cfg is not None else ""


def _auto_vrm_comp(session: Session, row: VrmConfig) -> str:
    derive = getattr(session, "_default_vrm_comp", None)
    return derive(row.net, row.gnet) if callable(derive) else row.comp


def _auto_sink_comp(session: Session, row: SinkConfig) -> str:
    derive = getattr(session, "_default_sink_comp", None)
    if not callable(derive):
        return row.comp
    cfg = session.nets.get(row.net)
    return derive(row.net, cfg.die if cfg is not None else naming.die_of(row.net))


# --------------------------------------------------------------------------- #
# ColumnSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColumnSpec:
    """One table column's metadata (design §C column tables).

    *key* is the `Session` config attribute the column edits (``"voltage"``,
    ``"net_class"``, ...) or a synthetic name for a derived read-only column
    (``"pins_vrm"``, ``"name"``, ``"source"``). It doubles as the routing key
    for `Session`'s ``(row_key, field)`` change lists.
    """

    key: str
    title: str
    kind: str
    editable: bool = False
    #: ``(session, row_cfg, row_key) -> value``; default ``getattr(row, key)``.
    getter: Callable[[Session, Any, str], Any] | None = None
    #: ``(session, row_key, value) -> list[ChangedKey]``; default `Session.set_field`.
    setter: Callable[[Session, str, Any], list[ChangedKey]] | None = None
    #: ``(session, row_cfg) -> Sequence[str]`` for `KIND_ENUM` columns.
    choices: Callable[[Session, Any], Sequence[str]] | None = None
    #: name of the dataclass ``*_override`` flag backing this column (design §B).
    override_field: str | None = None
    #: True when the "user set this" bit lives in `Session._pinned` (comp/gnet).
    pinned: bool = False
    #: ``(session, row_cfg) -> value`` the session would derive automatically.
    auto_value: Callable[[Session, Any], Any] | None = None
    #: ``(session, row_cfg) -> bool``; e.g. Paired GND is editable on power rows only.
    enabled_for: Callable[[Session, Any], bool] | None = None
    #: optional overrides of the kind-based default text<->value conversion.
    parser: Callable[[str], Any] | None = None
    formatter: Callable[[Any], str] | None = None
    #: v0.1.2 static help text for the header and for cells with nothing else
    #: to say (an override tooltip and a validation issue both outrank it).
    tooltip: str = ""

    # -- the stub published `header`; `title` is the design §C wording -------- #
    @property
    def header(self) -> str:
        return self.title

    @property
    def resettable(self) -> bool:
        """True when this column has an auto value "Reset to auto" can restore."""
        return bool(self.override_field) or self.pinned

    # -- accessors ------------------------------------------------------- #

    def get(self, session: Session, row: Any, row_key: str) -> Any:
        if self.getter is not None:
            return self.getter(session, row, row_key)
        return getattr(row, self.key, None)

    def set(self, session: Session, row_key: str, value: Any) -> list[ChangedKey]:
        if self.setter is not None:
            return self.setter(session, row_key, value)
        return session.set_field(row_key, self.key, value)

    def choice_list(self, session: Session, row: Any) -> list[str]:
        if self.choices is None:
            return []
        return [str(choice) for choice in self.choices(session, row)]

    def is_override(self, session: Session, row: Any, row_key: str) -> bool:
        """design §B: has the user pinned this cell away from its auto value?"""
        if self.override_field:
            return bool(getattr(row, self.override_field, False))
        if self.pinned:
            return (row_key, self.key) in getattr(session, "_pinned", ())
        return False

    def auto(self, session: Session, row: Any) -> Any:
        if self.auto_value is None:
            return None
        return self.auto_value(session, row)

    def format(self, value: Any) -> str:
        if self.formatter is not None:
            return self.formatter(value)
        if value is None:
            return ""
        if self.kind == KIND_BOOL:
            return "1" if value else "0"
        if self.kind in NUMERIC_KINDS:
            return format_number(value)
        return str(value)


def _specs(
    defs: Sequence[ColumnDef], extras: dict[str, dict[str, Any]]
) -> tuple[ColumnSpec, ...]:
    """`core/columns.py` manifest -> `ColumnSpec`s, plus this table's Qt wiring.

    v0.1.4: key, title, kind and editability come from the manifest the Excel
    export/import reads, so a column can only ever be renamed or reordered in
    both places at once. *extras* adds what only the GUI has -- getters for the
    derived columns, combo choices, the ``*_override`` flag a cell styles on.
    """
    return tuple(
        ColumnSpec(
            column.key,
            column.title,
            column.kind,
            editable=column.editable,
            **extras.get(column.key, {}),
        )
        for column in defs
    )


# --------------------------------------------------------------------------- #
# BaseConfigModel
# --------------------------------------------------------------------------- #


class BaseConfigModel(QAbstractTableModel):
    """Shared plumbing for the three config tables; `setData` writes through to `Session`.

    Generic over a *row provider* (``session -> Sequence[config]``) and a
    `ColumnSpec` list, so chunk 5 can build ad-hoc tables from the same class.
    """

    #: ``"net"`` | ``"vrm"`` | ``"sink"`` -- selects the `Session` row-key scheme.
    ROW_KIND = "net"
    #: default `ColumnSpec` list (subclasses); may be overridden per instance.
    COLUMNS: tuple[ColumnSpec, ...] = ()
    #: column indices hidden behind design §C's *Show advanced* checkbox.
    ADVANCED_COLUMNS: tuple[int, ...] = ()

    _KEY_BUILDERS: dict[str, Callable[[Any], str]] = {
        "net": lambda row: net_key(row.net),
        "vrm": lambda row: vrm_key(row.net, row.row_id),
        "sink": lambda row: sink_key(row.net, row.row_id),
    }
    _ROW_PROVIDERS: dict[str, Callable[[Session], Sequence[Any]]] = {
        "net": lambda session: session.net_rows,
        "vrm": lambda session: session.vrm_rows,
        "sink": lambda session: session.sink_rows,
    }

    #: session -> live models built on it (drives the cross-tab cascade repaint).
    _registry: "weakref.WeakKeyDictionary[Session, weakref.WeakSet[BaseConfigModel]]" = (
        weakref.WeakKeyDictionary()
    )

    def __init__(
        self,
        session: Session,
        columns: Sequence[ColumnSpec] | None = None,
        parent: QObject | None = None,
        *,
        row_kind: str | None = None,
        rows_provider: Callable[[Session], Sequence[Any]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.row_kind = row_kind or self.ROW_KIND
        self.columns: tuple[ColumnSpec, ...] = tuple(
            columns if columns is not None else self.COLUMNS
        )
        self._rows_provider = rows_provider or self._ROW_PROVIDERS[self.row_kind]
        self._key_of_row = self._KEY_BUILDERS[self.row_kind]
        #: design §G.4 red row background; off for very large tables if needed.
        self.highlight_issues = True

        self._rows: list[Any] = []
        self._row_of_key: dict[str, int] = {}
        self._rows_of_net: dict[str, list[int]] = {}
        self._col_of_field: dict[str, int] = {}
        self._structure_version = -1
        self._issues: dict[str, list[str]] | None = None
        self._names: dict[str, str] | None = None

        self._build_field_map()
        self._reload()
        self._registry.setdefault(session, weakref.WeakSet()).add(self)

    # ------------------------------------------------------------------ #
    # Row / column bookkeeping
    # ------------------------------------------------------------------ #

    def _build_field_map(self) -> None:
        """`Session` field name -> column index (includes the ``*_override`` flags,
        so an override toggle repaints the value cell it styles)."""
        self._col_of_field = {}
        for col, spec in enumerate(self.columns):
            self._col_of_field.setdefault(spec.key, col)
            if spec.override_field:
                self._col_of_field.setdefault(spec.override_field, col)

    def _reload(self) -> None:
        self._rows = list(self._rows_provider(self.session))
        self._row_of_key = {}
        self._rows_of_net = {}
        for row, cfg in enumerate(self._rows):
            key = self._key_of_row(cfg)
            self._row_of_key[key] = row
            self._rows_of_net.setdefault(cfg.net, []).append(row)
        self._structure_version = self.session.structure_version
        self._invalidate_caches()

    def _invalidate_caches(self) -> None:
        self._issues = None
        self._names = None

    def refresh_from_session(self) -> None:
        """Full reset -- use after a structural change (`Session.structure_version`)."""
        self.beginResetModel()
        self._reload()
        self.endResetModel()

    def row_config(self, row: int) -> Any:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_key(self, row: int) -> str:
        cfg = self.row_config(row)
        return "" if cfg is None else self._key_of_row(cfg)

    def row_of_key(self, row_key: str) -> int:
        return self._row_of_key.get(row_key, -1)

    def net_for_row(self, row: int) -> str:
        cfg = self.row_config(row)
        return "" if cfg is None else cfg.net

    def index_for_key(self, row_key: str, column: int) -> QModelIndex:
        row = self.row_of_key(row_key)
        if row < 0 or not 0 <= column < len(self.columns):
            return QModelIndex()
        return self.index(row, column)

    def column_spec(self, column: int) -> ColumnSpec | None:
        if 0 <= column < len(self.columns):
            return self.columns[column]
        return None

    def column_index(self, key: str) -> int:
        for col, spec in enumerate(self.columns):
            if spec.key == key:
                return col
        return -1

    def _local(self, index: QModelIndex) -> QModelIndex:
        """Accept an index from this model *or* from a proxy on top of it."""
        if index.isValid() and index.model() is not self:
            return source_index(index)
        return index

    # ------------------------------------------------------------------ #
    # QAbstractTableModel
    # ------------------------------------------------------------------ #

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return section + 1 if role == Qt.ItemDataRole.DisplayRole else None
        spec = self.column_spec(section)
        if spec is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return spec.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return spec.tooltip or None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        spec = self.column_spec(index.column())
        if spec is None or not self.is_editable(index):
            return flags
        if spec.kind == KIND_BOOL:
            return flags | Qt.ItemFlag.ItemIsUserCheckable
        return flags | Qt.ItemFlag.ItemIsEditable

    def is_editable(self, index: QModelIndex) -> bool:
        """Editability per `ColumnSpec` (design §C: Paired GND on power rows only).

        The bulk-edit view tests this rather than `Qt.ItemIsEditable`, because a
        bool column is *checkable*, not *editable*, yet still a fill target.
        """
        index = self._local(index)
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None or not spec.editable:
            return False
        if spec.enabled_for is not None and not spec.enabled_for(self.session, cfg):
            return False
        return True

    def value_at(self, index: QModelIndex) -> Any:
        index = self._local(index)
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None:
            return None
        return spec.get(self.session, cfg, self._key_of_row(cfg))

    def is_override(self, index: QModelIndex) -> bool:
        """design §B: is this cell user-set (vs. auto-derived)?"""
        index = self._local(index)
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None:
            return False
        return spec.is_override(self.session, cfg, self._key_of_row(cfg))

    def cell_text(self, index: QModelIndex) -> str:
        """Clipboard/TSV text -- bools render as ``1``/``0`` so paste round-trips."""
        index = self._local(index)
        spec = self.column_spec(index.column())
        if spec is None:
            return ""
        return spec.format(self.value_at(index))

    def choices_for(self, index: QModelIndex) -> list[str]:
        index = self._local(index)
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None:
            return []
        return spec.choice_list(self.session, cfg)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None:
            return None
        key = self._key_of_row(cfg)

        if role == Qt.ItemDataRole.DisplayRole:
            if spec.kind == KIND_BOOL:
                return None  # the checkbox is the whole cell
            return spec.format(spec.get(self.session, cfg, key))

        if role == Qt.ItemDataRole.EditRole:
            return spec.get(self.session, cfg, key)

        if role == SORT_ROLE:
            # v0.1.2 header-click sorting: the raw value, so a numeric column
            # orders numerically. `EditRole` is the same value for the editable
            # columns; the read-only ones have no `EditRole` of their own.
            return spec.get(self.session, cfg, key)

        if role == Qt.ItemDataRole.CheckStateRole:
            if spec.kind != KIND_BOOL:
                return None
            checked = bool(spec.get(self.session, cfg, key))
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        overridden = spec.is_override(self.session, cfg, key)

        if role == Qt.ItemDataRole.FontRole:
            if overridden:
                font = QFont()
                font.setItalic(True)
                return font
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(OVERRIDE_COLOR) if overridden else None

        if role == Qt.ItemDataRole.ToolTipRole:
            if overridden:
                auto = spec.auto(self.session, cfg)
                if auto is not None:
                    unit = _UNITS.get(spec.kind, "")
                    return f"User-set (auto value: {spec.format(auto)}{unit})"
                return "User-set"
            problems = self._issue_map().get(cfg.net)
            if problems:
                return "\n".join(problems)
            return spec.tooltip or None

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.highlight_issues and cfg.net in self._issue_map():
                return QBrush(ERROR_BACKGROUND)
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if spec.kind in NUMERIC_KINDS:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if spec.kind == KIND_BOOL:
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        return None

    def setData(
        self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        if not index.isValid():
            return False
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None or not self.is_editable(index):
            return False

        if role == Qt.ItemDataRole.CheckStateRole:
            if spec.kind != KIND_BOOL:
                return False
            value = _check_state_is_checked(value)
        elif role != Qt.ItemDataRole.EditRole:
            return False

        parsed = self._parse(spec, cfg, value)
        if parsed is _INVALID:
            return False

        row_key = self._key_of_row(cfg)
        try:
            changed = spec.set(self.session, row_key, parsed)
        except (KeyError, ValueError):
            return False
        self.broadcast(changed or [(row_key, spec.key)])
        return True

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def parse_value(self, index: QModelIndex, raw: Any) -> tuple[bool, Any]:
        """``(ok, value)`` -- the conversion `setData` would apply to *raw*.

        The bulk-edit view uses it to drop cells a pasted token cannot fill
        *before* they reach the undo stack (design §C "skipped silently").
        """
        index = self._local(index)
        spec = self.column_spec(index.column())
        cfg = self.row_config(index.row())
        if spec is None or cfg is None:
            return False, None
        parsed = self._parse(spec, cfg, raw)
        return (False, None) if parsed is _INVALID else (True, parsed)

    def _parse(self, spec: ColumnSpec, cfg: Any, value: Any) -> Any:
        if spec.parser is not None and isinstance(value, str):
            try:
                return spec.parser(value)
            except (TypeError, ValueError):
                return _INVALID

        if spec.kind == KIND_BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in _TRUE_WORDS:
                return True
            if text in _FALSE_WORDS:
                return False
            return _INVALID

        if spec.kind in NUMERIC_KINDS:
            number = _parse_number(value)
            if number is None:
                return _INVALID
            return int(round(number)) if spec.kind == KIND_INT else number

        text = str(value).strip()
        if spec.kind == KIND_ENUM:
            allowed = spec.choice_list(self.session, cfg)
            if not text or not allowed:
                return text  # "" clears the cell; `Session` rejects it if it cannot
            for choice in allowed:
                if choice.lower() == text.lower():
                    return choice
            # A name that is not a choice is a typo, not a new net: rejecting it
            # here is what keeps a mistyped ground out of `Session`.
            return _INVALID
        return text

    # ------------------------------------------------------------------ #
    # Reset to auto (design §B `Session.reset_auto`)
    # ------------------------------------------------------------------ #

    def reset_auto(self, indexes: Iterable[QModelIndex]) -> bool:
        """Clear the override/pin of every resettable cell in *indexes*."""
        keys: list[ChangedKey] = []
        for index in indexes:
            index = self._local(index)
            spec = self.column_spec(index.column())
            cfg = self.row_config(index.row())
            if spec is None or cfg is None or not spec.resettable:
                continue
            keys.append((self._key_of_row(cfg), spec.key))
        if not keys:
            return False
        self.broadcast(self.session.reset_auto(keys))
        return True

    # ------------------------------------------------------------------ #
    # Change propagation (design §B batched `dataChanged`)
    # ------------------------------------------------------------------ #

    def siblings(self) -> list[BaseConfigModel]:
        """Every live model built on the same `Session` (this one included)."""
        return list(self._registry.get(self.session, ()))

    def broadcast(self, keys: Iterable[ChangedKey]) -> None:
        """Feed a `Session` changed-key list to every model on this session."""
        keys = list(keys)
        for model in self.siblings():
            model.apply_changed_keys(keys)

    def apply_changed_keys(self, keys: Iterable[ChangedKey]) -> None:
        """Translate `Session` ``(row_key, field)`` keys into batched `dataChanged`.

        One signal per touched row, spanning that row's min..max changed column.
        A key naming a row of another kind (the design §B cascade: a net edit
        moving VRM/Sink cells) repaints this model's whole row for that net --
        that is also what keeps derived columns such as *Pins @VRM comp* fresh.
        """
        self._invalidate_caches()
        if self.session.structure_version != self._structure_version:
            self.refresh_from_session()
            return
        if len(self._rows_provider(self.session)) != len(self._rows):
            # Every `Session` mutator that adds a row bumps `structure_version`
            # (including `_ensure_ground` since chunk 7), so this is now only
            # the safety net for a session mutated outside that API.
            self.refresh_from_session()
            return

        ranges: dict[int, tuple[int, int]] = {}

        def touch(row: int, col: int) -> None:
            low, high = ranges.get(row, (col, col))
            ranges[row] = (min(low, col), max(high, col))

        last_col = len(self.columns) - 1
        if last_col < 0:
            return
        for row_key, field in keys:
            row = self._row_of_key.get(row_key)
            if row is not None:
                col = self._col_of_field.get(field)
                if col is not None:
                    touch(row, col)
                continue
            # foreign row kind -> repaint this model's rows for the same net
            try:
                _kind, net, _row_id = split_key(row_key)
            except ValueError:
                continue
            for other in self._rows_of_net.get(net, ()):
                touch(other, 0)
                touch(other, last_col)

        for row, (low, high) in ranges.items():
            self.dataChanged.emit(self.index(row, low), self.index(row, high))

    # ------------------------------------------------------------------ #
    # Validation-driven styling / filtering
    # ------------------------------------------------------------------ #

    def _issue_map(self) -> dict[str, list[str]]:
        """net -> blocking `ValidationIssue` messages (cached per change batch)."""
        if self._issues is None:
            issues: dict[str, list[str]] = {}
            if self.highlight_issues:
                for issue in self.session.validate():
                    if not issue.blocking:
                        continue
                    for net in issue.nets:
                        issues.setdefault(net, []).append(issue.message)
            self._issues = issues
        return self._issues

    def row_has_error(self, row: int) -> bool:
        cfg = self.row_config(row)
        return cfg is not None and cfg.net in self._issue_map()

    def filter_text(self, row: int) -> str:
        """Haystack for `NetFilterProxy` -- the net name (design §C filter box)."""
        return self.net_for_row(row)

    def filter_class(self, row: int) -> str:
        """``"power"``/``"ground"``/``"none"`` of the row's net."""
        cfg = self.row_config(row)
        if cfg is None:
            return "none"
        if isinstance(cfg, PowerNetConfig):
            return cfg.net_class
        net = self.session.nets.get(cfg.net)
        return net.net_class if net is not None else "none"

    def row_is_selected(self, row: int) -> bool:
        """The row's "Use" state (`PowerNetConfig.selected` / ``*Config.enabled``)."""
        cfg = self.row_config(row)
        if cfg is None:
            return False
        return bool(getattr(cfg, "selected", getattr(cfg, "enabled", False)))


def _check_state_is_checked(value: Any) -> bool:
    if isinstance(value, Qt.CheckState):
        return value == Qt.CheckState.Checked
    try:
        return Qt.CheckState(int(value)) == Qt.CheckState.Checked
    except (TypeError, ValueError):
        return bool(value)


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    # design §C NumericDelegate "formats trailing units off": accept "0.75 V".
    while text and text[-1].isalpha():
        text = text[:-1]
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Concrete models (design §C column tables)
# --------------------------------------------------------------------------- #


def _pins_at(which: int) -> Callable[[Session, Any, str], int]:
    def getter(session: Session, cfg: Any, _key: str) -> int:
        return session.net_pin_counts(cfg.net)[which]

    return getter


class NetTableModel(BaseConfigModel):
    """design §C Net Manager table (columns 0-8: Use ... Source)."""

    ROW_KIND = "net"
    COLUMNS = (
        ColumnSpec("selected", "Use", KIND_BOOL, editable=True),
        ColumnSpec(
            "net", "Net", KIND_TEXT, getter=lambda _s, cfg, _k: cfg.net
        ),
        ColumnSpec(
            "net_class",
            "Class",
            KIND_ENUM,
            editable=True,
            choices=lambda _s, _cfg: _NET_CLASSES,
        ),
        ColumnSpec(
            "paired_gnd",
            "Paired GND",
            KIND_ENUM,
            editable=True,
            choices=_ground_choices("paired_gnd"),
            enabled_for=lambda _s, cfg: cfg.net_class == "power",
        ),
        ColumnSpec(
            "voltage",
            "Voltage (V)",
            KIND_VOLTAGE,
            editable=True,
            override_field="voltage_override",
            auto_value=_auto_net_voltage,
        ),
        ColumnSpec("die", "Die", KIND_INT),
        ColumnSpec("pins_vrm", "Pins @VRM comp", KIND_INT, getter=_pins_at(0)),
        ColumnSpec("pins_sink", "Pins @Sink comp", KIND_INT, getter=_pins_at(1)),
        ColumnSpec(
            "source",
            "Source",
            KIND_TEXT,
            getter=lambda session, cfg, _k: session.class_source(cfg.net),
            tooltip="Where this net's class came from",
        ),
    )

    def _build_field_map(self) -> None:
        super()._build_field_map()
        # design §C column 8 is derived state, not a field of its own: v0.1.2
        # reads `class_source` (0.1.1 read `from_input`), and both must repaint
        # it, since `apply_changed_keys` spans only the columns it is handed.
        source_column = self.column_index("source")
        self._col_of_field.setdefault("class_source", source_column)
        self._col_of_field.setdefault("from_input", source_column)


class VrmTableModel(BaseConfigModel):
    """design §C VRMs table (Name derived read-only, ``VRM_{comp}_{pnet}_{gnet}``)."""

    ROW_KIND = "vrm"
    COLUMNS = _specs(
        VRM_COLUMNS,
        {
            "net": dict(getter=lambda _s, cfg, _k: cfg.net),
            "comp": dict(
                choices=lambda session, _cfg: _circuit_names(session),
                pinned=True,
                auto_value=_auto_vrm_comp,
            ),
            "gnet": dict(
                choices=_ground_choices("gnet"),
                pinned=True,
                auto_value=_auto_gnet,
            ),
            "nominal_voltage": dict(
                override_field="nominal_override",
                auto_value=_net_voltage,
            ),
            "sense_voltage": dict(
                override_field="sense_override",
                # spec §9 `{v_sense}` = `{v_nom}` (see `Session`'s module docstring).
                auto_value=lambda _s, cfg: cfg.nominal_voltage,
            ),
            "output_current": dict(
                override_field="current_override",
                auto_value=lambda _s, _cfg: DEFAULT_CURRENT,
            ),
            "pos_pins": dict(getter=lambda session, cfg, _k: len(session.positive_pins(cfg))),
            "neg_pins": dict(getter=lambda session, cfg, _k: len(session.negative_pins(cfg))),
            "name": dict(getter=lambda session, _cfg, key: session.display_names().get(key, "")),
        },
    )


class SinkTableModel(BaseConfigModel):
    """design §C Sinks table; Model/PFMode/PinEqualCurrent are the advanced trio."""

    ROW_KIND = "sink"
    COLUMNS = _specs(
        SINK_COLUMNS,
        {
            "net": dict(getter=lambda _s, cfg, _k: cfg.net),
            "comp": dict(
                choices=lambda session, _cfg: _circuit_names(session),
                pinned=True,
                auto_value=_auto_sink_comp,
            ),
            "gnet": dict(
                choices=_ground_choices("gnet"),
                pinned=True,
                auto_value=_auto_gnet,
            ),
            "nominal_voltage": dict(
                override_field="nominal_override",
                auto_value=_net_voltage,
            ),
            "current": dict(
                override_field="current_override",
                auto_value=lambda _s, _cfg: DEFAULT_CURRENT,
            ),
            "pos_pins": dict(getter=lambda session, cfg, _k: len(session.positive_pins(cfg))),
            "neg_pins": dict(getter=lambda session, cfg, _k: len(session.negative_pins(cfg))),
            "name": dict(getter=lambda session, _cfg, key: session.display_names().get(key, "")),
        },
    )

    #: design §C: "hidden behind a *Show advanced* checkbox (defaults 2/2/1, §A3)".
    #: Derived from the keys so it cannot drift if a column is inserted.
    ADVANCED_COLUMNS = tuple(
        index
        for index, spec in enumerate(COLUMNS)
        if spec.key in ("model", "pf_mode", "pin_equal_current")
    )


# --------------------------------------------------------------------------- #
# Sort proxy (v0.1.2 header-click sorting) + filter proxy (design §C)
# --------------------------------------------------------------------------- #


class ConfigSortProxy(QSortFilterProxyModel):
    """Header-click sorting for the three config tables (v0.1.2).

    All three tables now sit behind one of these -- the Net Manager through
    `NetFilterProxy`, which subclasses it -- so a click on any header sorts the
    rows and `BulkEditTableView` sees a proxy in every tab. Nothing downstream
    has to care: a `BulkEditCommand` addresses cells by stable `Session` row key
    (see `ui/table_view.py`), so an edit, an undo and a redo all land on the row
    the user picked no matter how the view is ordered in between.

    Sorting is on `SORT_ROLE` (raw values, so numeric columns order
    numerically), and `lessThan` breaks ties on the *source* row, which keeps
    equal cells in netlist order instead of an arbitrary one and makes a sort
    reproducible between runs.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self.setSortRole(SORT_ROLE)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = left.data(self.sortRole())
        b = right.data(self.sortRole())
        if a is None or b is None:
            if a is None and b is None:
                return left.row() < right.row()
            return b is not None  # empties sort first, ascending
        try:
            if a == b:
                return left.row() < right.row()
            return bool(a < b)
        except TypeError:
            pass  # mixed types (a cleared numeric cell next to a filled one)
        text_a, text_b = str(a), str(b)
        if text_a == text_b:
            return left.row() < right.row()
        return text_a < text_b


class NetFilterProxy(ConfigSortProxy):
    """Space-separated AND terms, `*` glob, case-insensitive (design §C filter box),
    plus the class combo's All/Power/Ground/Unclassified/Selected/Errors modes."""

    MODE_ALL = "All"
    MODE_POWER = "Power"
    MODE_GROUND = "Ground"
    MODE_UNCLASSIFIED = "Unclassified"
    MODE_SELECTED = "Selected"
    MODE_ERRORS = "Errors"
    MODES: tuple[str, ...] = (
        MODE_ALL,
        MODE_POWER,
        MODE_GROUND,
        MODE_UNCLASSIFIED,
        MODE_SELECTED,
        MODE_ERRORS,
    )

    _CLASS_OF_MODE = {
        MODE_POWER: "power",
        MODE_GROUND: "ground",
        MODE_UNCLASSIFIED: "none",
    }
    _GLOB_CHARS = "*?["

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)  # `ConfigSortProxy` sets the sort role
        self._terms: tuple[str, ...] = ()
        self._expression = ""
        self._mode = self.MODE_ALL

    # -- filter state ---------------------------------------------------- #

    def setFilterExpression(self, text: str) -> None:
        """``"vdd *_070_* /0"`` -> three AND-ed, case-insensitive terms."""
        expression = str(text or "")
        terms = tuple(term.lower() for term in expression.split() if term)
        if terms == self._terms and expression == self._expression:
            return
        self._expression = expression
        self._terms = terms
        self.invalidateRowsFilter()

    def filterExpression(self) -> str:
        return self._expression

    def setClassFilter(self, mode: str) -> None:
        """One of `MODES` (case-insensitive); unknown values fall back to *All*."""
        wanted = str(mode or self.MODE_ALL)
        for known in self.MODES:
            if known.lower() == wanted.lower():
                wanted = known
                break
        else:
            wanted = self.MODE_ALL
        if wanted == self._mode:
            return
        self._mode = wanted
        self.invalidateRowsFilter()

    def classFilter(self) -> str:
        return self._mode

    # -- counts for design §C's "12 / 3712 shown" label ------------------- #

    def shownCount(self) -> int:
        return self.rowCount()

    def totalCount(self) -> int:
        source = self.sourceModel()
        return source.rowCount() if source is not None else 0

    # -- QSortFilterProxyModel ------------------------------------------- #

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if source_parent.isValid():
            return False
        model = config_model(self.sourceModel())
        if model is None:
            return super().filterAcceptsRow(source_row, source_parent)

        if self._mode != self.MODE_ALL:
            wanted_class = self._CLASS_OF_MODE.get(self._mode)
            if wanted_class is not None:
                if model.filter_class(source_row) != wanted_class:
                    return False
            elif self._mode == self.MODE_SELECTED:
                if not model.row_is_selected(source_row):
                    return False
            elif self._mode == self.MODE_ERRORS:
                if not model.row_has_error(source_row):
                    return False

        if not self._terms:
            return True
        haystack = model.filter_text(source_row).lower()
        return all(self._matches(term, haystack) for term in self._terms)

    @classmethod
    def _matches(cls, term: str, haystack: str) -> bool:
        if any(char in term for char in cls._GLOB_CHARS):
            return fnmatch.fnmatchcase(haystack, term)
        return term in haystack
