"""`ui/models.py` + `ui/table_view.py` + `ui/delegates.py` -- design §E
`test_models_bulk` row.

Covers the design §C column layouts of all three tables and the full
"Bulk-edit semantics" list (type-to-fill across a multi-cell/multi-column
same-kind selection, kind mismatch and read-only skipped, fill down/right, TSV
copy/paste with 1x1 and 1xN/Nx1 broadcast plus rectangular clipping,
space-toggle to the inverse of the anchor, one `QUndoCommand` per bulk
operation), the design §B override styling roles, the §C filter box/class combo,
and the §B propagation cascade arriving at the VRM table as `dataChanged`.

The models are deliberately thin: every assertion about *what* a value becomes
is really an assertion about `Session` (chunk 3), so these tests drive the real
`Session` built from a real `scan_spd` of the `style="si"` fixture.
"""

from __future__ import annotations

import os

# design §E: the offscreen platform must be selected before PySide6 is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from fixtures import (  # noqa: E402
    GROUND_NET,
    POWER_NET_A,
    POWER_NET_B,
    SENSE_NETS,
    UNCLASSIFIED_NETS,
    build_mini_spd,
)
from powerdc_setup_tool.core.model import PowerNetConfig  # noqa: E402
from powerdc_setup_tool.core.session import Session, net_key, vrm_key  # noqa: E402
from powerdc_setup_tool.core.spd_scan import scan_spd  # noqa: E402
from powerdc_setup_tool.ui.delegates import ComboDelegate, NumericDelegate  # noqa: E402
from powerdc_setup_tool.ui.models import (  # noqa: E402
    ERROR_BACKGROUND,
    KIND_BOOL,
    KIND_CURRENT,
    KIND_ENUM,
    KIND_INT,
    KIND_TEXT,
    KIND_VOLTAGE,
    OVERRIDE_COLOR,
    NetFilterProxy,
    NetTableModel,
    SinkTableModel,
    VrmTableModel,
)
from powerdc_setup_tool.ui.table_view import BulkEditTableView  # noqa: E402

# design §C column indices, named once so the tests read like the spec table.
NET_USE, NET_NET, NET_CLASS, NET_GND, NET_V, NET_DIE, NET_PVRM, NET_PSINK, NET_SRC = range(9)
VRM_USE, VRM_NET, VRM_COMP, VRM_GND, VRM_NOM, VRM_SENSE, VRM_CUR = range(7)
VRM_NAME = 9
SINK_NOM, SINK_CUR, SINK_MODEL = 4, 5, 6


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    """One `QApplication` for the whole module (Qt allows exactly one)."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path: Path) -> Session:
    """A real `Session` over a real scan of the `style="si"` miniature."""
    path = tmp_path / "mini.spd"
    build_mini_spd(path, style="si")
    loaded = Session()
    loaded.load(scan_spd(path))
    return loaded


def select(view: BulkEditTableView, top_left, bottom_right, current=None) -> None:
    """Select the rect and park the current cell without collapsing the selection.

    `QAbstractItemView.setCurrentIndex` issues a ClearAndSelect in
    `ExtendedSelection` mode, so the current cell has to be set through the
    selection model with `NoUpdate` -- exactly what a real Ctrl-drag leaves
    behind.
    """
    model = view.model()
    selection_model = view.selectionModel()
    selection_model.select(
        QItemSelection(model.index(*top_left), model.index(*bottom_right)),
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )
    selection_model.setCurrentIndex(
        model.index(*(current or top_left)), QItemSelectionModel.SelectionFlag.NoUpdate
    )


def texts(model, row: int, columns=None) -> list[str]:
    columns = range(model.columnCount()) if columns is None else columns
    return [model.cell_text(model.index(row, column)) for column in columns]


def row_of(model, net: str) -> int:
    for row in range(model.rowCount()):
        if model.net_for_row(row) == net:
            return row
    raise AssertionError(f"{net!r} is not in {type(model).__name__}")


def view_for(model) -> BulkEditTableView:
    view = BulkEditTableView()
    view.setModel(model)
    return view


# --------------------------------------------------------------------------- #
# column layouts (design §C)
# --------------------------------------------------------------------------- #


def test_net_table_columns_match_design(session: Session) -> None:
    model = NetTableModel(session)
    assert [spec.title for spec in model.columns] == [
        "Use",
        "Net",
        "Class",
        "Paired GND",
        "Voltage (V)",
        "Die",
        "Pins @VRM comp",
        "Pins @Sink comp",
        "Source",
    ]
    assert [spec.kind for spec in model.columns] == [
        KIND_BOOL,
        KIND_TEXT,
        KIND_ENUM,
        KIND_ENUM,
        KIND_VOLTAGE,
        KIND_INT,
        KIND_INT,
        KIND_INT,
        KIND_TEXT,
    ]
    assert [spec.editable for spec in model.columns] == [
        True, False, True, True, True, False, False, False, False
    ]
    assert model.headerData(NET_V, Qt.Orientation.Horizontal) == "Voltage (V)"
    assert model.columnCount() == 9
    assert model.rowCount() == len(session.net_rows)

    # design §C column 2/3 choice providers.
    power_row = row_of(model, POWER_NET_A)
    assert model.choices_for(model.index(power_row, NET_CLASS)) == ["power", "ground", "none"]
    assert model.choices_for(model.index(power_row, NET_GND)) == [GROUND_NET]
    # "Paired GND ... combo (power rows only)"
    assert model.is_editable(model.index(power_row, NET_GND))
    assert not model.is_editable(model.index(row_of(model, UNCLASSIFIED_NETS[0]), NET_GND))


def test_net_table_derived_columns(session: Session) -> None:
    model = NetTableModel(session)
    row = row_of(model, POWER_NET_A)
    assert texts(model, row) == [
        "1", POWER_NET_A, "power", GROUND_NET, "0.7", "0", "3", "2", "input"
    ]
    # a net only the `.NetList` knows: 0 pins on both components, source "new".
    sense = row_of(model, SENSE_NETS[0])
    assert texts(model, sense, (NET_PVRM, NET_PSINK, NET_SRC)) == ["0", "0", "new"]


def test_vrm_and_sink_table_columns_match_design(session: Session) -> None:
    vrm = VrmTableModel(session)
    assert [spec.title for spec in vrm.columns] == [
        "Use",
        "Net",
        "Component",
        "Ground",
        "Nominal V",
        "Sense V",
        "Output Current (A)",
        "Pos pins",
        "Neg pins",
        "Name",
    ]
    assert [spec.kind for spec in vrm.columns] == [
        KIND_BOOL,
        KIND_TEXT,
        KIND_ENUM,
        KIND_ENUM,
        KIND_VOLTAGE,
        KIND_VOLTAGE,
        KIND_CURRENT,
        KIND_INT,
        KIND_INT,
        KIND_TEXT,
    ]
    assert not vrm.columns[VRM_NAME].editable  # "Name derived read-only"
    assert texts(vrm, 0) == [
        "1", POWER_NET_A, "LGA", GROUND_NET, "0.7", "0.7", "1", "3", "2",
        f"VRM_LGA_{POWER_NET_A}_{GROUND_NET}",
    ]

    sink = SinkTableModel(session)
    assert [spec.title for spec in sink.columns] == [
        "Use",
        "Net",
        "Component",
        "Ground",
        "Nominal V",
        "Current (A)",
        "Model",
        "PFMode",
        "PinEqualCurrent",
        "Pos pins",
        "Neg pins",
        "Name",
    ]
    # design §C: the advanced trio is exposed for the *Show advanced* toggle.
    assert sink.ADVANCED_COLUMNS == (6, 7, 8)
    assert [sink.columns[column].key for column in sink.ADVANCED_COLUMNS] == [
        "model",
        "pf_mode",
        "pin_equal_current",
    ]
    assert [sink.columns[column].kind for column in sink.ADVANCED_COLUMNS] == [KIND_INT] * 3
    assert sink.columns[SINK_CUR].kind == KIND_CURRENT
    assert texts(sink, 0, sink.ADVANCED_COLUMNS) == ["2", "2", "1"]  # §A3 defaults


# --------------------------------------------------------------------------- #
# type-to-fill (design §C)
# --------------------------------------------------------------------------- #


def test_type_to_fill_spans_every_matching_kind_in_the_selection(session: Session) -> None:
    """"a selection spanning Nominal V + Sense V fills both"."""
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_USE), (1, VRM_CUR), current=(0, VRM_NOM))

    filled = view.apply_bulk("0.9", model.index(0, VRM_NOM))

    assert filled == 4  # 2 rows x (Nominal V, Sense V)
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.9", "0.9"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["0.9", "0.9"]
    # kind mismatch skipped silently: Use (bool), Net (text), Component/Ground
    # (enum) and Output Current (current) are untouched.
    assert texts(model, 1, (VRM_USE, VRM_NET, VRM_COMP, VRM_GND, VRM_CUR)) == [
        "1", POWER_NET_B, "LGA", GROUND_NET, "1",
    ]
    assert view.undo_stack.count() == 1  # "One BulkEditCommand ... per operation"


def test_type_to_fill_skips_read_only_cells(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)

    # a read-only column (Net) accepts nothing at all
    select(view, (0, VRM_NET), (1, VRM_NET))
    assert view.apply_bulk("NOPE", model.index(0, VRM_NET)) == 0
    assert view.undo_stack.count() == 0
    assert texts(model, 0, (VRM_NET,)) == [POWER_NET_A]

    # a mixed selection writes the editable cells only
    net_model = NetTableModel(session)
    net_view = view_for(net_model)
    row = row_of(net_model, POWER_NET_A)
    select(net_view, (row, NET_V), (row, NET_PSINK), current=(row, NET_V))
    assert net_view.apply_bulk("0.5", net_model.index(row, NET_V)) == 1
    assert texts(net_model, row, (NET_V, NET_DIE, NET_PVRM, NET_PSINK)) == ["0.5", "0", "3", "2"]


def test_type_to_fill_through_the_real_numeric_delegate(session: Session) -> None:
    """design §C: the delegate commits to `currentIndex`, *then* broadcasts."""
    model = VrmTableModel(session)
    view = view_for(model)
    delegate = view.itemDelegateForColumn(VRM_NOM)
    assert isinstance(delegate, NumericDelegate)
    assert isinstance(view.itemDelegateForColumn(VRM_COMP), ComboDelegate)
    # read-only columns get no editor at all
    assert view.itemDelegateForColumn(VRM_NET) is None
    assert view.itemDelegateForColumn(VRM_NAME) is None

    select(view, (0, VRM_NOM), (1, VRM_SENSE), current=(0, VRM_NOM))
    anchor = model.index(0, VRM_NOM)
    view.edit(anchor)  # snapshots the pre-commit value, opens the editor
    editor = view.viewport().findChild(QLineEdit)
    assert editor is not None and editor.text() == "0.7"
    editor.setText("0.85")
    delegate.setModelData(editor, model, anchor)

    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.85", "0.85"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["0.85", "0.85"]
    # the anchor's *pre-edit* value survived the delegate's early commit
    assert view.undo_stack.count() == 1
    view.undo_stack.undo()
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.7", "0.7"]


# --------------------------------------------------------------------------- #
# fill down / right (design §C Ctrl+D / Ctrl+R)
# --------------------------------------------------------------------------- #


def test_fill_down_uses_the_topmost_selected_cell_per_column(session: Session) -> None:
    model = NetTableModel(session)
    view = view_for(model)
    first = row_of(model, POWER_NET_A)
    last = row_of(model, SENSE_NETS[3])
    model.setData(model.index(first, NET_V), 1.85)

    select(view, (first, NET_V), (last, NET_V))
    changed = view._fill_down()

    assert changed == last - first
    assert [texts(model, row, (NET_V,))[0] for row in range(first, last + 1)] == ["1.85"] * (
        last - first + 1
    )
    # the seed `setData` bypassed the view, so the fill is the only command
    assert view.undo_stack.count() == 1


def test_fill_right_only_crosses_kind_compatible_columns(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    model.setData(model.index(0, VRM_NOM), 2.5)

    select(view, (0, VRM_NOM), (0, VRM_CUR))
    changed = view._fill_right()

    assert changed == 1  # Nominal V -> Sense V; Output Current is kind "current"
    assert texts(model, 0, (VRM_NOM, VRM_SENSE, VRM_CUR)) == ["2.5", "2.5", "1"]


# --------------------------------------------------------------------------- #
# TSV copy / paste (design §C Ctrl+C / Ctrl+V)
# --------------------------------------------------------------------------- #


def test_copy_writes_tsv_of_the_selection_bounding_rect(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (1, VRM_SENSE))

    assert view.selection_as_tsv() == "0.7\t0.7\n1.2\t1.2"
    view._copy_selection()
    assert QGuiApplication.clipboard().text() == "0.7\t0.7\n1.2\t1.2"


def test_paste_rectangle_is_anchored_at_current_index(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (1, VRM_SENSE), current=(0, VRM_NOM))

    assert view.paste_grid(view.parse_clipboard_grid("1.1\t2.2\n4.4\t5.5")) == 4
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["1.1", "2.2"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["4.4", "5.5"]
    assert view.undo_stack.count() == 1


def test_paste_1x1_broadcasts_over_the_whole_selection(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (1, VRM_SENSE), current=(0, VRM_NOM))
    QGuiApplication.clipboard().setText("3.3")

    assert view._paste_clipboard() == 4
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["3.3", "3.3"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["3.3", "3.3"]


def test_paste_1xn_and_nx1_broadcast_along_the_other_axis(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (1, VRM_SENSE), current=(0, VRM_NOM))

    assert view.paste_grid(view.parse_clipboard_grid("6.1\t6.2")) == 4
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["6.1", "6.2"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["6.1", "6.2"]

    assert view.paste_grid(view.parse_clipboard_grid("7.1\n7.2")) == 4
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["7.1", "7.1"]
    assert texts(model, 1, (VRM_NOM, VRM_SENSE)) == ["7.2", "7.2"]


def test_paste_is_clipped_to_the_model_bounds(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    last = model.rowCount() - 1
    select(view, (last, VRM_NOM), (last, VRM_NOM), current=(last, VRM_NOM))

    # a 2x2 payload anchored on the last row: the second row falls off the end
    assert view.paste_grid(view.parse_clipboard_grid("7.7\t8.8\n9.9\t0.5")) == 2
    assert texts(model, last, (VRM_NOM, VRM_SENSE)) == ["7.7", "8.8"]
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.7", "0.7"]


def test_paste_falls_back_to_comma_and_skips_unparseable_tokens(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (0, VRM_SENSE), current=(0, VRM_NOM))

    assert view.parse_clipboard_grid("1.5,2.5\n") == [["1.5", "2.5"]]
    assert view.paste_grid([["1.5", "not-a-number"]]) == 1
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["1.5", "1.5"]


# --------------------------------------------------------------------------- #
# space toggle (design §C)
# --------------------------------------------------------------------------- #


def test_space_toggles_selected_bools_to_the_inverse_of_the_anchor(session: Session) -> None:
    model = NetTableModel(session)
    view = view_for(model)
    first, last = 0, 3
    anchor = 2
    model.setData(model.index(anchor, NET_USE), False)
    assert [texts(model, row, (NET_USE,))[0] for row in range(first, last + 1)] == [
        "1", "1", "0", "1",
    ]

    select(view, (first, NET_USE), (last, NET_USE), current=(anchor, NET_USE))
    view._toggle_selected_bools()

    # anchor was unchecked -> every selected bool becomes checked
    assert [texts(model, row, (NET_USE,))[0] for row in range(first, last + 1)] == ["1"] * 4

    # and again: anchor now checked -> everything clears, in one undo step
    view._toggle_selected_bools()
    assert [texts(model, row, (NET_USE,))[0] for row in range(first, last + 1)] == ["0"] * 4
    view.undo_stack.undo()
    assert [texts(model, row, (NET_USE,))[0] for row in range(first, last + 1)] == ["1"] * 4


# --------------------------------------------------------------------------- #
# undo (design §C "One BulkEditCommand on the undo stack per operation")
# --------------------------------------------------------------------------- #


def test_undo_restores_every_cell_of_a_bulk_op_in_one_step(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    before = [texts(model, row, (VRM_NOM, VRM_SENSE)) for row in range(2)]
    assert before == [["0.7", "0.7"], ["1.2", "1.2"]]

    select(view, (0, VRM_NOM), (1, VRM_SENSE), current=(0, VRM_NOM))
    assert view.apply_bulk("0.42", model.index(0, VRM_NOM)) == 4
    assert view.undo_stack.count() == 1
    assert [texts(model, row, (VRM_NOM, VRM_SENSE)) for row in range(2)] == [
        ["0.42", "0.42"],
        ["0.42", "0.42"],
    ]

    view.undo_stack.undo()
    assert [texts(model, row, (VRM_NOM, VRM_SENSE)) for row in range(2)] == before
    # design §B: cells that were auto before are auto again, not "user-set 0.7"
    assert not any(
        model.is_override(model.index(row, column))
        for row in range(2)
        for column in (VRM_NOM, VRM_SENSE)
    )

    view.undo_stack.redo()
    assert [texts(model, row, (VRM_NOM, VRM_SENSE)) for row in range(2)] == [
        ["0.42", "0.42"],
        ["0.42", "0.42"],
    ]


def test_undo_stack_is_hookable_from_outside(session: Session) -> None:
    from PySide6.QtGui import QUndoStack

    shared = QUndoStack()
    net_view = view_for(NetTableModel(session))
    vrm_view = view_for(VrmTableModel(session))
    net_view.undo_stack = shared
    vrm_view.undo_stack = shared
    assert net_view.undo_stack is shared and vrm_view.undo_stack is shared

    model = vrm_view.model()
    select(vrm_view, (0, VRM_NOM), (0, VRM_NOM), current=(0, VRM_NOM))
    vrm_view.apply_bulk("1.05", model.index(0, VRM_NOM))
    net_model = net_view.model()
    row = row_of(net_model, POWER_NET_B)
    select(net_view, (row, NET_V), (row, NET_V), current=(row, NET_V))
    net_view.apply_bulk("2.05", net_model.index(row, NET_V))

    assert shared.count() == 2  # one stack spans both tabs (design §C Ctrl+Z)
    shared.undo()
    shared.undo()
    assert texts(model, 0, (VRM_NOM,)) == ["0.7"]
    assert texts(net_model, row, (NET_V,)) == ["1.2"]


def test_reset_to_auto_is_undoable(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    model.setData(model.index(0, VRM_NOM), 2.5)
    assert model.is_override(model.index(0, VRM_NOM))

    select(view, (0, VRM_NOM), (0, VRM_SENSE), current=(0, VRM_NOM))
    assert view.reset_selection_to_auto() == 1  # only the nominal was overridden
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.7", "0.7"]
    assert not model.is_override(model.index(0, VRM_NOM))

    view.undo_stack.undo()
    assert texts(model, 0, (VRM_NOM,)) == ["2.5"]
    assert model.is_override(model.index(0, VRM_NOM))


# --------------------------------------------------------------------------- #
# override styling (design §B)
# --------------------------------------------------------------------------- #


def test_overridden_cells_are_italic_accented_and_carry_the_auto_value(
    session: Session,
) -> None:
    model = NetTableModel(session)
    row = row_of(model, POWER_NET_A)
    index = model.index(row, NET_V)

    assert model.data(index, Qt.ItemDataRole.FontRole) is None
    assert model.data(index, Qt.ItemDataRole.ForegroundRole) is None

    assert model.setData(index, "0.95")

    font = model.data(index, Qt.ItemDataRole.FontRole)
    assert font is not None and font.italic()
    assert model.data(index, Qt.ItemDataRole.ForegroundRole).color() == OVERRIDE_COLOR
    tooltip = model.data(index, Qt.ItemDataRole.ToolTipRole)
    assert tooltip == "User-set (auto value: 0.7 V)"  # design §B, verbatim wording

    # a derived cell that followed the edit stays auto-styled...
    vrm = VrmTableModel(session)
    vrm_index = vrm.index(0, VRM_NOM)
    assert vrm.cell_text(vrm_index) == "0.95"
    assert vrm.data(vrm_index, Qt.ItemDataRole.FontRole) is None
    # ...until it is edited itself, and then quotes its own auto value.
    assert vrm.setData(vrm_index, "1.5")
    assert vrm.data(vrm_index, Qt.ItemDataRole.ToolTipRole) == "User-set (auto value: 0.95 V)"
    assert vrm.data(vrm.index(0, VRM_CUR), Qt.ItemDataRole.ToolTipRole) is None


def test_blocking_validation_issues_paint_the_row(session: Session) -> None:
    model = NetTableModel(session)
    # a remote-sense net has no pins on any component -> §G.4 blocking issue
    model.setData(model.index(row_of(model, SENSE_NETS[0]), NET_CLASS), "power")
    row = row_of(model, SENSE_NETS[0])

    assert model.row_has_error(row)
    brush = model.data(model.index(row, NET_NET), Qt.ItemDataRole.BackgroundRole)
    assert brush is not None and brush.color() == ERROR_BACKGROUND
    assert "no positive pins" in model.data(model.index(row, NET_NET), Qt.ItemDataRole.ToolTipRole)
    assert model.data(model.index(row_of(model, POWER_NET_A), NET_NET),
                      Qt.ItemDataRole.BackgroundRole) is None


# --------------------------------------------------------------------------- #
# filter proxy (design §C Net Manager top row)
# --------------------------------------------------------------------------- #


def test_filter_proxy_and_terms_and_glob(session: Session) -> None:
    model = NetTableModel(session)
    proxy = NetFilterProxy()
    proxy.setSourceModel(model)

    def shown() -> list[str]:
        return [proxy.index(row, NET_NET).data() for row in range(proxy.rowCount())]

    assert proxy.totalCount() == model.rowCount()
    proxy.setFilterExpression("")
    assert proxy.shownCount() == model.rowCount()

    proxy.setFilterExpression("vdd")  # case-insensitive substring
    assert shown() == [POWER_NET_A, POWER_NET_B, *SENSE_NETS]

    proxy.setFilterExpression("VDD 070")  # space-separated AND terms
    assert shown() == [POWER_NET_A, SENSE_NETS[0], SENSE_NETS[1]]

    proxy.setFilterExpression("*_gs/*")  # `*` glob, whole-name match
    assert shown() == [SENSE_NETS[1], SENSE_NETS[3]]

    proxy.setFilterExpression("vdd *_ps/*")  # glob AND-ed with a plain term
    assert shown() == [SENSE_NETS[0], SENSE_NETS[2]]

    proxy.setFilterExpression("no-such-net")
    assert shown() == []


def test_filter_proxy_class_modes(session: Session) -> None:
    model = NetTableModel(session)
    proxy = NetFilterProxy()
    proxy.setSourceModel(model)

    def shown() -> list[str]:
        return [proxy.index(row, NET_NET).data() for row in range(proxy.rowCount())]

    assert NetFilterProxy.MODES == (
        "All", "Power", "Ground", "Unclassified", "Selected", "Errors",
    )

    proxy.setClassFilter("All")
    assert proxy.rowCount() == model.rowCount()
    proxy.setClassFilter("Power")
    assert shown() == [POWER_NET_A, POWER_NET_B]
    proxy.setClassFilter("Ground")
    assert shown() == [GROUND_NET]
    proxy.setClassFilter("Unclassified")
    assert shown() == [*UNCLASSIFIED_NETS, *SENSE_NETS]

    proxy.setClassFilter("Selected")
    assert proxy.rowCount() == model.rowCount()
    model.setData(model.index(row_of(model, POWER_NET_A), NET_USE), False)
    assert POWER_NET_A not in shown()

    proxy.setClassFilter("Errors")
    assert shown() == []
    model.setData(model.index(row_of(model, SENSE_NETS[0]), NET_CLASS), "power")
    assert shown() == [SENSE_NETS[0]]

    proxy.setClassFilter("nonsense-mode")  # unknown -> All
    assert proxy.classFilter() == "All"

    # the filter box and the class combo AND together
    proxy.setClassFilter("Power")
    proxy.setFilterExpression("120")
    assert shown() == [POWER_NET_B]


def test_bulk_edit_through_the_filter_proxy(session: Session) -> None:
    """Row keys, not row numbers: undo still lands on the right nets when the
    proxy is hiding most of the table."""
    model = NetTableModel(session)
    proxy = NetFilterProxy()
    proxy.setSourceModel(model)
    proxy.setClassFilter("Power")
    view = view_for(proxy)
    assert proxy.rowCount() == 2

    select(view, (0, NET_V), (1, NET_V), current=(0, NET_V))
    assert view.apply_bulk("0.33", proxy.index(0, NET_V)) == 2
    assert [proxy.index(row, NET_V).data() for row in range(2)] == ["0.33", "0.33"]

    view.undo_stack.undo()
    assert texts(model, row_of(model, POWER_NET_A), (NET_V,)) == ["0.7"]
    assert texts(model, row_of(model, POWER_NET_B), (NET_V,)) == ["1.2"]


# --------------------------------------------------------------------------- #
# write-through to Session (design §B / §C)
# --------------------------------------------------------------------------- #


def test_check_state_role_routes_to_session_set_selected(session: Session) -> None:
    model = NetTableModel(session)
    row = row_of(model, POWER_NET_A)
    index = model.index(row, NET_USE)
    calls: list[tuple[str, bool]] = []
    original = session.set_selected

    def spy(net: str, selected: bool):
        calls.append((net, selected))
        return original(net, selected)

    session.set_selected = spy  # type: ignore[method-assign]

    assert model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)

    assert calls == [(POWER_NET_A, False)]
    assert session.nets[POWER_NET_A].selected is False
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    # `Session.set_selected` mirrors onto that net's VRM/Sink rows
    assert session.vrm(POWER_NET_A).enabled is False

    # a raw int check state (what a view click delivers) works too
    assert model.setData(index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole)
    assert session.nets[POWER_NET_A].selected is True


def test_set_data_rejects_values_session_would_refuse(session: Session) -> None:
    model = NetTableModel(session)
    row = row_of(model, POWER_NET_A)
    assert not model.setData(model.index(row, NET_CLASS), "bogus")  # not a choice
    assert texts(model, row, (NET_CLASS,)) == ["power"]
    assert not model.setData(model.index(row, NET_V), "abc")  # not a number
    assert texts(model, row, (NET_V,)) == ["0.7"]
    assert not model.setData(model.index(row, NET_NET), "x")  # read-only


def test_structural_change_resets_the_model(session: Session) -> None:
    net_model = NetTableModel(session)
    vrm_model = VrmTableModel(session)
    resets: list[int] = []
    vrm_model.modelReset.connect(lambda: resets.append(1))
    assert vrm_model.rowCount() == 2

    # dropping a power net removes its VRM/Sink rows -> `structure_version` bump
    net_model.setData(net_model.index(row_of(net_model, POWER_NET_A), NET_CLASS), "none")

    assert resets and vrm_model.rowCount() == 1
    assert vrm_model.net_for_row(0) == POWER_NET_B


# --------------------------------------------------------------------------- #
# propagation cascade (design §B)
# --------------------------------------------------------------------------- #


def test_net_voltage_edit_reaches_the_vrm_table_as_data_changed(session: Session) -> None:
    net_model = NetTableModel(session)
    vrm_model = VrmTableModel(session)
    sink_model = SinkTableModel(session)
    signals: list[tuple[int, int, int]] = []
    vrm_model.dataChanged.connect(
        lambda top_left, bottom_right, roles=None: signals.append(
            (top_left.row(), top_left.column(), bottom_right.column())
        )
    )

    row = row_of(net_model, POWER_NET_A)
    assert net_model.setData(net_model.index(row, NET_V), "0.42")

    # the value cascaded (design §B: nominal + sense follow while not overridden)
    assert texts(vrm_model, 0, (VRM_NOM, VRM_SENSE)) == ["0.42", "0.42"]
    assert texts(sink_model, 0, (SINK_NOM,)) == ["0.42"]
    # ...and the VRM table was told to repaint that row's changed span
    assert signals, "no dataChanged reached the VRM model"
    vrm_row = row_of(vrm_model, POWER_NET_A)
    assert any(
        row_index == vrm_row and low <= VRM_NOM <= high for row_index, low, high in signals
    )
    # the untouched net's rows were not repainted
    other = row_of(vrm_model, POWER_NET_B)
    assert all(row_index != other for row_index, _low, _high in signals)


def test_overridden_derived_cell_ignores_the_cascade(session: Session) -> None:
    net_model = NetTableModel(session)
    vrm_model = VrmTableModel(session)
    assert vrm_model.setData(vrm_model.index(0, VRM_SENSE), "1.8")

    net_model.setData(net_model.index(row_of(net_model, POWER_NET_A), NET_V), "0.5")

    assert texts(vrm_model, 0, (VRM_NOM, VRM_SENSE)) == ["0.5", "1.8"]
    assert vrm_model.is_override(vrm_model.index(0, VRM_SENSE))
    assert not vrm_model.is_override(vrm_model.index(0, VRM_NOM))


def test_changed_keys_map_to_the_right_rows(session: Session) -> None:
    """`Session` row keys -> model rows (the §B "(row, col) keys" translation)."""
    net_model = NetTableModel(session)
    vrm_model = VrmTableModel(session)
    assert net_model.row_key(row_of(net_model, POWER_NET_A)) == net_key(POWER_NET_A)
    assert vrm_model.row_key(0) == vrm_key(POWER_NET_A, 0)
    assert vrm_model.row_of_key(vrm_key(POWER_NET_B, 0)) == 1
    index = vrm_model.index_for_key(vrm_key(POWER_NET_B, 0), VRM_NOM)
    assert index.isValid() and vrm_model.cell_text(index) == "1.2"
    assert not vrm_model.index_for_key("vrm:nope#0", VRM_NOM).isValid()


# --------------------------------------------------------------------------- #
# context menu (design §C)
# --------------------------------------------------------------------------- #


def test_context_menu_offers_every_design_entry(session: Session) -> None:
    view = view_for(VrmTableModel(session))
    titles = [action.text() for action in view.build_context_menu().actions() if action.text()]
    assert titles == [
        "Set value…",
        "Fill Down",
        "Fill Right",
        "Copy",
        "Paste",
        "Reset to auto",
        "Check selected",
        "Uncheck selected",
        "Set paired ground…",
        "Go to net",
    ]


def test_context_menu_operations(session: Session) -> None:
    model = NetTableModel(session)
    view = view_for(model)
    row = row_of(model, POWER_NET_A)

    # Set value…
    select(view, (row, NET_V), (row, NET_V), current=(row, NET_V))
    assert view.set_value_for_selection("1.25") == 1
    assert texts(model, row, (NET_V,)) == ["1.25"]

    # Check / Uncheck selected (no bool cell selected -> the Use column)
    assert view.check_selection(False) == 1
    assert texts(model, row, (NET_USE,)) == ["0"]
    assert view.check_selection(True) == 1
    assert texts(model, row, (NET_USE,)) == ["1"]

    # Set paired ground… (needs a second ground to pick from)
    second = UNCLASSIFIED_NETS[0]
    assert model.setData(model.index(row_of(model, second), NET_CLASS), "ground")
    row = row_of(model, POWER_NET_A)  # the class change reset/re-sorted the model
    select(view, (row, NET_V), (row, NET_V), current=(row, NET_V))
    assert texts(model, row, (NET_GND,)) == [GROUND_NET]
    grounds: list[str] = []
    view.pairedGroundRequested.connect(grounds.append)
    assert view.set_paired_ground_for_selection(second) == 1
    assert texts(model, row, (NET_GND,)) == [second]
    assert grounds == [second]

    # Go to net
    nets: list[str] = []
    view.goToNetRequested.connect(nets.append)
    view.go_to_net()
    assert nets == [POWER_NET_A]


# --------------------------------------------------------------------------- #
# mouse / keyboard entry points
# --------------------------------------------------------------------------- #


def _click(view: BulkEditTableView, index) -> None:
    """Synthesize a left press+release on *index*'s cell rect."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    rect = view.visualRect(index)
    local = QPointF(rect.center())
    globally = view.viewport().mapToGlobal(rect.center())
    for kind, buttons in (
        (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
    ):
        QApplication.sendEvent(
            view.viewport(),
            QMouseEvent(
                kind,
                local,
                globally,
                Qt.MouseButton.LeftButton,
                buttons,
                Qt.KeyboardModifier.NoModifier,
            ),
        )


def test_checkbox_click_toggles_once_and_is_undoable(session: Session) -> None:
    model = NetTableModel(session)
    view = view_for(model)
    view.resize(900, 400)
    view.show()
    QApplication.processEvents()

    index = model.index(0, NET_USE)
    assert model.cell_text(index) == "1"
    _click(view, index)

    assert model.cell_text(index) == "0"  # exactly one toggle, not two
    assert view.undo_stack.count() == 1
    view.undo_stack.undo()
    assert model.cell_text(index) == "1"

    # a click on a non-bool cell pushes nothing
    _click(view, model.index(0, NET_V))
    assert view.undo_stack.count() == 1


def test_space_key_reaches_the_bulk_toggle(session: Session) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    model = NetTableModel(session)
    view = view_for(model)
    select(view, (0, NET_USE), (2, NET_USE), current=(0, NET_USE))

    view.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ")
    )

    assert [texts(model, row, (NET_USE,))[0] for row in range(3)] == ["0", "0", "0"]
    assert view.undo_stack.count() == 1


# --------------------------------------------------------------------------- #
# bool columns round-trip through the clipboard
# --------------------------------------------------------------------------- #


def test_bool_cells_copy_as_1_0_and_paste_back(session: Session) -> None:
    model = NetTableModel(session)
    view = view_for(model)
    model.setData(model.index(1, NET_USE), False)

    select(view, (0, NET_USE), (2, NET_USE), current=(0, NET_USE))
    assert view.selection_as_tsv() == "1\n0\n1"

    assert view.paste_grid([["0"], ["1"], ["0"]]) == 3
    assert [texts(model, row, (NET_USE,))[0] for row in range(3)] == ["0", "1", "0"]
    assert session.nets[model.net_for_row(1)].selected is True


# --------------------------------------------------------------------------- #
# delegates
# --------------------------------------------------------------------------- #


def test_combo_delegate_offers_session_choices_and_rejects_typos(session: Session) -> None:
    from PySide6.QtWidgets import QStyleOptionViewItem

    model = NetTableModel(session)
    view = view_for(model)
    delegate = view.itemDelegateForColumn(NET_CLASS)
    row = row_of(model, POWER_NET_A)
    index = model.index(row, NET_CLASS)

    editor = delegate.createEditor(view.viewport(), QStyleOptionViewItem(), index)
    delegate.setEditorData(editor, index)
    assert [editor.itemText(i) for i in range(editor.count())] == ["power", "ground", "none"]
    assert editor.currentText() == "power"

    editor.setCurrentText("not-a-class")
    delegate.setModelData(editor, model, index)
    assert texts(model, row, (NET_CLASS,)) == ["power"]  # unchanged, nothing broadcast

    editor.setCurrentText("ground")
    delegate.setModelData(editor, model, index)
    assert session.nets[POWER_NET_A].net_class == "ground"


def test_numeric_delegate_edits_without_a_spin_box(session: Session) -> None:
    from PySide6.QtWidgets import QStyleOptionViewItem

    model = VrmTableModel(session)
    view = view_for(model)
    delegate = view.itemDelegateForColumn(VRM_CUR)
    index = model.index(0, VRM_CUR)

    editor = delegate.createEditor(view.viewport(), QStyleOptionViewItem(), index)
    assert isinstance(editor, QLineEdit)
    delegate.setEditorData(editor, index)
    assert editor.text() == "1"
    # a value a QDoubleSpinBox's default 2 decimals would have rounded away
    editor.setText("0.0625")
    delegate.setModelData(editor, model, index)
    assert texts(model, 0, (VRM_CUR,)) == ["0.0625"]
    assert session.vrm(POWER_NET_A).output_current == pytest.approx(0.0625)


def test_numeric_columns_accept_a_pasted_unit_suffix(session: Session) -> None:
    model = VrmTableModel(session)
    view = view_for(model)
    select(view, (0, VRM_NOM), (0, VRM_NOM), current=(0, VRM_NOM))

    assert view.apply_bulk("1.05 V", model.index(0, VRM_NOM)) == 1
    assert texts(model, 0, (VRM_NOM,)) == ["1.05"]


# --------------------------------------------------------------------------- #
# model bookkeeping
# --------------------------------------------------------------------------- #


def test_model_resets_when_ensure_ground_adds_a_net_row(session: Session) -> None:
    """`Session._ensure_ground` materializes a net row *and* bumps
    `structure_version` (chunk 7 fix), so `apply_changed_keys` resets."""
    model = NetTableModel(session)
    before = model.rowCount()
    version = session.structure_version

    changed = session.set_paired_ground(POWER_NET_A, "BRAND_NEW_GND")

    assert session.structure_version > version  # Session flags the addition itself
    assert len(session.nets) == before + 1
    resets: list[int] = []
    model.modelReset.connect(lambda: resets.append(1))
    model.apply_changed_keys(changed)
    assert resets and model.rowCount() == before + 1
    assert row_of(model, "BRAND_NEW_GND") >= 0


def test_model_row_count_check_still_catches_an_unflagged_row(session: Session) -> None:
    """The row-count safety net in `apply_changed_keys` survives the chunk 7 fix:
    a session grown outside the mutator API still forces a full reset."""
    model = NetTableModel(session)
    before = model.rowCount()

    # Bypass every mutator -- exactly what a hand-rolled caller would do.
    session._add_net(PowerNetConfig(net="OFF_BOOKS_GND", net_class="ground"))
    assert session.structure_version == model._structure_version

    resets: list[int] = []
    model.modelReset.connect(lambda: resets.append(1))
    model.apply_changed_keys([(net_key(POWER_NET_A), "voltage")])
    assert resets and model.rowCount() == before + 1


def test_setdata_swallows_session_valueerror(session: Session) -> None:
    """design §C: a rejected value is "skipped silently" -- `Session.set_class`'s
    `ValueError` must never escape into the view (session issue (c))."""
    model = NetTableModel(session)
    row = row_of(model, UNCLASSIFIED_NETS[0])
    index = model.index(row, NET_CLASS)

    # An unknown class survives the enum parser; `Session.set_class` rejects it.
    assert model.setData(index, "POWER_SUPPLY", Qt.ItemDataRole.EditRole) is False
    assert session.nets[UNCLASSIFIED_NETS[0]].net_class == "none"


def test_sorting_uses_edit_role_so_numbers_sort_numerically(session: Session) -> None:
    model = NetTableModel(session)
    proxy = NetFilterProxy()
    proxy.setSourceModel(model)
    proxy.setClassFilter("Power")
    model.setData(model.index(row_of(model, POWER_NET_A), NET_V), 10.0)

    proxy.sort(NET_V, Qt.SortOrder.AscendingOrder)
    # 1.2 before 10 (a DisplayRole string sort would put "10" first)
    assert [proxy.index(row, NET_NET).data() for row in range(2)] == [POWER_NET_B, POWER_NET_A]


def test_refresh_from_session_picks_up_out_of_band_edits(session: Session) -> None:
    model = VrmTableModel(session)
    resets: list[int] = []
    model.modelReset.connect(lambda: resets.append(1))

    session.add_vrm_row(POWER_NET_A)  # a direct `Session` call, no model involved
    model.refresh_from_session()

    assert resets == [1]
    assert model.rowCount() == 3
    # chunk 7: `add_vrm_row` places the row in netlist order immediately, so the
    # second VRM of net A sits next to the first, not at the end of the table.
    assert model.row_key(1) == vrm_key(POWER_NET_A, 1)
    assert model.row_of_key(vrm_key(POWER_NET_A, 1)) == 1
    assert model.row_key(2) == vrm_key(POWER_NET_B, 0)


def test_base_model_is_generic_over_a_row_provider(session: Session) -> None:
    """The three tables are just column lists over `Session` row providers."""
    from powerdc_setup_tool.ui.models import BaseConfigModel, ColumnSpec

    columns = (
        ColumnSpec("net", "Net", KIND_TEXT, getter=lambda _s, cfg, _k: cfg.net),
        ColumnSpec(
            "output_current",
            "A",
            KIND_CURRENT,
            editable=True,
            override_field="current_override",
        ),
    )
    model = BaseConfigModel(
        session,
        columns,
        row_kind="vrm",
        rows_provider=lambda s: [row for row in s.vrm_rows if row.net == POWER_NET_B],
    )

    assert model.rowCount() == 1
    assert model.columnCount() == 2
    assert model.row_key(0) == vrm_key(POWER_NET_B, 0)
    assert model.setData(model.index(0, 1), "7.5")
    assert session.vrm(POWER_NET_B).output_current == pytest.approx(7.5)
    assert model.is_override(model.index(0, 1))


def test_view_selection_setup_matches_design(session: Session) -> None:
    from PySide6.QtWidgets import QAbstractItemView

    view = view_for(NetTableModel(session))
    assert view.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert view.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems
    # design §C "row-select via vertical header"
    assert view.verticalHeader().sectionsClickable()
    view.selectRow(2)
    assert sorted(index.column() for index in view.selectionModel().selectedIndexes()) == list(
        range(view.model().columnCount())
    )


def test_ctrl_keys_route_to_the_bulk_operations(session: Session) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    model = VrmTableModel(session)
    view = view_for(model)

    def press(key) -> None:
        view.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.ControlModifier, "")
        )

    model.setData(model.index(0, VRM_NOM), 3.75)
    select(view, (0, VRM_NOM), (1, VRM_NOM), current=(0, VRM_NOM))
    press(Qt.Key.Key_D)  # fill down
    assert texts(model, 1, (VRM_NOM,)) == ["3.75"]

    select(view, (0, VRM_NOM), (0, VRM_SENSE), current=(0, VRM_NOM))
    press(Qt.Key.Key_R)  # fill right
    assert texts(model, 0, (VRM_SENSE,)) == ["3.75"]

    press(Qt.Key.Key_C)  # copy
    assert QGuiApplication.clipboard().text() == "3.75\t3.75"

    QGuiApplication.clipboard().setText("0.25")
    press(Qt.Key.Key_V)  # paste
    assert texts(model, 0, (VRM_NOM, VRM_SENSE)) == ["0.25", "0.25"]
