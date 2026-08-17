"""`core/session.py` -- design §E `test_session` row.

Covers the design §B voltage-propagation rule, §C autopair/export flow, §D
`build_plan`, and the §G.1-4 edge cases, plus the first true cross-module
integration test: si fixture -> `scan_spd` -> `Session` -> `build_plan` ->
`write_spd` == `expected_dc()`.

Unlike `test_writer.py`, this module deliberately *does* use the real scanner:
chunk 3's job is to sit between chunks 1/2, so the fixture scans are the
contract under test. Corner cases the miniature fixture cannot express (a
second ground net, a component with power pins but no ground pins) get a
hand-built `ScanResult` whose netlist entries still come from the real
`parse_netlist`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fixtures
from fixtures import (
    GROUND_NET,
    POWER_NET_A,
    POWER_NET_B,
    SENSE_NETS,
    UNCLASSIFIED_NETS,
    build_mini_spd,
    expected_dc,
)
from powerdc_setup_tool.core.model import CircuitInfo, PinMapIndex, ScanResult
from powerdc_setup_tool.core.netlist import parse_netlist, render_netlist
from powerdc_setup_tool.core.session import (
    Session,
    net_key,
    sink_key,
    split_key,
    vrm_key,
)
from powerdc_setup_tool.core.spd_scan import scan_spd
from powerdc_setup_tool.core.writer import write_spd

VRM_COMP = fixtures.VRM_COMP  # "LGA"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _scan(tmp_path: Path, style: str = "si", *, name: str = "mini.spd") -> ScanResult:
    path = tmp_path / name
    build_mini_spd(path, style=style)  # type: ignore[arg-type]
    return scan_spd(path)


def _session(tmp_path: Path, style: str = "si") -> Session:
    session = Session()
    session.load(_scan(tmp_path, style))
    return session


def _pins(prefix: str, count: int, start: int = 1) -> tuple[tuple[str, str], ...]:
    return tuple((f"{prefix}{i}", f"Node{prefix}{i}") for i in range(start, start + count))


def _make_scan(
    body: str,
    by_circuit: dict[str, dict[str, tuple[tuple[str, str], ...]]],
    **overrides: object,
) -> ScanResult:
    """A `ScanResult` with real netlist entries and hand-written pin maps.

    Byte offsets are dummies: none of the tests using this write a file.
    """
    fields: dict[str, object] = dict(
        path=Path("synthetic.spd"),
        file_size=len(body),
        elapsed_s=0.0,
        workflow_key_span=None,
        powerdc_span=(0, 0),
        anchor_pdc_elem_end=0,
        anchor_spice_end=0,
        existing_blocks_span=None,
        netlist_span=(0, 0),
        netlist_body_span=(0, len(body)),
        netlist_body=body,
        nets=tuple(parse_netlist(body)),
        pin_maps=PinMapIndex(by_circuit=by_circuit),
        circuits=tuple(
            CircuitInfo(name=name, pin_count=sum(len(p) for p in nets.values()))
            for name, nets in by_circuit.items()
        ),
        other_circuit_names=(),
        existing_vrms=(),
        existing_sinks=(),
        warnings=(),
    )
    fields.update(overrides)
    return ScanResult(**fields)  # type: ignore[arg-type]


TWO_GROUND_BODY = (
    "\t::Unselected||DropShape RiseTime = 0ps %Coupling = 0\n"
    "\tGroundNets Color = LIME\n"
    "\tPowerNets Color = RED\n"
    "\tVSS::Unselected||DropShape Color = GREEN\n"
    "\tDGND -> GroundNets Color = GREEN Voltage = 0\n"
    "\tP_VDD_075_MAIN/0 -> PowerNets Color = RED\n"
)


def _two_ground_scan(**overrides: object) -> ScanResult:
    """`DGND` (4 LGA pins) vs `VSS` (1 LGA pin, unclassified but name-matched)."""
    return _make_scan(
        TWO_GROUND_BODY,
        {
            "ALI1": {},
            "C9_0": {"P_VDD_075_MAIN/0": _pins("c", 1), "DGND": _pins("d", 1)},
            "SITE0": {"P_VDD_075_MAIN/0": _pins("s", 2), "DGND": _pins("t", 2)},
            "LGA": {
                "P_VDD_075_MAIN/0": _pins("p", 3),
                "DGND": _pins("g", 4),
                "VSS": _pins("v", 1),
            },
        },
        **overrides,
    )


def _keys(changed: list[tuple[str, str]]) -> set[tuple[str, str]]:
    return set(changed)


# --------------------------------------------------------------------------- #
# row keys
# --------------------------------------------------------------------------- #


def test_row_keys_round_trip() -> None:
    assert split_key(net_key(POWER_NET_A)) == ("net", POWER_NET_A, -1)
    assert split_key(vrm_key(POWER_NET_A, 2)) == ("vrm", POWER_NET_A, 2)
    assert split_key(sink_key(GROUND_NET)) == ("sink", GROUND_NET, 0)
    assert split_key("vrm:NET_NO_ROW_ID") == ("vrm", "NET_NO_ROW_ID", 0)
    with pytest.raises(ValueError):
        split_key("bogus:NET#0")


# --------------------------------------------------------------------------- #
# load / preload (design §G.1, §G.2)
# --------------------------------------------------------------------------- #


def test_load_preloads_classification_from_the_netlist(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert session.nets[POWER_NET_A].net_class == "power"
    assert session.nets[GROUND_NET].net_class == "ground"
    assert session.nets[UNCLASSIFIED_NETS[0]].net_class == "none"
    # design §C "Source" column: `input` iff the file already classified it.
    assert session.nets[POWER_NET_A].from_input is True
    assert session.nets[GROUND_NET].from_input is True
    assert session.nets[UNCLASSIFIED_NETS[0]].from_input is False
    # group nodes and the empty-named root entry are never nets
    assert "PowerNets" not in session.nets and "GroundNets" not in session.nets
    assert "" not in session.nets
    assert session.nets[POWER_NET_A].die == 0 and session.nets[POWER_NET_B].die == 1


def test_load_auto_guesses_voltage_with_a_1v_fallback(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert session.nets[POWER_NET_A].voltage == pytest.approx(0.7)
    assert session.nets[POWER_NET_B].voltage == pytest.approx(1.2)
    assert session.nets[UNCLASSIFIED_NETS[0]].voltage == pytest.approx(1.0)
    # nothing the user typed -> nothing is an override
    assert not any(cfg.voltage_override for cfg in session.net_rows)
    # the ground net's `Voltage = 0` netlist attribute is its auto value, not an edit
    assert session.nets[GROUND_NET].voltage == 0.0
    assert session.nets[GROUND_NET].voltage_override is False


def test_load_derives_one_vrm_and_one_sink_per_power_net(tmp_path: Path) -> None:
    session = _session(tmp_path)
    assert [row.net for row in session.vrm_rows] == [POWER_NET_A, POWER_NET_B]
    assert [row.net for row in session.sink_rows] == [POWER_NET_A, POWER_NET_B]
    # §G.3: VRM comp = non-SITE, non-`.OtherCircuit` circuit with most ground pins
    assert [row.comp for row in session.vrm_rows] == [VRM_COMP, VRM_COMP]
    # §G.3 / spec §6: Sink comp = SITE{die}
    assert [row.comp for row in session.sink_rows] == ["SITE0", "SITE1"]
    assert {row.gnet for row in (*session.vrm_rows, *session.sink_rows)} == {GROUND_NET}
    assert [row.nominal_voltage for row in session.vrm_rows] == [
        pytest.approx(0.7),
        pytest.approx(1.2),
    ]
    assert [row.output_current for row in session.vrm_rows] == [1.0, 1.0]
    assert [(r.model, r.pf_mode, r.pin_equal_current) for r in session.sink_rows] == [
        (2, 2, 1),
        (2, 2, 1),
    ]


def test_existing_blocks_preload_from_the_dc_fixture(tmp_path: Path) -> None:
    # §G.2: parse headers, preload for editing, never append a second run.
    scan = _scan(tmp_path, "dc")
    assert len(scan.existing_vrms) == 2 and len(scan.existing_sinks) == 2
    session = Session()
    session.load(scan)

    assert len(session.vrm_rows) == 2 and len(session.sink_rows) == 2
    assert [row.row_id for row in session.vrm_rows] == [0, 0]
    assert [row.comp for row in session.vrm_rows] == [VRM_COMP, VRM_COMP]
    assert [row.comp for row in session.sink_rows] == ["SITE0", "SITE1"]
    # design §C: the ground comes from the parsed block, not from autopair
    assert session.nets[POWER_NET_A].paired_gnd == GROUND_NET
    # the converter's values match the name-derived ones -> still auto, not overrides
    assert [row.nominal_voltage for row in session.sink_rows] == [
        pytest.approx(0.7),
        pytest.approx(1.2),
    ]
    assert not any(row.nominal_override for row in session.sink_rows)
    assert not any(row.sense_override or row.current_override for row in session.vrm_rows)
    assert session.validate() == []


def test_existing_block_values_that_disagree_with_the_guess_become_overrides(
    tmp_path: Path,
) -> None:
    # spec §5 documents one hand-edited Sink; that number must survive a reload.
    path = tmp_path / "hand_edited.spd"
    build_mini_spd(path, style="dc")
    data = path.read_bytes()
    old = b'.Sink NominalVoltage = 0.7 Current = 1 '
    assert old in data
    path.write_bytes(data.replace(old, b".Sink NominalVoltage = 0.5 Current = 3 ", 1))

    session = Session()
    session.load(scan_spd(path))
    sink = session.sink(POWER_NET_A)
    assert sink is not None
    assert sink.nominal_voltage == pytest.approx(0.5) and sink.nominal_override is True
    assert sink.current == pytest.approx(3.0) and sink.current_override is True
    # ... and it sticks through a later net-voltage edit (design §B)
    session.set_net_voltage(POWER_NET_A, 0.9)
    assert sink.nominal_voltage == pytest.approx(0.5)
    # while `reset_auto` is the one-click way back to the derived value
    session.reset_auto([(sink_key(POWER_NET_A), "nominal_voltage")])
    assert sink.nominal_voltage == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# voltage propagation (design §B, verbatim)
# --------------------------------------------------------------------------- #


def test_net_voltage_edit_propagates_into_every_non_overridden_derived_field(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    changed = session.set_net_voltage(POWER_NET_A, 0.9)

    cfg = session.nets[POWER_NET_A]
    vrm = session.vrm(POWER_NET_A)
    sink = session.sink(POWER_NET_A)
    assert cfg.voltage == pytest.approx(0.9) and cfg.voltage_override is True
    assert vrm is not None and sink is not None
    assert vrm.nominal_voltage == pytest.approx(0.9)
    assert vrm.sense_voltage == pytest.approx(0.9)  # spec §9: {v_sense} = {v_nom}
    assert sink.nominal_voltage == pytest.approx(0.9)
    # the other net is untouched
    assert session.nets[POWER_NET_B].voltage == pytest.approx(1.2)

    assert _keys(changed) == {
        (net_key(POWER_NET_A), "voltage"),
        (net_key(POWER_NET_A), "voltage_override"),
        (vrm_key(POWER_NET_A), "nominal_voltage"),
        (vrm_key(POWER_NET_A), "sense_voltage"),
        (sink_key(POWER_NET_A), "nominal_voltage"),
    }


def test_sense_voltage_follows_its_own_rows_nominal(tmp_path: Path) -> None:
    # spec §9 `{v_sense}` = `{v_nom}`: an auto sense cell tracks the VRM's
    # nominal, which is the §B net voltage only while the nominal is itself auto.
    session = _session(tmp_path)
    session.set_field(vrm_key(POWER_NET_A), "nominal_voltage", 0.8)
    vrm = session.vrm(POWER_NET_A)
    assert vrm is not None
    assert vrm.nominal_override is True
    assert vrm.sense_voltage == pytest.approx(0.8)
    assert vrm.sense_override is False
    # the net's own value is untouched: derived edits never propagate upward
    assert session.nets[POWER_NET_A].voltage == pytest.approx(0.7)
    assert session.nets[POWER_NET_A].voltage_override is False
    # ... and the Sink, whose nominal is still auto, stays on the net voltage
    sink = session.sink(POWER_NET_A)
    assert sink is not None and sink.nominal_voltage == pytest.approx(0.7)


def test_derived_override_sticks_through_a_later_net_voltage_edit(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_field(sink_key(POWER_NET_A), "nominal_voltage", 0.85)
    session.set_field(vrm_key(POWER_NET_A), "sense_voltage", 0.65)

    session.set_net_voltage(POWER_NET_A, 1.05)

    vrm = session.vrm(POWER_NET_A)
    sink = session.sink(POWER_NET_A)
    assert vrm is not None and sink is not None
    assert sink.nominal_voltage == pytest.approx(0.85)  # override wins
    assert vrm.sense_voltage == pytest.approx(0.65)  # override wins
    assert vrm.nominal_voltage == pytest.approx(1.05)  # auto follows


def test_reset_auto_clears_overrides_and_re_derives(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_net_voltage(POWER_NET_A, 1.05)
    session.set_field(sink_key(POWER_NET_A), "nominal_voltage", 0.85)
    session.set_field(vrm_key(POWER_NET_A), "output_current", 12.0)

    changed = session.reset_auto(
        [
            (net_key(POWER_NET_A), "voltage"),
            (sink_key(POWER_NET_A), "nominal_voltage"),
        ]
    )

    cfg = session.nets[POWER_NET_A]
    vrm = session.vrm(POWER_NET_A)
    sink = session.sink(POWER_NET_A)
    assert vrm is not None and sink is not None
    assert cfg.voltage == pytest.approx(0.7) and cfg.voltage_override is False
    assert sink.nominal_voltage == pytest.approx(0.7) and sink.nominal_override is False
    assert vrm.output_current == pytest.approx(12.0)  # untouched key keeps its override
    assert (net_key(POWER_NET_A), "voltage_override") in _keys(changed)
    assert (sink_key(POWER_NET_A), "nominal_override") in _keys(changed)

    # the wildcard form resets every derived field of a row
    session.reset_auto([(vrm_key(POWER_NET_A), "")])
    assert vrm.output_current == pytest.approx(1.0)
    assert not (vrm.nominal_override or vrm.sense_override or vrm.current_override)


def test_set_field_rejects_unknown_fields_and_rows(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(ValueError):
        session.set_field(vrm_key(POWER_NET_A), "row_id", 3)
    with pytest.raises(ValueError):
        session.set_field(net_key(POWER_NET_A), "from_input", True)
    with pytest.raises(KeyError):
        session.set_field(vrm_key("NO_SUCH_NET"), "comp", "LGA")
    with pytest.raises(KeyError):
        session.set_net_voltage("NO_SUCH_NET", 1.0)


def test_component_edit_is_pinned_against_re_derivation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_field(vrm_key(POWER_NET_A), "comp", "C1_0")
    session.derive_all()
    vrm = session.vrm(POWER_NET_A)
    assert vrm is not None and vrm.comp == "C1_0"
    session.reset_auto([(vrm_key(POWER_NET_A), "comp")])
    assert vrm.comp == VRM_COMP


# --------------------------------------------------------------------------- #
# autopair (design §C)
# --------------------------------------------------------------------------- #


def test_autopair_picks_the_ground_with_the_most_pins_on_the_vrm_component() -> None:
    session = Session()
    session.load(_two_ground_scan())
    # DGND: 4 LGA pins, VSS: 1 -> DGND, even though VSS sorts later and is
    # only a *candidate* by name (`^(D?GND|VSS|GROUND)`), not by classification.
    assert session.nets["P_VDD_075_MAIN/0"].paired_gnd == "DGND"
    assert set(session._ground_candidates()) == {"DGND", "VSS"}
    assert session.autopair() == []  # idempotent


def test_autopair_tie_breaks_ascii_first() -> None:
    # `VSS` is the classified ground, `GND` is a candidate by name only; both
    # have 2 LGA pins, so the tie must break ASCII-first ("GND" < "VSS").
    body = (
        "\t::Unselected||DropShape RiseTime = 0ps %Coupling = 0\n"
        "\tGroundNets Color = LIME\n"
        "\tPowerNets Color = RED\n"
        "\tGND::Unselected||DropShape Color = GREEN\n"
        "\tVSS -> GroundNets Color = GREEN Voltage = 0\n"
        "\tP_VDD_075_MAIN/0 -> PowerNets Color = RED\n"
    )
    scan = _make_scan(
        body,
        {
            "LGA": {
                "P_VDD_075_MAIN/0": _pins("p", 3),
                "GND": _pins("n", 2),
                "VSS": _pins("v", 2),
            }
        },
    )
    session = Session()
    session.load(scan)
    assert session.nets["P_VDD_075_MAIN/0"].paired_gnd == "GND"
    # a name-matched candidate that gets used is classified ground (spec §2:
    # PowerDC reads the pairing off `GroundNets` membership)
    assert session.nets["GND"].net_class == "ground"
    assert session.ground_nets() == ["GND", "VSS"]


def test_autopair_keeps_a_preloaded_ground_unless_forced() -> None:
    from powerdc_setup_tool.core.model import ExistingBlock

    block = ExistingBlock(
        kind="vrm",
        name="VRM_LGA_P_VDD_075_MAIN/0_VSS",
        net="P_VDD_075_MAIN/0",
        gnet="VSS",
        comp="LGA",
        values={"NominalVoltage": "0.75", "SenseVoltage": "0.75", "OutputCurrent": "1"},
    )
    session = Session()
    session.load(_two_ground_scan(existing_vrms=(block,)))

    assert session.nets["P_VDD_075_MAIN/0"].paired_gnd == "VSS"  # §G.2 preload wins
    assert session.autopair() == []
    assert session.nets["P_VDD_075_MAIN/0"].paired_gnd == "VSS"

    changed = session.autopair(force=True)
    assert session.nets["P_VDD_075_MAIN/0"].paired_gnd == "DGND"
    assert (net_key("P_VDD_075_MAIN/0"), "paired_gnd") in _keys(changed)
    assert {row.gnet for row in (*session.vrm_rows, *session.sink_rows)} == {"DGND"}


# --------------------------------------------------------------------------- #
# classification / selection
# --------------------------------------------------------------------------- #


def test_set_class_creates_and_drops_rows(tmp_path: Path) -> None:
    session = _session(tmp_path)
    net = UNCLASSIFIED_NETS[0]
    version = session.structure_version

    changed = session.set_class(net, "power")
    assert session.nets[net].net_class == "power"
    assert session.nets[net].paired_gnd == GROUND_NET  # newly classified nets pair
    assert session.vrm(net) is not None and session.sink(net) is not None
    assert session.structure_version > version
    assert (vrm_key(net), "comp") in _keys(changed)

    session.set_class(net, "none")
    assert session.vrm(net) is None and session.sink(net) is None
    assert session.nets[net].paired_gnd == ""
    assert [row.net for row in session.vrm_rows] == [POWER_NET_A, POWER_NET_B]

    with pytest.raises(ValueError):
        session.set_class(net, "POWER_SUPPLY")


def test_set_selected_mirrors_onto_rows(tmp_path: Path) -> None:
    session = _session(tmp_path)
    changed = session.set_selected(POWER_NET_A, False)
    assert session.nets[POWER_NET_A].selected is False
    assert session.vrm(POWER_NET_A).enabled is False  # type: ignore[union-attr]
    assert session.sink(POWER_NET_A).enabled is False  # type: ignore[union-attr]
    assert (vrm_key(POWER_NET_A), "enabled") in _keys(changed)
    assert session.counts()["vrm"] == 1 and session.counts()["sink"] == 1


def test_per_row_ground_edit_is_pinned_and_never_propagates_upward() -> None:
    session = Session()
    session.load(_two_ground_scan())
    net = "P_VDD_075_MAIN/0"
    changed = session.set_field(vrm_key(net), "gnet", "VSS")

    assert session.vrm(net).gnet == "VSS"  # type: ignore[union-attr]
    assert session.sink(net).gnet == "DGND"  # type: ignore[union-attr]
    assert session.nets[net].paired_gnd == "DGND"  # derived edits stay put
    assert session.nets["VSS"].net_class == "ground"  # spec §2 membership
    assert (net_key("VSS"), "net_class") in _keys(changed)

    session.derive_all()
    assert session.vrm(net).gnet == "VSS"  # pinned against re-derivation
    session.reset_auto([(vrm_key(net), "gnet")])
    assert session.vrm(net).gnet == "DGND"  # type: ignore[union-attr]


def test_set_paired_ground_moves_every_row_of_the_net() -> None:
    session = Session()
    session.load(_two_ground_scan())
    net = "P_VDD_075_MAIN/0"
    session.set_paired_ground(net, "VSS")
    assert session.nets[net].paired_gnd == "VSS"
    assert {row.gnet for row in (*session.vrm_rows, *session.sink_rows)} == {"VSS"}
    assert session.nets["VSS"].net_class == "ground"
    assert session.ground_nets() == ["VSS", "DGND"]  # netlist order


def test_counts_matches_the_status_bar_fields(tmp_path: Path) -> None:
    session = _session(tmp_path)
    counts = session.counts()
    assert counts["nets"] == 9  # 2 unclassified + DGND + 2 power + 4 sense
    assert counts["power"] == 2 and counts["ground"] == 1
    assert counts["unclassified"] == 6
    assert counts["vrm"] == 2 and counts["sink"] == 2


# --------------------------------------------------------------------------- #
# multiple rows per net + derived names (design §G.3)
# --------------------------------------------------------------------------- #


def test_extra_rows_get_dedup_suffixes_and_survive_derive_all(tmp_path: Path) -> None:
    session = _session(tmp_path)
    key = session.add_vrm_row(POWER_NET_A)
    assert key == vrm_key(POWER_NET_A, 1)
    session.derive_all()
    assert len(session.vrm_rows) == 3

    names = session.display_names()
    base = f"VRM_{VRM_COMP}_{POWER_NET_A}_{GROUND_NET}"
    assert names[vrm_key(POWER_NET_A, 0)] == base
    assert names[vrm_key(POWER_NET_A, 1)] == base + "_2"
    # the *emitted* name carries no suffix -- which is why validate() blocks
    assert Session.block_name(session.vrm(POWER_NET_A, 1)) == base  # type: ignore[arg-type]

    assert session.remove_row(key) is True
    assert len(session.vrm_rows) == 2
    assert session.remove_row(vrm_key(POWER_NET_A)) is False  # never the last row


# --------------------------------------------------------------------------- #
# chunk 7 regressions: structural bookkeeping
# --------------------------------------------------------------------------- #


def test_ensure_ground_bumps_structure_version_when_it_adds_a_net_row(
    tmp_path: Path,
) -> None:
    """A paired ground the `.NetList` never mentioned materializes a brand-new
    Net Manager row -- a structural change models must reset for, not patch."""
    session = _session(tmp_path)
    before_rows = len(session.nets)
    version = session.structure_version

    session.set_paired_ground(POWER_NET_A, "BRAND_NEW_GND")

    assert len(session.nets) == before_rows + 1
    assert session.structure_version > version
    assert session.nets["BRAND_NEW_GND"].net_class == "ground"
    assert session.nets["BRAND_NEW_GND"].voltage == 0.0

    # An *existing* net promoted to ground adds no row -> no further bump.
    version = session.structure_version
    session.set_paired_ground(POWER_NET_B, "BRAND_NEW_GND")
    assert session.structure_version == version


def test_ensure_ground_bumps_through_a_per_row_ground_edit(tmp_path: Path) -> None:
    """`set_field(..., "gnet", ...)` reaches `_ensure_ground` too."""
    session = _session(tmp_path)
    version = session.structure_version
    session.set_field(vrm_key(POWER_NET_A), "gnet", "SIDEBAND_GND")
    assert "SIDEBAND_GND" in session.nets
    assert session.structure_version > version


def test_added_rows_land_in_netlist_order_immediately(tmp_path: Path) -> None:
    """`add_vrm_row`/`add_sink_row` place the row where `_sync_rows` would --
    the design §D block order -- without waiting for a later `derive_all`."""
    session = _session(tmp_path)
    assert [r.net for r in session.vrm_rows] == [POWER_NET_A, POWER_NET_B]

    key = session.add_vrm_row(POWER_NET_A)
    assert key == vrm_key(POWER_NET_A, 1)
    assert [(r.net, r.row_id) for r in session.vrm_rows] == [
        (POWER_NET_A, 0),
        (POWER_NET_A, 1),
        (POWER_NET_B, 0),
    ]
    sink = session.add_sink_row(POWER_NET_A)
    assert sink == sink_key(POWER_NET_A, 1)
    assert [(r.net, r.row_id) for r in session.sink_rows] == [
        (POWER_NET_A, 0),
        (POWER_NET_A, 1),
        (POWER_NET_B, 0),
    ]

    # ... and `derive_all` is a no-op on the ordering (it was already correct).
    order = [(r.net, r.row_id) for r in session.vrm_rows]
    session.derive_all()
    assert [(r.net, r.row_id) for r in session.vrm_rows] == order


def test_added_rows_are_exported_in_netlist_order(tmp_path: Path) -> None:
    """The immediate placement is what `build_plan` emits (design §D step 5)."""
    session = _session(tmp_path)
    session.add_vrm_row(POWER_NET_A)
    plan = session.build_plan()
    assert [cfg.net for cfg, _pos, _neg in plan.vrms] == [
        POWER_NET_A,
        POWER_NET_A,
        POWER_NET_B,
    ]


def test_add_row_bumps_structure_version(tmp_path: Path) -> None:
    session = _session(tmp_path)
    version = session.structure_version
    session.add_sink_row(POWER_NET_B)
    assert session.structure_version > version


# --------------------------------------------------------------------------- #
# validate (design §C export flow, §G.3/§G.4)
# --------------------------------------------------------------------------- #


def test_validate_is_clean_for_the_canonical_fixture(tmp_path: Path) -> None:
    assert _session(tmp_path).validate() == []


def test_validate_flags_a_zero_pin_net_as_blocking(tmp_path: Path) -> None:
    # §G.4: sense nets are netlist-only and carry no `.Connect` pins.
    session = _session(tmp_path)
    session.set_class(SENSE_NETS[0], "power")
    issues = session.validate()

    blocking = [i for i in issues if i.blocking]
    assert blocking, "a 0-pin net must block the export"
    assert any("no positive pins" in i.message for i in blocking)
    assert all(SENSE_NETS[0] in i.nets for i in blocking)
    warnings = [i for i in issues if not i.blocking]
    assert any("absent from the .Connect pin maps" in i.message for i in warnings)
    assert any("remote-sense" in i.message for i in warnings)

    # design §G.4: unchecking the offending row clears it
    session.set_selected(SENSE_NETS[0], False)
    assert session.validate() == []


def test_validate_flags_duplicate_block_names_as_blocking(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.add_vrm_row(POWER_NET_A)
    issues = [i for i in session.validate() if i.blocking]
    assert len(issues) == 1
    assert "duplicate block name" in issues[0].message
    assert f"VRM_{VRM_COMP}_{POWER_NET_A}_{GROUND_NET} x2" in issues[0].message
    assert issues[0].nets == (POWER_NET_A,)

    # a different component makes the names unique again
    session.set_field(vrm_key(POWER_NET_A, 1), "comp", "C1_0")
    assert [i for i in session.validate() if i.blocking] == []


def test_validate_flags_a_ground_with_no_pins_on_the_chosen_component() -> None:
    # §G.4: "an empty Negative Pin block is silently wrong in PowerDC".
    session = Session()
    session.load(_two_ground_scan())
    session.set_paired_ground("P_VDD_075_MAIN/0", "VSS")  # VSS has no SITE0 pins
    blocking = [i for i in session.validate() if i.blocking]
    assert any("no ground pins" in i.message for i in blocking)
    assert any("VSS @SITE0" in i.message for i in blocking)


def test_validate_flags_missing_ground_and_non_positive_voltage(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_class(GROUND_NET, "none")  # the pairing target is gone
    session.set_net_voltage(POWER_NET_B, 0.0)
    blocking = [i for i in session.validate() if i.blocking]
    assert any("no paired ground" in i.message for i in blocking)
    assert any("voltage must be > 0" in i.message for i in blocking)
    assert any(POWER_NET_B in i.nets for i in blocking if "voltage" in i.message)


def test_validate_ignores_disabled_rows(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_class(SENSE_NETS[0], "power")
    assert any(i.blocking for i in session.validate())
    session.set_field(vrm_key(SENSE_NETS[0]), "enabled", False)
    session.set_field(sink_key(SENSE_NETS[0]), "enabled", False)
    assert [i for i in session.validate() if i.blocking] == []


def test_validate_without_a_scan_is_blocking() -> None:
    issues = Session().validate()
    assert len(issues) == 1 and issues[0].blocking


# --------------------------------------------------------------------------- #
# JSON round trip (design §C Save/Load Config)
# --------------------------------------------------------------------------- #


def _config_payload(session: Session) -> dict[str, object]:
    import json

    payload = json.loads(session.to_json())
    payload.pop("source", None)  # provenance only
    return payload


def test_json_round_trip_preserves_values_and_overrides(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_net_voltage(POWER_NET_A, 0.95)
    session.set_field(sink_key(POWER_NET_A), "current", 42.5)
    session.set_field(vrm_key(POWER_NET_B), "comp", "C1_1")
    session.set_selected(UNCLASSIFIED_NETS[1], False)

    restored = Session.from_json(session.to_json())

    assert _config_payload(restored) == _config_payload(session)
    assert restored.nets[POWER_NET_A].voltage == pytest.approx(0.95)
    assert restored.nets[POWER_NET_A].voltage_override is True
    assert restored.sink(POWER_NET_A).current_override is True  # type: ignore[union-attr]
    assert restored.vrm(POWER_NET_B).comp == "C1_1"  # type: ignore[union-attr]
    assert (vrm_key(POWER_NET_B), "comp") in restored._pinned  # pin survives
    assert restored.nets[UNCLASSIFIED_NETS[1]].selected is False
    assert [row.net for row in restored.vrm_rows] == [POWER_NET_A, POWER_NET_B]

    # re-applying onto the scanned session leaves the derived state alone
    session.apply_json(session.to_json())
    assert session.vrm(POWER_NET_B).comp == "C1_1"  # type: ignore[union-attr]
    assert session.build_plan({}).netlist_body is None


def test_from_json_tolerates_unknown_keys_and_versions() -> None:
    data = """
    {
      "version": 99,
      "future_section": {"anything": [1, 2, 3]},
      "nets": [
        {"net": "VDD/0", "net_class": "power", "paired_gnd": "DGND",
         "voltage": 0.8, "voltage_override": true, "unknown_field": "ignored"},
        {"net": "DGND", "net_class": "ground"},
        {"no_net_key": true}
      ],
      "vrms": [{"net": "VDD/0", "row_id": 0, "comp": "LGA", "gnet": "DGND",
                "nominal_voltage": 0.8, "pinned": ["comp", "bogus"]}],
      "sinks": []
    }
    """
    session = Session.from_json(data)
    assert list(session.nets) == ["VDD/0", "DGND"]
    assert session.nets["VDD/0"].voltage == pytest.approx(0.8)
    assert session.nets["VDD/0"].voltage_override is True
    assert len(session.vrm_rows) == 1 and session.vrm_rows[0].comp == "LGA"
    assert session._pinned == {(vrm_key("VDD/0"), "comp")}

    with pytest.raises(ValueError):
        Session.from_json("[]")


# --------------------------------------------------------------------------- #
# build_plan (design §D)
# --------------------------------------------------------------------------- #


def test_build_plan_netlist_body_is_none_when_nothing_changed(tmp_path: Path) -> None:
    # design §G.1 / spec §8: an already-classified input must come out
    # byte-identical, which `writer` can only guarantee via `netlist_body is None`.
    scan = _scan(tmp_path)
    session = Session()
    session.load(scan)
    assert render_netlist(list(scan.nets), dict(session.nets)) == scan.netlist_body
    assert session.build_plan({}).netlist_body is None
    # a pure value edit is not a classification change either
    session.set_net_voltage(POWER_NET_A, 0.65)
    assert session.build_plan({}).netlist_body is None


def test_build_plan_emits_a_netlist_body_once_classification_changes(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    session = Session()
    session.load(scan)
    session.set_class(UNCLASSIFIED_NETS[0], "power")

    body = session.build_plan({}).netlist_body
    assert body is not None and body != scan.netlist_body
    # spec §A7: a classified net joins its group and drops `::sel||view`
    assert f"\t{UNCLASSIFIED_NETS[0]} Color = " in body
    assert f"{UNCLASSIFIED_NETS[0]}::Unselected" not in body

    # ... and the toggle switches it back off (design §C "Rewrite NetList")
    assert session.build_plan({"rewrite_netlist": False}).netlist_body is None


def test_build_plan_other_circuits_none_vs_tuple(tmp_path: Path) -> None:
    session = _session(tmp_path)
    # a tuple (even an empty one) means "regenerate the run"; None means
    # "leave the input's own run alone" (writer.WritePlan docstring)
    assert session.build_plan({}).other_circuits == ("C1_0", "C1_1")
    assert session.build_plan({"other_circuits": True}).other_circuits == ("C1_0", "C1_1")
    assert session.build_plan({"other_circuits": False}).other_circuits is None


def test_build_plan_filters_other_circuits_by_the_shared_pattern() -> None:
    scan = _two_ground_scan(other_circuit_names=("C9_0", "LGA", "C9_2", "SITE0"))
    session = Session()
    session.load(scan)
    assert session.build_plan({}).other_circuits == ("C9_0",)


def test_build_plan_order_matches_the_netlist_order(tmp_path: Path) -> None:
    session = _session(tmp_path)
    plan = session.build_plan({})
    order = [cfg.net for cfg, _pos, _neg in plan.vrms]
    assert order == [POWER_NET_A, POWER_NET_B] == session.power_nets()
    assert [cfg.net for cfg, _pos, _neg in plan.sinks] == order
    # netlist order, not ASCII order of the derived block names
    assert order == [net for net in session.power_nets()]


def test_build_plan_carries_pins_from_the_pin_maps(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    session = Session()
    session.load(scan)
    plan = session.build_plan({})
    cfg, pos, neg = plan.vrms[0]
    assert pos == scan.pin_maps.pins(VRM_COMP, POWER_NET_A)
    assert neg == scan.pin_maps.pins(VRM_COMP, GROUND_NET)
    sink_cfg, sink_pos, sink_neg = plan.sinks[0]
    assert sink_pos == scan.pin_maps.pins("SITE0", POWER_NET_A)
    assert sink_neg == scan.pin_maps.pins("SITE0", GROUND_NET)
    assert cfg.comp == VRM_COMP and sink_cfg.model == 2
    assert plan.patch_workflow_key is True
    assert session.build_plan({"patch_workflow_key": False}).patch_workflow_key is False


def test_build_plan_excludes_disabled_rows_and_snapshots_configs(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_selected(POWER_NET_B, False)
    plan = session.build_plan({})
    assert [cfg.net for cfg, _p, _n in plan.vrms] == [POWER_NET_A]
    assert [cfg.net for cfg, _p, _n in plan.sinks] == [POWER_NET_A]

    # the plan is a frozen copy: an export on a worker thread cannot be
    # mutated mid-write by the UI (design §D)
    session.set_net_voltage(POWER_NET_A, 3.3)
    assert plan.vrms[0][0].nominal_voltage == pytest.approx(0.7)


def test_build_plan_requires_a_loaded_scan() -> None:
    with pytest.raises(RuntimeError):
        Session().build_plan({})


# --------------------------------------------------------------------------- #
# end-to-end integration (design §E / §F chunk 7 preview)
# --------------------------------------------------------------------------- #


def test_end_to_end_si_fixture_matches_expected_dc(tmp_path: Path) -> None:
    """si fixture -> scan_spd -> Session -> build_plan -> write_spd == golden."""
    scan = _scan(tmp_path, "si", name="in.spd")
    session = Session()
    session.load(scan)
    session.autopair()
    assert session.validate() == []

    out = tmp_path / "in_DC.spd"
    write_spd(scan, session.build_plan({}), out)
    assert out.read_bytes() == expected_dc().encode("utf-8")


def test_end_to_end_dc_fixture_is_idempotent(tmp_path: Path) -> None:
    # §G.2: existing blocks are regenerated wholesale, never appended to.
    scan = _scan(tmp_path, "dc", name="in.spd")
    session = Session()
    session.load(scan)
    out = tmp_path / "in_DC.spd"
    write_spd(scan, session.build_plan({}), out)
    assert out.read_bytes() == expected_dc().encode("utf-8")


def test_end_to_end_export_options_reach_the_output(tmp_path: Path) -> None:
    scan = _scan(tmp_path, "si", name="in.spd")
    session = Session()
    session.load(scan)
    out = tmp_path / "plain.spd"
    write_spd(
        scan,
        session.build_plan({"other_circuits": False, "patch_workflow_key": False}),
        out,
    )
    assert out.read_bytes() == expected_dc(
        other_circuits=False, patch_workflow_key=False
    ).encode("utf-8")


def test_end_to_end_with_an_edited_voltage(tmp_path: Path) -> None:
    scan = _scan(tmp_path, "si", name="in.spd")
    session = Session()
    session.load(scan)
    session.set_net_voltage(POWER_NET_A, 0.75)
    session.set_field(sink_key(POWER_NET_A), "current", 2.5)

    out = tmp_path / "edited.spd"
    write_spd(scan, session.build_plan({}), out)
    text = out.read_text(encoding="utf-8")
    assert (
        f'.VRM NominalVoltage = 0.75 SenseVoltage = 0.75 OutputCurrent = 1 '
        f'Name = "VRM_{VRM_COMP}_{POWER_NET_A}_{GROUND_NET}"' in text
    )
    assert (
        f".Sink NominalVoltage = 0.75 Current = 2.5 Model = 2 PFMode = 2 "
        f'PinEqualCurrent = 1 Name = "SINK_SITE0_{POWER_NET_A}_{GROUND_NET}"' in text
    )
    # the untouched net keeps its guessed voltage
    assert f".VRM NominalVoltage = 1.2 SenseVoltage = 1.2 OutputCurrent = 1 " in text
    assert b"\r" not in out.read_bytes()
