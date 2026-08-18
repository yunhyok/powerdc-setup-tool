"""`ui/main_window.py` + `ui/net_manager.py` + `ui/vrm_tab.py` + `ui/sink_tab.py`
+ `ui/workers.py` -- design §E `test_ui_smoke` row.

Window title/tabs/status bar wiring, the filter proxy's shown-count label, a
real threaded scan through `ScanWorker` (plus the sibling app's worker
strong-ref regression), a headless export round trip through `ExportWorker`
that must equal `expected_dc()`, busy-state toggling, and the shared undo
stack across all three tabs.

Every `MainWindow` is built through the `make_window` fixture rather than
`MainWindow()` directly, so it is always `close()`d at teardown: `MainWindow`'s
own signal/slot connections (e.g. a model's `dataChanged` bound to
`window._on_model_changed`) form Python reference cycles back to the window,
so a window that merely goes out of scope is not collected deterministically
-- and destroying a `QThread` whose managed thread has not been `wait()`-ed
is a fatal Qt error (``QThread: Destroyed while thread '' is still
running``). `closeEvent` already does that `quit()`/`wait()`; this fixture
just makes sure it always runs before a window can become garbage.
"""

from __future__ import annotations

import os

# design §E: the offscreen platform must be selected before PySide6 is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QUndoStack  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QStyleOptionViewItem,
)

from fixtures import (  # noqa: E402
    GROUND_NET,
    POWER_NET_A,
    POWER_NET_B,
    SENSE_NETS,
    UNCLASSIFIED_NETS,
    build_mini_spd,
    expected_dc,
)
from powerdc_setup_tool.core.session import Session  # noqa: E402
from powerdc_setup_tool.core.spd_scan import ScanCancelled, scan_spd  # noqa: E402
from powerdc_setup_tool.ui import main_window as main_window_mod  # noqa: E402
from powerdc_setup_tool.ui import workers as workers_mod  # noqa: E402
from powerdc_setup_tool.ui.main_window import APP_TITLE, MainWindow, _ExportOptionsDialog  # noqa: E402
from powerdc_setup_tool.ui.models import (  # noqa: E402
    SORT_ROLE,
    NetTableModel,
    SinkTableModel,
    VrmTableModel,
    source_index,
)
from powerdc_setup_tool.ui.net_manager import NetManagerTab  # noqa: E402
from powerdc_setup_tool.ui.sink_tab import SinkTab  # noqa: E402
from powerdc_setup_tool.ui.vrm_tab import VrmTab  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    """One `QApplication` for the whole module (Qt allows exactly one)."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_window(qapp: QApplication) -> Iterator[Callable[[], MainWindow]]:
    """Factory for `MainWindow` instances that are guaranteed to be properly
    torn down (thread-safe) at the end of the test -- see module docstring."""
    created: list[MainWindow] = []

    def factory(*, initial_path: Path | None = None) -> MainWindow:
        window = MainWindow(initial_path=initial_path)
        created.append(window)
        return window

    yield factory

    for window in created:
        window.close()
    for _ in range(5):
        qapp.processEvents()


def _spin_until(app: QApplication, predicate, timeout: float, what: str) -> None:
    """Pump the event loop until *predicate* is true (mirrors the sibling
    `spd-model-injector` app's test helper -- real QThread work needs a real
    event loop to deliver its queued signals back to the GUI thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"Timed out after {timeout}s waiting for: {what}")


def _load_via_thread(app: QApplication, window: MainWindow, path: Path) -> None:
    """Drive a real scan through `ScanWorker`/`QThread`, exactly like a user."""
    window._load_spd(path)
    _spin_until(app, lambda: not window._busy, timeout=15.0, what="threaded scan to finish")


# --------------------------------------------------------------------------- #
# window shell
# --------------------------------------------------------------------------- #


def test_window_title_and_size(make_window) -> None:
    window = make_window()
    assert APP_TITLE in window.windowTitle()
    assert "SPD Manipulator for PowerDC" in window.windowTitle()
    assert (window.size().width(), window.size().height()) == (1400, 860)
    assert (window.minimumSize().width(), window.minimumSize().height()) == (1100, 700)


def test_three_tabs_present_with_correct_table_models(make_window) -> None:
    window = make_window()
    assert window.tabs.count() == 3
    assert [window.tabs.tabText(i) for i in range(3)] == ["Net Manager", "VRMs", "Sinks"]

    assert isinstance(window.tabs.widget(0), NetManagerTab)
    assert isinstance(window.tabs.widget(1), VrmTab)
    assert isinstance(window.tabs.widget(2), SinkTab)

    assert type(window.net_tab.model) is NetTableModel
    assert type(window.vrm_tab.model) is VrmTableModel
    assert type(window.sink_tab.model) is SinkTableModel

    # every table view is a real BulkEditTableView wired to the shared stack.
    assert window.net_tab.view.undo_stack is window.undo_stack
    assert window.vrm_tab.view.undo_stack is window.undo_stack
    assert window.sink_tab.view.undo_stack is window.undo_stack


def test_status_bar_has_message_counts_and_hidden_progress_bar(make_window) -> None:
    window = make_window()
    assert "Open a .spd file" in window.status_label.text()
    assert window.counts_label.text() == "Nets 0 · Power 0 · Ground 0 · VRM 0 · Sink 0"
    assert not window.progress_bar.isVisible()
    assert not window.cancel_button.isVisible()


def test_toolbar_actions_have_expected_shortcuts(make_window) -> None:
    window = make_window()
    shortcuts = {
        window.open_action: "Ctrl+O",
        window.rescan_action: "F5",
        window.export_action: "Ctrl+E",
        window.save_config_action: "Ctrl+S",
        window.load_config_action: "Ctrl+L",
        window.undo_action: "Ctrl+Z",
        window.redo_action: "Ctrl+Y",
    }
    for action, sequence in shortcuts.items():
        assert action.shortcut().toString() == sequence


# --------------------------------------------------------------------------- #
# filter proxy shown-count label (design §C "12 / 3712 shown")
# --------------------------------------------------------------------------- #


def test_filter_proxy_count_label_updates(qapp: QApplication, make_window, tmp_path: Path) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    total = window.net_tab.model.rowCount()
    assert window.net_tab.shown_label.text() == f"{total} / {total} shown"

    window.net_tab.filter_edit.setText("vdd")
    assert window.net_tab.proxy.shownCount() < total
    shown = window.net_tab.proxy.shownCount()
    assert window.net_tab.shown_label.text() == f"{shown} / {total} shown"

    window.net_tab.filter_edit.setText("")
    assert window.net_tab.shown_label.text() == f"{total} / {total} shown"

    window.net_tab.class_combo.setCurrentText("Power")
    assert window.net_tab.shown_label.text() == "2 / {} shown".format(total)


# --------------------------------------------------------------------------- #
# real threaded scan (design §E) + worker strong-ref regression
# --------------------------------------------------------------------------- #


def test_scan_worker_populates_session_and_counts_label(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")

    _load_via_thread(qapp, window, spd)

    assert window.spd_path == spd
    assert window.session.scan is not None
    # `Session.load()` legitimately drops group-node/empty/duplicate entries
    # from the raw `scan_spd().nets` tuple -- compare against a parallel
    # `Session().load()` rather than the raw scan to pin down *this* window's
    # behavior without re-deriving core's own filtering rules here.
    reference = Session()
    reference.load(scan_spd(spd))
    assert set(window.session.nets) == set(reference.nets)
    counts = window.session.counts()
    assert window.counts_label.text() == (
        f"Nets {counts['nets']} · Power {counts['power']} · Ground {counts['ground']} · "
        f"VRM {counts['vrm']} · Sink {counts['sink']}"
    )
    assert counts["power"] == 2  # POWER_NET_A / POWER_NET_B (fixtures.py)
    assert window.net_tab.model.rowCount() == counts["nets"]
    assert window.vrm_tab.model.rowCount() == counts["power"]
    assert window.sink_tab.model.rowCount() == counts["power"]
    # v0.1.2 status line: "Loaded: N nets", plus the auto-classify tally only
    # when the load-time pass actually moved something (it does not here: the
    # miniature's `.NetList` already classifies both rails and the ground).
    assert window.status_label.text() == f"Loaded: {counts['nets']} nets"


def test_scan_worker_strong_ref_regression(qapp: QApplication, make_window, tmp_path: Path) -> None:
    """Regression: the scan worker must be kept alive (strong ref on self).

    Without ``self._scan_worker`` the worker QObject can be garbage-collected
    after `_load_spd()` returns, so ``started -> run`` never fires and the
    scan hangs forever. Mirrors the sibling app's
    ``test_load_spd_real_threaded_scan_completes``.
    """
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")

    window._load_spd(spd)
    assert window._busy, "busy state should be set while the scan runs"
    assert window._scan_worker is not None, "worker must be strongly referenced during the scan"
    assert window._scan_thread is not None

    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="threaded scan to finish")

    assert window.session.scan is not None
    assert window.spd_path == spd

    # the ref-clearing slot is guarded with `if self.sender() is self._scan_thread`
    # (design docstring); once the thread winds down the refs are released.
    _spin_until(
        qapp,
        lambda: window._scan_thread is None and window._scan_worker is None,
        timeout=15.0,
        what="scan thread/worker refs to be cleared",
    )


def test_rescan_reloads_from_disk(qapp: QApplication, make_window, tmp_path: Path) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    first_net_count = len(window.session.nets)

    window.session.set_net_voltage(POWER_NET_A, 42.0)
    assert window.session.nets[POWER_NET_A].voltage == 42.0

    window._rescan()
    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="rescan to finish")

    # a fresh scan resets the session (edits do not survive a rescan).
    assert window.session.nets[POWER_NET_A].voltage != 42.0
    assert len(window.session.nets) == first_net_count


def test_scan_failure_shows_error_and_resets_busy_state(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    missing = tmp_path / "does-not-exist.spd"

    window._load_spd(missing)
    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="failed scan to settle")

    assert window.session.scan is None
    assert "failed" in window.status_label.text().lower()
    assert not window.progress_bar.isVisible()
    assert window.open_action.isEnabled()


def test_open_spd_via_cli_initial_path(qapp: QApplication, make_window, tmp_path: Path) -> None:
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    window = make_window(initial_path=spd)
    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="initial-path scan to finish")
    assert window.spd_path == spd
    assert window.session.scan is not None


# --------------------------------------------------------------------------- #
# busy-state toggling
# --------------------------------------------------------------------------- #


def test_busy_state_disables_toolbar_actions_and_tabs(make_window) -> None:
    window = make_window()
    actions = (
        window.open_action,
        window.rescan_action,
        window.auto_classify_action,
        window.export_action,
        window.save_config_action,
        window.load_config_action,
    )
    assert all(action.isEnabled() for action in actions)
    assert window.tabs.isEnabled()

    window._set_busy(True)
    assert all(not action.isEnabled() for action in actions)
    assert not window.tabs.isEnabled()

    window._set_busy(False)
    assert all(action.isEnabled() for action in actions)
    assert window.tabs.isEnabled()


# --------------------------------------------------------------------------- #
# undo stack shared across tabs (design §C Ctrl+Z/Y)
# --------------------------------------------------------------------------- #


def test_undo_stack_is_shared_across_all_three_tabs(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    assert window.net_tab.view.undo_stack is window.undo_stack
    assert window.vrm_tab.view.undo_stack is window.undo_stack
    assert window.sink_tab.view.undo_stack is window.undo_stack
    assert isinstance(window.undo_stack, QUndoStack)
    assert window.undo_stack.count() == 0

    # a bulk edit on the Net Manager tab...
    net_model = window.net_tab.model
    net_row = next(
        r for r in range(net_model.rowCount()) if net_model.net_for_row(r) == POWER_NET_A
    )
    net_col = net_model.column_index("voltage")
    net_proxy_index = window.net_tab.proxy.mapFromSource(net_model.index(net_row, net_col))
    window.net_tab.view.apply_bulk("3.3", net_proxy_index)

    # ...and one on the VRM tab both land on the *same* stack.
    vrm_model = window.vrm_tab.model
    vrm_row = next(
        r for r in range(vrm_model.rowCount()) if vrm_model.net_for_row(r) == POWER_NET_B
    )
    vrm_col = vrm_model.column_index("output_current")
    window.vrm_tab.view.apply_bulk("2.5", vrm_model.index(vrm_row, vrm_col))

    assert window.undo_stack.count() == 2
    net_index = net_model.index(net_row, net_col)
    vrm_index = vrm_model.index(vrm_row, vrm_col)
    assert net_model.cell_text(net_index) == "3.3"
    assert vrm_model.cell_text(vrm_index) == "2.5"

    # undoing from the shared stack unwinds the *last* edit first, regardless
    # of which tab's view originally pushed it.
    window.undo_stack.undo()
    assert vrm_model.cell_text(vrm_index) != "2.5"
    assert net_model.cell_text(net_index) == "3.3"

    window.undo_stack.undo()
    assert net_model.cell_text(net_index) != "3.3"


# --------------------------------------------------------------------------- #
# cross-tab "Go to net" wiring (design §C context menu)
# --------------------------------------------------------------------------- #


def test_go_to_net_jumps_and_selects_across_tabs(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    window.tabs.setCurrentWidget(window.vrm_tab)
    # v0.1.2: every tab is sorted, and a sort that moves the target row is what
    # would break a jump that took a view row for a model row.
    window.vrm_tab.view.sortByColumn(
        window.vrm_tab.model.column_index("net"), Qt.SortOrder.DescendingOrder
    )
    window.vrm_tab.view.goToNetRequested.emit(POWER_NET_A)

    assert window.tabs.currentWidget() is window.net_tab
    net_view = window.net_tab.view
    proxy = net_view.model()
    net_col = window.net_tab.model.column_index("net")
    current = net_view.currentIndex()
    assert proxy.index(current.row(), net_col).data() == POWER_NET_A

    for tab in (window.vrm_tab, window.sink_tab):
        current = tab.view.currentIndex()
        assert tab.model.net_for_row(source_index(current).row()) == POWER_NET_A


# --------------------------------------------------------------------------- #
# export flow headless smoke (design §C)
# --------------------------------------------------------------------------- #


def test_export_flow_produces_expected_dc_output(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch every interactive dialog to auto-accept; the resulting file
    must equal `expected_dc()` for the si fixture, exactly like the writer's
    own golden-file test."""
    window = make_window()
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    _load_via_thread(qapp, window, source)
    assert window.session.validate() == []  # no issues dialog should be needed

    output_path = tmp_path / "in_DC.spd"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(output_path), "")),
    )
    monkeypatch.setattr(
        _ExportOptionsDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    confirm_calls: list[int] = []
    monkeypatch.setattr(
        MainWindow,
        "_confirm_validation_issues",
        lambda self, issues: confirm_calls.append(len(issues)) or True,
    )

    window._export_dc_spd()
    assert window._busy
    assert window._export_worker is not None

    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="export to finish")

    assert not confirm_calls  # validate() was clean, so the issues dialog never ran
    assert output_path.exists()
    assert output_path.read_bytes() == expected_dc().encode("utf-8")
    assert "Exported" in window.status_label.text()
    assert not window.progress_bar.isVisible()
    assert not window.cancel_button.isVisible()

    _spin_until(
        qapp,
        lambda: window._export_thread is None and window._export_worker is None,
        timeout=15.0,
        what="export thread/worker refs to be cleared",
    )


def test_export_destination_guarded_against_the_source_path(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window()
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    _load_via_thread(qapp, window, source)

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )
    # the same-file guard pops a real modal `QMessageBox.warning` (design
    # §C) -- headless `.exec()` never returns without a click, so simulate
    # the user dismissing it.
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(
            lambda *a, **k: warned.append("warned") or QMessageBox.StandardButton.Ok
        ),
    )
    result = window._prompt_export_destination(source.with_name("in_DC.spd"))
    assert result is None  # writing over the source must be refused in the UI too
    assert warned
    assert not window._busy


def test_export_cancel_deletes_partial_output(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    # `QWidget.isVisible()` reflects the whole ancestor chain, including the
    # top-level window -- without `show()` the button stays report-invisible
    # even after `setVisible(True)`, regardless of the export's own state.
    window.show()
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    _load_via_thread(qapp, window, source)

    output_path = tmp_path / "in_DC.spd"
    plan = window.session.build_plan({})
    window._start_export(output_path, plan)
    assert window.cancel_button.isVisible()

    window._cancel_export()
    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="cancelled export to settle")

    assert not output_path.exists()
    assert not (tmp_path / f"{output_path.name}.part").exists()
    assert "cancel" in window.status_label.text().lower()
    assert not window.cancel_button.isVisible()

    _spin_until(
        qapp,
        lambda: window._export_thread is None and window._export_worker is None,
        timeout=15.0,
        what="cancelled export thread/worker refs to be cleared",
    )


# --------------------------------------------------------------------------- #
# Save / Load config (design §C)
# --------------------------------------------------------------------------- #


def test_save_and_load_config_round_trips_through_models(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window()
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    _load_via_thread(qapp, window, source)

    net_model = window.net_tab.model
    row = next(r for r in range(net_model.rowCount()) if net_model.net_for_row(r) == POWER_NET_A)
    voltage_col = net_model.column_index("voltage")
    net_model.setData(net_model.index(row, voltage_col), "0.42")

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(config_path), ""))
    )
    window._save_config()
    assert config_path.exists()

    # a second window loads the same scan, then overlays the saved config.
    other = make_window()
    _load_via_thread(qapp, other, source)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(config_path), ""))
    )
    other._load_config()

    other_model = other.net_tab.model
    other_row = next(
        r for r in range(other_model.rowCount()) if other_model.net_for_row(r) == POWER_NET_A
    )
    assert other_model.cell_text(other_model.index(other_row, voltage_col)) == "0.42"
    assert "Loaded config" in other.status_label.text()


# --------------------------------------------------------------------------- #
# Net Manager context menu: Classify (v0.1.1, PowerSI's own right-click wording)
# --------------------------------------------------------------------------- #


def _net_row(model, net: str) -> int:
    return next(r for r in range(model.rowCount()) if model.net_for_row(r) == net)


def _net_class(model, net: str) -> str:
    return model.data(model.index(_net_row(model, net), model.column_index("net_class")))


def _menu_titles(view) -> list[str]:
    return [action.text() for action in view.build_context_menu().actions() if action.text()]


def _menu_action(view, title: str):
    """The context-menu entry titled *title* (top level or inside a submenu)."""
    for action in view.build_context_menu().actions():
        if action.text() == title:
            return action
        submenu = action.menu()
        if submenu is not None:
            for entry in submenu.actions():
                if entry.text() == title:
                    return entry
    raise AssertionError(f"no context-menu entry {title!r} in {_menu_titles(view)}")


def test_net_table_context_menu_leads_with_the_classify_submenu(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    titles = _menu_titles(window.net_tab.view)
    assert titles[:2] == ["Classify", "Set voltage…"]
    assert titles[2] == "Set value…"  # the shared design §C entries follow
    # the multi-row ground picker is a shared entry -- not duplicated, still there
    assert "Set paired ground…" in titles

    submenu = next(
        action.menu()
        for action in window.net_tab.view.build_context_menu().actions()
        if action.text() == "Classify"
    )
    assert [action.text() for action in submenu.actions()] == [
        "as PowerNets",
        "as GroundNets",
        "as Signal Nets",
    ]

    # the VRM/Sink tabs share `BulkEditTableView` and must NOT grow the submenu
    for tab in (window.vrm_tab, window.sink_tab):
        assert "Classify" not in _menu_titles(tab.view)
        assert "Set voltage…" not in _menu_titles(tab.view)


def test_classify_menu_applies_to_a_multi_row_proxy_selection_and_undoes_once(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    tab = window.net_tab
    model = tab.model

    # a filter shifts every visible row, so the menu has to map its selection
    # back through `NetFilterProxy` to reach the right `Session` rows.
    tab.filter_edit.setText("_gs")
    nets = (SENSE_NETS[1], SENSE_NETS[3])
    assert tab.proxy.shownCount() == 2
    assert [_net_row(model, net) for net in nets] != [0, 1]
    tab.view.selectAll()

    before = window.undo_stack.count()
    _menu_action(tab.view, "as GroundNets").trigger()

    assert [_net_class(model, net) for net in nets] == ["ground", "ground"]
    assert window.undo_stack.count() == before + 1  # ONE command for both rows
    assert window.status_label.text() == "Classified 2 nets as GroundNets."

    window.undo_stack.undo()  # ...so one Ctrl+Z restores both
    assert [_net_class(model, net) for net in nets] == ["none", "none"]


def test_classify_as_signal_nets_clears_a_class_and_skips_no_op_rows(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    tab = window.net_tab
    model = tab.model

    proxy_row = tab.proxy.mapFromSource(model.index(_net_row(model, GROUND_NET), 0)).row()
    tab.view.selectRow(proxy_row)

    before = window.undo_stack.count()
    _menu_action(tab.view, "as Signal Nets").trigger()
    assert _net_class(model, GROUND_NET) == "none"
    assert window.undo_stack.count() == before + 1

    # the row is already unclassified now: a second run is a no-op, not a command
    _menu_action(tab.view, "as Signal Nets").trigger()
    assert window.undo_stack.count() == before + 1

    window.undo_stack.undo()
    assert _net_class(model, GROUND_NET) == "ground"


def test_net_menu_voltage_and_ground_dialogs_cover_the_whole_selection(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    tab = window.net_tab
    model = tab.model
    # a second ground to pick from, so "Set paired ground…" has a real choice
    second_ground = UNCLASSIFIED_NETS[0]
    window.session.set_class(second_ground, "ground")
    window._refresh_all_models()

    tab.class_combo.setCurrentText("Power")
    assert tab.proxy.shownCount() == 2
    tab.view.selectAll()

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (0.9, True)))
    _menu_action(tab.view, "Set voltage…").trigger()
    voltage_col = model.column_index("voltage")
    for net in (POWER_NET_A, POWER_NET_B):
        assert model.cell_text(model.index(_net_row(model, net), voltage_col)) == "0.9"
    assert window.status_label.text() == "Set 0.9 V on 2 nets."

    monkeypatch.setattr(
        QInputDialog, "getItem", staticmethod(lambda *a, **k: (second_ground, True))
    )
    _menu_action(tab.view, "Set paired ground…").trigger()
    gnd_col = model.column_index("paired_gnd")
    for net in (POWER_NET_A, POWER_NET_B):
        assert model.cell_text(model.index(_net_row(model, net), gnd_col)) == second_ground


def test_net_manager_auto_classify_toolbar_action(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    # one power net dropped back to unclassified (its name still says `_VDD_`),
    # and -- `Session.set_paired_ground(net, "")` immediately re-fills an empty
    # pair via `_sync_rows()`'s own auto-choose -- one directly-unpaired net.
    window.session.set_class(POWER_NET_A, "none")
    window.session.nets[POWER_NET_B].paired_gnd = ""
    window._refresh_all_models()

    window._auto_classify()

    # name-based classification filled the gap the input left ...
    assert window.session.nets[POWER_NET_A].net_class == "power"
    assert window.session.nets[POWER_NET_A].voltage == pytest.approx(0.7)
    # ... without touching signal nets or the `_PS`/`_GS` sense nets ...
    for net in (*UNCLASSIFIED_NETS, *SENSE_NETS):
        assert window.session.nets[net].net_class == "none", net
    # ... and the ground pairing was re-run.
    assert window.session.nets[POWER_NET_B].paired_gnd == GROUND_NET
    assert window.status_label.text() == "Auto-classify: +1 power, +0 ground"
    assert _net_class(window.net_tab.model, POWER_NET_A) == "power"


# --------------------------------------------------------------------------- #
# v0.1.2: auto-classification at load time
# --------------------------------------------------------------------------- #


def test_loading_an_unclassified_spd_classifies_it_before_the_first_repaint(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    """The field-reported case: a `.spd` PowerSI never classified used to open
    as N unclassified rows -- no power nets, no VRM/Sink rows, no grounds."""
    window = make_window()
    spd = tmp_path / "unclassified.spd"
    build_mini_spd(spd, style="si", classified=False)

    # the load-time pass is the only thing that classifies here
    reference = Session()
    reference.load(scan_spd(spd))
    assert reference.counts()["power"] == 0 and reference.counts()["ground"] == 0

    painted: list[int] = []
    window.net_tab.model.modelReset.connect(
        lambda: painted.append(window.session.counts()["power"])
    )
    _load_via_thread(qapp, window, spd)

    counts = window.session.counts()
    assert (counts["power"], counts["ground"]) == (2, 1)
    assert window.session.nets[POWER_NET_A].net_class == "power"
    assert window.session.nets[GROUND_NET].net_class == "ground"
    assert window.session.nets[POWER_NET_A].paired_gnd == GROUND_NET
    # ...and the VRM/Sink tabs already have their rows
    assert window.vrm_tab.model.rowCount() == 2
    assert window.sink_tab.model.rowCount() == 2
    # the classes were in place *before* the first model reset painted anything
    assert painted and all(power == 2 for power in painted)

    assert window.status_label.text() == (
        f"Loaded: {counts['nets']} nets · auto-classified +2 power +1 ground"
    )
    # signal + `_PS`/`_GS` sense nets stay out of it (§G.4)
    for net in (*UNCLASSIFIED_NETS, *SENSE_NETS):
        assert window.session.nets[net].net_class == "none", net


def test_load_time_auto_classify_never_overrides_the_input_files_own_markers(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    """`.NetList` classification preloads first; the name pass only fills gaps."""
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")  # POWER_NET_A/B + GROUND_NET already classified
    _load_via_thread(qapp, window, spd)

    source_col = window.net_tab.model.column_index("source")

    def source_of(net: str) -> str:
        model = window.net_tab.model
        return model.cell_text(model.index(_net_row(model, net), source_col))

    assert source_of(POWER_NET_A) == "input"
    assert source_of(GROUND_NET) == "input"
    assert source_of(UNCLASSIFIED_NETS[0]) == ""
    # nothing was left for the name pass, so the status line drops the tally
    counts = window.session.counts()
    assert window.status_label.text() == f"Loaded: {counts['nets']} nets"


def test_source_column_reports_auto_then_user_after_a_hand_classification(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "unclassified.spd"
    build_mini_spd(spd, style="si", classified=False)
    _load_via_thread(qapp, window, spd)
    model = window.net_tab.model
    source_col = model.column_index("source")

    def source_of(net: str) -> str:
        return model.cell_text(model.index(_net_row(model, net), source_col))

    assert source_of(POWER_NET_A) == "auto"  # the load-time name pass did it

    row = window.net_tab.proxy.mapFromSource(model.index(_net_row(model, POWER_NET_A), 0))
    window.net_tab.view.selectRow(row.row())
    window.net_tab.classify_selection("ground")
    assert source_of(POWER_NET_A) == "user"
    assert _net_class(model, POWER_NET_A) == "ground"

    # Undo puts the *class* back by replaying the same user-facing setter, so
    # the origin stays "user": the column records who last decided this net's
    # class, and after a Ctrl+Z on a hand classification that is still the user.
    window.undo_stack.undo()
    assert _net_class(model, POWER_NET_A) == "power"
    assert source_of(POWER_NET_A) == "user"

    assert model.headerData(
        source_col, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole
    ) == "Where this net's class came from"


def test_paired_gnd_editor_offers_a_net_classified_ground_from_the_context_menu(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    """v0.1.2 bug fix, end to end: classify X as ground, open the editor, X is there."""
    window = make_window()
    spd = tmp_path / "unclassified.spd"
    build_mini_spd(spd, style="si", classified=False)
    _load_via_thread(qapp, window, spd)
    tab = window.net_tab
    model = tab.model
    gnd_col = model.column_index("paired_gnd")

    def combo_items(net: str) -> list[str]:
        index = tab.proxy.mapFromSource(model.index(_net_row(model, net), gnd_col))
        delegate = tab.view.itemDelegateForColumn(gnd_col)
        editor = delegate.createEditor(tab.view.viewport(), QStyleOptionViewItem(), index)
        return [editor.itemText(i) for i in range(editor.count())]

    assert model.is_editable(model.index(_net_row(model, POWER_NET_A), gnd_col))
    assert combo_items(POWER_NET_A) == ["", GROUND_NET]
    assert UNCLASSIFIED_NETS[0] not in combo_items(POWER_NET_A)

    row = tab.proxy.mapFromSource(model.index(_net_row(model, UNCLASSIFIED_NETS[0]), 0))
    tab.view.selectRow(row.row())
    assert tab.classify_selection("ground") == 1

    assert UNCLASSIFIED_NETS[0] in combo_items(POWER_NET_A)


def test_check_all_shown_from_the_net_table_context_menu(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    tab = window.net_tab

    assert "Check all (shown)" in _menu_titles(tab.view)
    assert "Uncheck all (shown)" in _menu_titles(tab.view)
    assert "Check selected" in _menu_titles(tab.view)  # the per-selection pair stays

    tab.filter_edit.setText("_gs")
    assert tab.proxy.shownCount() == 2
    tab.view.clearSelection()

    before = window.undo_stack.count()
    _menu_action(tab.view, "Uncheck all (shown)").trigger()

    assert [window.session.nets[net].selected for net in (SENSE_NETS[1], SENSE_NETS[3])] == [
        False,
        False,
    ]
    assert window.session.nets[POWER_NET_A].selected is True  # filtered out -> untouched
    assert window.undo_stack.count() == before + 1  # ONE undoable command
    assert window.status_label.text() == "Unchecked 2 shown rows."

    window.undo_stack.undo()
    assert window.session.nets[SENSE_NETS[1]].selected is True


def test_sorting_is_enabled_on_all_three_tables_and_survives_a_rescan(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    for tab in (window.net_tab, window.vrm_tab, window.sink_tab):
        assert tab.view.isSortingEnabled()
        assert tab.view.horizontalHeader().isSortIndicatorShown()
        assert tab.view.model() is tab.proxy
        assert tab.proxy.sourceModel() is tab.model

    voltage_col = window.net_tab.model.column_index("voltage")
    window.net_tab.view.sortByColumn(voltage_col, Qt.SortOrder.DescendingOrder)
    proxy = window.net_tab.proxy
    voltages = [proxy.index(r, voltage_col).data(SORT_ROLE) for r in range(proxy.rowCount())]
    assert voltages == sorted(voltages, reverse=True)

    # a rescan resets the models; the view must still be sorting
    window._rescan()
    _spin_until(qapp, lambda: not window._busy, 10.0, "rescan")
    assert window.net_tab.view.isSortingEnabled()
    assert window.net_tab.proxy.rowCount() == window.net_tab.model.rowCount()


# --------------------------------------------------------------------------- #
# closing mid-scan (regression: SIGABRT from a QThread destroyed while running)
# --------------------------------------------------------------------------- #


def _blocking_scan(release: threading.Event, entered: threading.Event, *, honour_cancel: bool):
    """A `scan_spd` stand-in that stays inside the worker thread until released.

    The mini fixture scans in microseconds, so a *real* mid-scan close is
    unraceable; this stands in for the 1.4 GB file, where the scan genuinely
    runs for tens of seconds after the user hits the X.
    """

    def _scan(path, progress=None, cancel=None):
        assert cancel is not None, "ScanWorker must pass its cancel event to scan_spd"
        entered.set()
        if honour_cancel:
            # the real `scan_spd` polls this per line batch and raises
            if not cancel.wait(15.0):
                raise AssertionError("closeEvent never set the scan's cancel event")
        else:
            release.wait(15.0)
        raise ScanCancelled(f"Scan of {Path(path).name} cancelled.")

    return _scan


def test_close_mid_scan_cancels_the_scan_and_accepts_the_close(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `closeEvent` used to destroy a still-running scan QThread.

    Qt aborts the process for that (``QThread: Destroyed while thread is still
    running`` -> SIGABRT). The window must instead set the scan's cancel event,
    let the thread wind down, and accept the close -- without popping an error
    dialog for a failure the user themselves asked for.
    """
    entered = threading.Event()
    monkeypatch.setattr(
        workers_mod, "scan_spd", _blocking_scan(threading.Event(), entered, honour_cancel=True)
    )
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        MainWindow, "_show_error", lambda self, title, message: shown.append((title, message))
    )

    window = make_window()
    window._load_spd(tmp_path / "huge.spd")
    _spin_until(qapp, entered.is_set, timeout=15.0, what="the scan to reach the worker thread")
    assert window._scan_thread is not None and window._scan_thread.isRunning()
    assert window._scan_worker is not None

    assert window.close() is True, "the close must be accepted, not deferred"
    assert not window._scan_thread.isRunning(), "the thread must be joined before teardown"

    for _ in range(10):
        qapp.processEvents()
    assert shown == [], "a cancelled scan is not an error -- no dialog"
    assert "cancel" in window.status_label.text().lower()
    assert window.session.scan is None


def test_close_defers_when_a_scan_outlives_the_wait_instead_of_forcing_teardown(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread that ignores the cancel must postpone the close, not be destroyed.

    `CLOSE_WAIT_MS` is shortened so the wait provably expires; the close is then
    ignored and re-issued from the thread's own `finished` signal.
    """
    monkeypatch.setattr(main_window_mod, "CLOSE_WAIT_MS", 1)
    release = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(
        workers_mod, "scan_spd", _blocking_scan(release, entered, honour_cancel=False)
    )
    monkeypatch.setattr(MainWindow, "_show_error", lambda self, title, message: None)

    window = make_window()
    # isVisible() only reports meaningfully for a shown window (see the export
    # cancel test), and it is how the deferred close is observed here.
    window.show()
    window._load_spd(tmp_path / "huge.spd")
    _spin_until(qapp, entered.is_set, timeout=15.0, what="the scan to reach the worker thread")

    assert window.close() is False, "the close must be deferred while the thread runs"
    assert window.isVisible(), "the window stays up rather than tearing down a live QThread"
    assert window._closing

    release.set()
    _spin_until(
        qapp,
        lambda: not window.isVisible(),
        timeout=15.0,
        what="the deferred close to land once the thread finished",
    )
    assert window._scan_thread is None


# --------------------------------------------------------------------------- #
# shared undo stack lifetime (regression: stale commands across sessions)
# --------------------------------------------------------------------------- #


def _push_voltage_edit(window: MainWindow, net: str, value: str) -> None:
    model = window.net_tab.model
    row = next(r for r in range(model.rowCount()) if model.net_for_row(r) == net)
    col = model.column_index("voltage")
    index = window.net_tab.proxy.mapFromSource(model.index(row, col))
    window.net_tab.view.apply_bulk(value, index)


def test_undo_stack_is_cleared_by_a_new_scan_and_by_a_config_load(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the shared stack outlived the session its commands describe.

    `BulkEditCommand`s hold row/column keys into the `Session` that was live
    when they were pushed. Re-scanning a file (or overlaying a config JSON)
    replaces that config wholesale, so an undo/redo afterwards wrote a stale
    edit onto the *new* session -- and the toolbar's Undo stayed enabled,
    advertising it.
    """
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    _push_voltage_edit(window, POWER_NET_A, "3.3")
    assert window.undo_stack.count() == 1
    assert window.undo_action.isEnabled()

    # a rescan rebuilds the session from disk ...
    window._rescan()
    _spin_until(qapp, lambda: not window._busy, timeout=15.0, what="rescan to finish")
    assert window.undo_stack.count() == 0
    assert not window.undo_stack.canUndo() and not window.undo_stack.canRedo()
    assert not window.undo_action.isEnabled() and not window.redo_action.isEnabled()

    # ... and so does opening a different file.
    other = tmp_path / "other.spd"
    build_mini_spd(other, style="dc")
    _push_voltage_edit(window, POWER_NET_B, "2.5")
    assert window.undo_stack.count() == 1
    _load_via_thread(qapp, window, other)
    assert window.undo_stack.count() == 0

    # Load Config replaces the config wholesale too.
    config_path = tmp_path / "config.json"
    config_path.write_text(window.session.to_json(), encoding="utf-8")
    _push_voltage_edit(window, POWER_NET_A, "1.8")
    assert window.undo_stack.count() == 1

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(config_path), ""))
    )
    window._load_config()
    assert "Loaded config" in window.status_label.text()
    assert window.undo_stack.count() == 0
    assert not window.undo_action.isEnabled()


def test_failed_config_load_leaves_the_undo_stack_alone(
    qapp: QApplication, make_window, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was replaced, so the stack must survive (the clear is not blanket)."""
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)
    _push_voltage_edit(window, POWER_NET_A, "3.3")
    assert window.undo_stack.count() == 1

    monkeypatch.setattr(MainWindow, "_show_error", lambda self, title, message: None)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "missing.json"), "")),
    )
    window._load_config()
    assert window.undo_stack.count() == 1
