"""Excel round-trip of the VRM and Sink tables (v0.1.4) -- Qt-free, openpyxl.

`export_tables` writes one workbook with a **VRMs** sheet, a **Sinks** sheet and
a hidden **_meta** sheet; `import_tables` reads an edited copy back and reports
what it would change, without touching the `Session`. The caller applies the
report only when it carries no errors (`ui/main_window.py` does that as a single
undoable command, so one Ctrl+Z takes the whole import back).

What may change in the file, and what may not
---------------------------------------------
The user is expected to **reorder rows** freely (sort by voltage, group by
component, drag rows around) and to **edit values**. Rows are therefore matched
by the ``Key`` column -- the `Session` row key, ``vrm:<net>#<row_id>`` /
``sink:<net>#<row_id>`` -- and never by position, so the order in the workbook
is irrelevant; a row the file no longer carries is simply left unchanged.

What may *not* change is the identity of a row and the shape of the table:

* the ``Key`` cell (renaming a channel by hand is the mistake this guards),
* the ``Net`` cell, cross-checked against the key's own net,
* the set of columns -- every column of the GUI table must still be present,
  matched by *title*, in any order; unknown extra columns are ignored.

Anything else that fails to read as a value of its column's kind (a word in a
voltage cell, a negative voltage, a component that is not a scanned circuit, a
ground net that is not classified ground) is a blocking error too. Import is
**all-or-nothing**: a single error means nothing is applied.

Why the sheets are not protected
--------------------------------
Locking the Key/Net cells with Excel's sheet protection was the obvious way to
make tampering impossible in the first place -- but a protected sheet also
forbids moving, inserting and deleting rows, which is exactly the freedom this
feature exists to give. Protection stays off and the validation above carries
the integrity guarantee instead (see `_write_sheet`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from powerdc_setup_tool import __version__
from powerdc_setup_tool.core.columns import (
    KIND_BOOL,
    KIND_CURRENT,
    KIND_ENUM,
    KIND_INT,
    KIND_VOLTAGE,
    SINK_COLUMNS,
    VRM_COLUMNS,
    ColumnDef,
)
from powerdc_setup_tool.core.model import SinkConfig, VrmConfig
from powerdc_setup_tool.core.session import ChangedKey, Session, sink_key, split_key, vrm_key

__all__ = [
    "ImportReport",
    "SCHEMA_VERSION",
    "SHEET_VRM",
    "SHEET_SINK",
    "SHEET_META",
    "KEY_HEADER",
    "apply_report",
    "export_tables",
    "import_tables",
]

#: `_meta` payload version. Bumped when the sheet layout stops being readable
#: by this module; `import_tables` refuses a workbook that carries another one.
SCHEMA_VERSION = 1

SHEET_VRM = "VRMs"
SHEET_SINK = "Sinks"
SHEET_META = "_meta"

#: Header of the row-identity column -- column A of both table sheets.
KEY_HEADER = "Key"

#: Cell text accepted for a `KIND_BOOL` column (mirrors `ui/models.py`).
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "x", "checked", "use"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "-", "unchecked"})

_NUMBER_FORMATS = {KIND_VOLTAGE: "0.####", KIND_CURRENT: "0.####", KIND_INT: "0"}

_HEADER_FILL = PatternFill("solid", fgColor="DDE3EA")
_HEADER_FONT = Font(bold=True)
_READONLY_FONT = Font(italic=True, color="808080")

_MIN_WIDTH = 9
_MAX_WIDTH = 44

# `split_key` kind -> (sheet name, column manifest, key builder).
_KIND_VRM = "vrm"
_KIND_SINK = "sink"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class ImportReport:
    """What `import_tables` found: the pending edits, plus why it may not apply.

    `applied_changes` is named for what the caller does with it: a list of
    ``(row_key, field, old, new)`` tuples ready to be pushed through
    `Session.set_field` (or, in the GUI, through one `BulkEditCommand` per
    table). It is empty when the workbook matches the session cell for cell.

    `errors` block the import outright -- every one of them names the sheet and
    the cell it came from -- and a report that carries any leaves
    `applied_changes` empty, so "all-or-nothing" holds even for a caller that
    forgets to check. `warnings` never block: a session row the workbook no
    longer carries, an edit to a read-only column, an extra sheet or column.
    """

    applied_changes: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the report may be applied (design: all-or-nothing)."""
        return not self.errors

    @property
    def cell_count(self) -> int:
        return len(self.applied_changes)

    @property
    def row_keys(self) -> list[str]:
        """Row keys touched, first-seen order (a row can carry several edits)."""
        return list(dict.fromkeys(row_key for row_key, _f, _o, _n in self.applied_changes))

    @property
    def row_count(self) -> int:
        return len(self.row_keys)

    @property
    def vrm_row_count(self) -> int:
        return sum(1 for key in self.row_keys if split_key(key)[0] == _KIND_VRM)

    @property
    def sink_row_count(self) -> int:
        return sum(1 for key in self.row_keys if split_key(key)[0] == _KIND_SINK)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def export_tables(
    session: Session,
    path: str | Path,
    *,
    exported_at: str = "",
    app_version: str = __version__,
) -> Path:
    """Write *session*'s VRM and Sink tables to the .xlsx at *path*.

    *exported_at* is passed in by the caller rather than read from the clock, so
    a test can produce a byte-stable workbook; the GUI passes a local timestamp.
    """
    workbook = Workbook()
    workbook.remove(workbook.active)  # drop the default "Sheet"
    _write_sheet(workbook, SHEET_VRM, VRM_COLUMNS, session, session.vrm_rows, vrm_key)
    _write_sheet(workbook, SHEET_SINK, SINK_COLUMNS, session, session.sink_rows, sink_key)
    _write_meta(workbook, session, exported_at=exported_at, app_version=app_version)
    target = Path(path)
    workbook.save(target)
    return target


def _write_sheet(
    workbook: Workbook,
    title: str,
    defs: Sequence[ColumnDef],
    session: Session,
    rows: Sequence[VrmConfig | SinkConfig],
    key_of: Callable[[str, int], str],
) -> Worksheet:
    """One table sheet: ``Key`` then the GUI columns, header frozen and bold.

    Deliberately **unprotected**: Excel refuses to move, insert or delete rows
    on a protected sheet, and reordering rows is the whole point of the round
    trip. `import_tables` re-checks the Key and Net cells instead.
    """
    sheet = workbook.create_sheet(title)
    headers = [KEY_HEADER, *(column.title for column in defs)]
    sheet.append(headers)
    widths = [len(header) for header in headers]

    names = session.display_names()
    for cfg in rows:
        row_key = key_of(cfg.net, cfg.row_id)
        values: list[Any] = [row_key]
        values.extend(_cell_value(session, cfg, row_key, column, names) for column in defs)
        sheet.append(values)
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(_as_text(value)))

    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = min(
            _MAX_WIDTH, max(_MIN_WIDTH, widths[index - 1] + 2)
        )
        column = None if index == 1 else defs[index - 2]
        number_format = "" if column is None else _NUMBER_FORMATS.get(column.kind, "")
        read_only = column is None or not column.editable
        if not number_format and not read_only:
            continue
        for row_index in range(2, sheet.max_row + 1):
            body = sheet.cell(row=row_index, column=index)
            if number_format:
                body.number_format = number_format
            if read_only:
                # Reference columns: greyed so an edit here reads as a mistake
                # (import ignores them, and reports the edit as a warning).
                body.font = _READONLY_FONT

    sheet.freeze_panes = "A2"
    if sheet.max_row > 1:
        sheet.auto_filter.ref = sheet.dimensions
    return sheet


def _write_meta(
    workbook: Workbook, session: Session, *, exported_at: str, app_version: str
) -> Worksheet:
    sheet = workbook.create_sheet(SHEET_META)
    source = str(session.scan.path.name) if session.scan is not None else ""
    for key, value in (
        ("schema", SCHEMA_VERSION),
        ("app_version", app_version),
        ("source", source),
        ("exported_at", exported_at),
        ("vrm_rows", len(session.vrm_rows)),
        ("sink_rows", len(session.sink_rows)),
    ):
        sheet.append([key, value])
    sheet.column_dimensions["A"].width = 14
    sheet.column_dimensions["B"].width = 40
    sheet.sheet_state = "hidden"
    return sheet


def _cell_value(
    session: Session,
    cfg: VrmConfig | SinkConfig,
    row_key: str,
    column: ColumnDef,
    names: dict[str, str],
) -> Any:
    if column.key == "pos_pins":
        return len(session.positive_pins(cfg))
    if column.key == "neg_pins":
        return len(session.negative_pins(cfg))
    if column.key == "name":
        return names.get(row_key, "")
    return getattr(cfg, column.key, "")


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def import_tables(session: Session, path: str | Path) -> ImportReport:
    """Read the workbook at *path* against *session*; never mutates the session.

    Returns the full picture in one pass -- every error, not just the first --
    so a dialog can list them all. Apply with `apply_report` (or, in the GUI,
    through the undo stack) only when `ImportReport.ok`.
    """
    report = ImportReport()
    try:
        workbook = load_workbook(Path(path), data_only=True)
    except OSError:
        raise
    except Exception as exc:  # openpyxl raises a zoo of parse errors
        report.errors.append(f"{Path(path).name} is not a readable .xlsx workbook: {exc}")
        return report

    try:
        if not _check_meta(workbook, report):
            return report
        seen: dict[str, str] = {}
        for title, defs, kind in (
            (SHEET_VRM, VRM_COLUMNS, _KIND_VRM),
            (SHEET_SINK, SINK_COLUMNS, _KIND_SINK),
        ):
            if title not in workbook.sheetnames:
                report.errors.append(f"the workbook has no {title!r} sheet")
                continue
            _read_sheet(session, workbook[title], defs, kind, report, seen)
        extra = [
            title
            for title in workbook.sheetnames
            if title not in (SHEET_VRM, SHEET_SINK, SHEET_META)
        ]
        if extra:
            report.warnings.append(f"ignored extra sheet(s): {_listed(extra)}")
        _warn_about_missing_rows(session, seen, report)
    finally:
        workbook.close()
    if report.errors:
        # All-or-nothing: a report that must not be applied does not get to
        # carry a half-list of "the good edits" a caller might apply anyway.
        report.applied_changes.clear()
    return report


def apply_report(session: Session, report: ImportReport) -> list[ChangedKey]:
    """Push a clean *report* into *session* -- the headless counterpart of the
    GUI's undoable apply. Raises `ValueError` on a report that carries errors."""
    if report.errors:
        raise ValueError("refusing to apply an ImportReport with errors")
    changed: list[ChangedKey] = []
    for row_key, name, _old, new in report.applied_changes:
        changed.extend(session.set_field(row_key, name, new))
    return changed


def _check_meta(workbook: Workbook, report: ImportReport) -> bool:
    """False when the workbook's schema is one this version cannot read."""
    if SHEET_META not in workbook.sheetnames:
        report.warnings.append(
            f"the workbook carries no {SHEET_META!r} sheet -- "
            f"assuming schema {SCHEMA_VERSION}"
        )
        return True
    meta = {
        str(row[0]).strip(): (row[1] if len(row) > 1 else None)
        for row in workbook[SHEET_META].iter_rows(values_only=True)
        if row and row[0] is not None
    }
    raw = meta.get("schema")
    if raw is None:
        report.warnings.append(
            f"{SHEET_META}: no schema entry -- assuming schema {SCHEMA_VERSION}"
        )
        return True
    try:
        schema = int(str(raw).strip())
    except (TypeError, ValueError):
        schema = -1
    if schema != SCHEMA_VERSION:
        report.errors.append(
            f"{SHEET_META}: workbook schema {_as_text(raw)!r} cannot be read by this "
            f"version (expected {SCHEMA_VERSION}) -- export the tables again"
        )
        return False
    return True


def _read_sheet(
    session: Session,
    sheet: Worksheet,
    defs: Sequence[ColumnDef],
    kind: str,
    report: ImportReport,
    seen: dict[str, str],
) -> None:
    columns = _header_map(sheet, defs, report)
    if columns is None:
        return
    circuits = set(session.circuit_names())
    grounds = set(session.ground_nets())
    names = session.display_names()  # O(rows); hoisted out of the cell loop

    for row_index in range(2, sheet.max_row + 1):
        cells = {title: sheet.cell(row=row_index, column=col) for title, col in columns.items()}
        if all(_is_blank(cell.value) for cell in cells.values()):
            continue
        row_key = _row_key_of(sheet, cells[KEY_HEADER], kind, session, report, seen)
        if row_key is None:
            continue
        cfg = session.row(row_key)
        if cfg is None:  # `_row_key_of` already rejects these; belt and braces
            continue
        for column in defs:
            cell = cells[column.title]
            if not column.editable:
                _check_reference_cell(session, sheet, cell, column, cfg, row_key, names, report)
                continue
            parsed = _parse_cell(cell, column, circuits, grounds, cfg, sheet, report)
            if parsed is _INVALID:
                continue
            current = getattr(cfg, column.key)
            if not _same_value(current, parsed):
                report.applied_changes.append((row_key, column.key, current, parsed))


def _header_map(
    sheet: Worksheet, defs: Sequence[ColumnDef], report: ImportReport
) -> dict[str, int] | None:
    """``title -> column index`` for the required columns, order-insensitive."""
    found: dict[str, int] = {}
    duplicates: list[str] = []
    unknown: list[str] = []
    required = {KEY_HEADER, *(column.title for column in defs)}
    for index in range(1, sheet.max_column + 1):
        title = _as_text(sheet.cell(row=1, column=index).value).strip()
        if not title:
            continue
        if title in found:
            duplicates.append(title)
            continue
        if title not in required:
            unknown.append(title)
            continue
        found[title] = index
    for title in duplicates:
        report.errors.append(f"{sheet.title}: column {title!r} appears more than once")
    if unknown:
        report.warnings.append(
            f"{sheet.title}: ignored unknown column(s): {_listed(dict.fromkeys(unknown))}"
        )
    missing = [title for title in (KEY_HEADER, *(c.title for c in defs)) if title not in found]
    if missing:
        report.errors.append(
            f"{sheet.title}: required column(s) missing: {_listed(missing)} -- "
            f"the column structure must not change"
        )
        return None
    return None if duplicates else found


def _row_key_of(
    sheet: Worksheet,
    cell: Any,
    kind: str,
    session: Session,
    report: ImportReport,
    seen: dict[str, str],
) -> str | None:
    where = _where(sheet, cell)
    row_key = _as_text(cell.value).strip()
    if not row_key:
        report.errors.append(f"{where}: row has no {KEY_HEADER} -- cannot tell which row it is")
        return None
    try:
        row_kind, _net, _row_id = split_key(row_key)
    except ValueError:
        report.errors.append(f"{where}: {row_key!r} is not a table row key")
        return None
    if row_key in seen:
        report.errors.append(f"{where}: duplicate {KEY_HEADER} {row_key!r} (also {seen[row_key]})")
        return None
    seen[row_key] = where
    if row_kind != kind:
        report.errors.append(f"{where}: {row_key!r} does not belong on the {sheet.title!r} sheet")
        return None
    if session.row(row_key) is None:
        report.errors.append(f"{where}: {row_key!r} is not a row of the loaded design")
        return None
    return row_key


def _check_reference_cell(
    session: Session,
    sheet: Worksheet,
    cell: Any,
    column: ColumnDef,
    cfg: VrmConfig | SinkConfig,
    row_key: str,
    names: dict[str, str],
    report: ImportReport,
) -> None:
    """Read-only columns. A changed ``Net`` is a blocking tamper; the rest are
    context, so an edit there is only reported and then ignored."""
    where = _where(sheet, cell)
    expected = _cell_value(session, cfg, row_key, column, names)
    if _as_text(cell.value).strip() == _as_text(expected).strip():
        return
    if column.key == "net":
        report.errors.append(
            f"{where}: {column.title} is {_as_text(cell.value)!r} but {row_key!r} is "
            f"{cfg.net!r} -- net (channel) names must not be edited"
        )
        return
    report.warnings.append(
        f"{where}: {column.title} is read-only (derived from the design) -- edit ignored"
    )


#: `_parse_cell` rejection sentinel (the error is already in the report).
_INVALID = object()


def _parse_cell(
    cell: Any,
    column: ColumnDef,
    circuits: set[str],
    grounds: set[str],
    cfg: VrmConfig | SinkConfig,
    sheet: Worksheet,
    report: ImportReport,
) -> Any:
    where = _where(sheet, cell)
    raw = cell.value
    current = getattr(cfg, column.key)

    if column.kind == KIND_BOOL:
        parsed = _parse_bool(raw)
        if parsed is None:
            report.errors.append(
                f"{where}: {column.title} must be TRUE/FALSE or 1/0, got {_as_text(raw)!r}"
            )
            return _INVALID
        return parsed

    if column.kind == KIND_ENUM:
        text = _as_text(raw).strip()
        allowed = circuits if column.key == "comp" else grounds
        if text == str(current) or text in allowed:
            return text
        what = "a circuit of the loaded design" if column.key == "comp" else "a ground net"
        report.errors.append(
            f"{where}: {column.title} {text!r} is not {what} "
            f"(allowed: {_listed(sorted(allowed)) or 'none'} or the row's own {current!r})"
        )
        return _INVALID

    if column.kind == KIND_INT:
        parsed_int = _parse_int(raw)
        if parsed_int is None:
            report.errors.append(
                f"{where}: {column.title} must be a whole number, got {_as_text(raw)!r}"
            )
            return _INVALID
        if parsed_int < 0:
            report.errors.append(f"{where}: {column.title} must not be negative ({parsed_int})")
            return _INVALID
        return parsed_int

    number = _parse_float(raw)
    if number is None:
        report.errors.append(f"{where}: {column.title} must be a number, got {_as_text(raw)!r}")
        return _INVALID
    if column.kind == KIND_VOLTAGE and number <= 0:
        # `Session.validate()` blocks an export on a non-positive voltage; catch
        # it here, where the offending cell can still be named.
        report.errors.append(f"{where}: {column.title} must be greater than 0 ({_num(number)})")
        return _INVALID
    if column.kind == KIND_CURRENT and number < 0:
        report.errors.append(f"{where}: {column.title} must not be negative ({_num(number)})")
        return _INVALID
    return number


def _warn_about_missing_rows(session: Session, seen: dict[str, str], report: ImportReport) -> None:
    missing = [
        key_of(row.net, row.row_id)
        for rows, key_of in ((session.vrm_rows, vrm_key), (session.sink_rows, sink_key))
        for row in rows
        if key_of(row.net, row.row_id) not in seen
    ]
    if missing:
        report.warnings.append(
            f"{len(missing)} row(s) of the session are absent from the workbook and "
            f"were left unchanged: {_listed(missing)}"
        )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _where(sheet: Worksheet, cell: Any) -> str:
    """``"VRMs!C7"`` -- the sheet and cell an error came from."""
    return f"{sheet.title}!{get_column_letter(cell.column)}{cell.row}"


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return _num(value)
    return str(value)


def _num(value: float) -> str:
    """``1.0`` -> ``"1"``, ``0.7`` -> ``"0.7"`` (the tables' own number style)."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value) in (0.0, 1.0):
            return bool(value)
        return None
    text = _as_text(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _parse_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = _as_text(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return None if math.isnan(number) or math.isinf(number) else number


def _parse_int(value: Any) -> int | None:
    number = _parse_float(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _same_value(current: Any, parsed: Any) -> bool:
    """Cell equals what the session already holds -- skip it (no override flip)."""
    if isinstance(current, bool) or isinstance(parsed, bool):
        return bool(current) is bool(parsed)
    if isinstance(current, (int, float)) and isinstance(parsed, (int, float)):
        return float(current) == float(parsed)
    return current == parsed


def _listed(items: Iterable[Any], limit: int = 8) -> str:
    values = [_as_text(item) for item in items]
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit} more)"
