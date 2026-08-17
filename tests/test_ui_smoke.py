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

import time  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtGui import QUndoStack  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox  # noqa: E402

from fixtures import (  # noqa: E402
    GROUND_NET,
    POWER_NET_A,
    POWER_NET_B,
    build_mini_spd,
    expected_dc,
)
from powerdc_setup_tool.core.session import Session  # noqa: E402
from powerdc_setup_tool.core.spd_scan import scan_spd  # noqa: E402
from powerdc_setup_tool.ui.main_window import APP_TITLE, MainWindow, _ExportOptionsDialog  # noqa: E402
from powerdc_setup_tool.ui.models import NetTableModel, SinkTableModel, VrmTableModel  # noqa: E402
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
    assert "mini.spd" in window.status_label.text()


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
    window.vrm_tab.view.goToNetRequested.emit(POWER_NET_A)

    assert window.tabs.currentWidget() is window.net_tab
    net_view = window.net_tab.view
    proxy = net_view.model()
    net_col = window.net_tab.model.column_index("net")
    current = net_view.currentIndex()
    assert proxy.index(current.row(), net_col).data() == POWER_NET_A

    assert window.vrm_tab.model.net_for_row(window.vrm_tab.view.currentIndex().row()) == POWER_NET_A
    assert window.sink_tab.model.net_for_row(window.sink_tab.view.currentIndex().row()) == POWER_NET_A


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
# Net Manager toolbar buttons (Mark Power / Mark Ground / Clear class / ...)
# --------------------------------------------------------------------------- #


def test_net_manager_mark_power_and_ground_buttons(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    model = window.net_tab.model
    view = window.net_tab.view
    proxy = window.net_tab.proxy
    row = next(r for r in range(model.rowCount()) if model.net_for_row(r) == GROUND_NET)
    proxy_row = proxy.mapFromSource(model.index(row, 0)).row()
    view.selectRow(proxy_row)

    before = window.undo_stack.count()
    window.net_tab._clear_class()
    assert model.data(model.index(row, model.column_index("net_class"))) == "none"
    assert window.undo_stack.count() == before + 1

    window.undo_stack.undo()
    assert model.data(model.index(row, model.column_index("net_class"))) == "ground"


def test_net_manager_auto_classify_toolbar_action(
    qapp: QApplication, make_window, tmp_path: Path
) -> None:
    window = make_window()
    spd = tmp_path / "mini.spd"
    build_mini_spd(spd, style="si")
    _load_via_thread(qapp, window, spd)

    # `Session.set_paired_ground(net, "")` immediately re-fills an empty pair
    # via `_sync_rows()`'s own auto-choose (there is only one ground net in
    # this fixture, so it always finds it) -- mutate the field directly to
    # get a durably-unpaired net to exercise the toolbar action against.
    window.session.nets[POWER_NET_A].paired_gnd = ""
    window._refresh_all_models()
    assert window.session.nets[POWER_NET_A].paired_gnd == ""

    window._auto_classify()

    assert window.session.nets[POWER_NET_A].paired_gnd == GROUND_NET
    assert "Auto-classification" in window.status_label.text()
