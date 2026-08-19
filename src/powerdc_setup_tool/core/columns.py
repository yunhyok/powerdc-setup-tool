"""Column manifest of the VRM / Sink tables -- the one place their shape lives.

Qt-free by design: `ui/models.py` builds its `ColumnSpec` list from these
`ColumnDef`s (adding the Qt-side getters, combo choices and override wiring),
and `core/xlsx_io.py` writes and reads the Excel workbook from the same list.
A column added, renamed or reordered here therefore moves the GUI table *and*
the spreadsheet together -- which is what makes v0.1.4's "the column structure
must not change" import check meaningful: the titles the workbook is validated
against are the titles the GUI itself shows.

`key` is the `Session` config attribute (or a synthetic name for a derived
read-only column: ``pos_pins``/``neg_pins``/``name``); `editable` marks the
cells a user may change -- in the tables and in the spreadsheet alike.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ColumnDef",
    "KIND_BOOL",
    "KIND_TEXT",
    "KIND_ENUM",
    "KIND_VOLTAGE",
    "KIND_CURRENT",
    "KIND_INT",
    "NUMERIC_KINDS",
    "VRM_COLUMNS",
    "SINK_COLUMNS",
]

# --------------------------------------------------------------------------- #
# Column kinds (design §C "kind" column of the three table specs)
# --------------------------------------------------------------------------- #

KIND_BOOL = "bool"
KIND_TEXT = "text"
KIND_ENUM = "enum"
KIND_VOLTAGE = "voltage"
KIND_CURRENT = "current"
KIND_INT = "int"

#: Kinds a `NumericDelegate` edits and a numeric parser accepts.
NUMERIC_KINDS: tuple[str, ...] = (KIND_VOLTAGE, KIND_CURRENT, KIND_INT)


@dataclass(frozen=True)
class ColumnDef:
    """Key/title/kind/editable of one column -- everything both back ends need."""

    key: str
    title: str
    kind: str
    editable: bool = False


#: design §C VRMs table (Name is derived read-only, ``VRM_{comp}_{pnet}_{gnet}``).
VRM_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("enabled", "Use", KIND_BOOL, editable=True),
    ColumnDef("net", "Net", KIND_TEXT),
    ColumnDef("comp", "Component", KIND_ENUM, editable=True),
    ColumnDef("gnet", "Ground", KIND_ENUM, editable=True),
    ColumnDef("nominal_voltage", "Nominal V", KIND_VOLTAGE, editable=True),
    ColumnDef("sense_voltage", "Sense V", KIND_VOLTAGE, editable=True),
    ColumnDef("output_current", "Output Current (A)", KIND_CURRENT, editable=True),
    ColumnDef("pos_pins", "Pos pins", KIND_INT),
    ColumnDef("neg_pins", "Neg pins", KIND_INT),
    ColumnDef("name", "Name", KIND_TEXT),
)

#: design §C Sinks table; Model/PFMode/PinEqualCurrent are the advanced trio.
SINK_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("enabled", "Use", KIND_BOOL, editable=True),
    ColumnDef("net", "Net", KIND_TEXT),
    ColumnDef("comp", "Component", KIND_ENUM, editable=True),
    ColumnDef("gnet", "Ground", KIND_ENUM, editable=True),
    ColumnDef("nominal_voltage", "Nominal V", KIND_VOLTAGE, editable=True),
    ColumnDef("current", "Current (A)", KIND_CURRENT, editable=True),
    ColumnDef("model", "Model", KIND_INT, editable=True),
    ColumnDef("pf_mode", "PFMode", KIND_INT, editable=True),
    ColumnDef("pin_equal_current", "PinEqualCurrent", KIND_INT, editable=True),
    ColumnDef("pos_pins", "Pos pins", KIND_INT),
    ColumnDef("neg_pins", "Neg pins", KIND_INT),
    ColumnDef("name", "Name", KIND_TEXT),
)
