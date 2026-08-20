"""Focused v0.1.6 regressions for production SPD grammar and export safety."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import fixtures
from powerdc_setup_tool.core import writer as writer_mod
from powerdc_setup_tool.core.model import CircuitInfo, PinMapIndex, PowerNetConfig
from powerdc_setup_tool.core.netlist import parse_netlist, render_netlist
from powerdc_setup_tool.core.pdc_gen import OTHER_CIRCUIT_RE, render_other_circuits
from powerdc_setup_tool.core.session import Session
from powerdc_setup_tool.core.spd_scan import SpdFormatError, scan_spd
from powerdc_setup_tool.core.writer import WritePlan, write_spd


def _empty_plan() -> WritePlan:
    return WritePlan([], [], None, None, False)


def _build(tmp_path: Path, *, style: str = "si", name: str = "in.spd") -> Path:
    path = tmp_path / name
    fixtures.build_mini_spd(path, style=style)  # type: ignore[arg-type]
    return path


def test_destination_group_suffix_keeps_inherited_rows_and_byte_identity() -> None:
    body = (
        "\tGroundNets Color = LIME\n"
        "\tPowerNets Color = RED\n"
        "\tVDDWL/15 -> PowerNets::Unselected||DropShape Color = RED\n"
        "\tVDDWL/14::Unselected||DropShape Color = RED\n"
        "\tVDDWL/13::Unselected||DropShape Color = RED\n"
    )
    entries = parse_netlist(body)
    assert [entry.group for entry in entries[2:]] == ["PowerNets"] * 3
    assert entries[2].group_sel_state == "Unselected"
    assert entries[3].sel_state == "Unselected"
    configs = {
        entry.name: PowerNetConfig(entry.name, "power", selected=False)
        for entry in entries[2:]
    }
    assert render_netlist(entries, configs) == body


def test_promoting_inherited_member_preserves_destination_metadata_style() -> None:
    body = (
        "\tPowerNets Color = RED\n"
        "\tVDD/0 -> PowerNets::Unselected||DropShape Color = RED\n"
        "\tVDD/1::Unselected||DropShape Color = RED\n"
        "\tVDD/2::Unselected||DropShape Color = RED\n"
    )
    entries = parse_netlist(body)
    configs = {
        "VDD/0": PowerNetConfig("VDD/0", "none"),
        "VDD/1": PowerNetConfig("VDD/1", "power", selected=False),
        "VDD/2": PowerNetConfig("VDD/2", "power", selected=False),
    }
    rendered = render_netlist(entries, configs)
    reparsed = parse_netlist(rendered)
    members = [entry for entry in reparsed if entry.name.startswith("VDD/")]
    assert [entry.group for entry in members] == ["PowerNets", "PowerNets", None]
    assert members[0].group_explicit is True
    assert members[0].group_sel_state == "Unselected"
    assert members[0].sel_state == "Unselected"


def test_other_circuit_forms_are_strict_and_rendered_verbatim() -> None:
    accepted = ("C1", "C1/0", "C1/1", "C1_0", "C1_1")
    rejected = ("C1/2", "C1_2", "C1A", "C1_0_extra", "CC1_0")
    assert all(OTHER_CIRCUIT_RE.fullmatch(name) for name in accepted)
    assert all(not OTHER_CIRCUIT_RE.fullmatch(name) for name in rejected)
    rendered = render_other_circuits(accepted)
    assert all(f'Device = {name} Name = "{name}"' in rendered for name in accepted)


def test_other_circuit_scan_and_session_plan_keep_source_refdes(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes().replace(b"C1_0", b"C1/0").replace(b"C1_1", b"C1")
    path.write_bytes(data)
    scan = scan_spd(path)
    assert scan.other_circuit_names == ("C1/0", "C1")
    session = Session()
    session.load(scan)
    assert session.build_plan({}).other_circuits == ("C1/0", "C1")


def test_session_maps_classified_source_unselected_to_disabled_row(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    old = (
        b"\tADC_VDD_070_VDDA/0 -> PowerNets Color = RED\n"
        b"\tADC_VDD_120_VDDB/1 Color = OLIVE\n"
    )
    new = (
        b"\tADC_VDD_070_VDDA/0 -> PowerNets::Unselected||DropShape Color = RED\n"
        b"\tADC_VDD_120_VDDB/1::Unselected||DropShape Color = OLIVE\n"
    )
    assert old in data
    path.write_bytes(data.replace(old, new, 1))
    scan = scan_spd(path)
    session = Session()
    session.load(scan)
    assert session.nets[fixtures.POWER_NET_A].selected is True
    assert session.nets[fixtures.POWER_NET_B].selected is False
    assert sum(row.enabled for row in session.vrm_rows) == 1
    assert session.build_plan({}).netlist_body is None


def test_selection_only_edits_round_trip_and_preserve_production_group_metadata(
    tmp_path: Path,
) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    old = (
        b"\tADC_VDD_070_VDDA/0 -> PowerNets Color = RED\n"
        b"\tADC_VDD_120_VDDB/1 Color = OLIVE\n"
    )
    new = (
        b"\tADC_VDD_070_VDDA/0 -> PowerNets::Unselected||DropShape Color = RED\n"
        b"\tADC_VDD_120_VDDB/1::Unselected||DropShape Color = OLIVE\n"
    )
    path.write_bytes(data.replace(old, new, 1))
    scan = scan_spd(path)
    session = Session()
    session.load(scan)

    # The first destination marker has no source suffix and is enabled; the
    # inherited member carries source Unselected and is disabled.
    assert session.nets[fixtures.POWER_NET_A].selected is True
    assert session.nets[fixtures.POWER_NET_B].selected is False

    session.set_selected(fixtures.POWER_NET_A, False)
    rendered = session.build_plan({}).netlist_body
    assert rendered is not None
    reparsed = {entry.name: entry for entry in parse_netlist(rendered)}
    assert reparsed[fixtures.POWER_NET_A].sel_state == "Unselected"
    assert reparsed[fixtures.POWER_NET_A].group == "PowerNets"
    assert reparsed[fixtures.POWER_NET_A].group_sel_state == "Unselected"
    reloaded = Session()
    reloaded.load(replace(scan, netlist_body=rendered, nets=tuple(parse_netlist(rendered))))
    assert reloaded.nets[fixtures.POWER_NET_A].selected is False
    assert sum(row.enabled for row in reloaded.vrm_rows) == 0

    session.set_selected(fixtures.POWER_NET_A, True)
    rendered = session.build_plan({}).netlist_body
    # Restoring the source selection reproduces the original bytes, so the
    # writer can keep the body on its byte-copy fast path.
    assert rendered is None

    # Selection-only changes on inherited members stay in place; changing its
    # electrical class retains the disabled source state and upgrades the
    # destination marker so the row remains in GroundNets after a re-scan.
    session.set_selected(fixtures.POWER_NET_B, True)
    rendered = session.build_plan({}).netlist_body
    assert rendered is not None
    reparsed = {entry.name: entry for entry in parse_netlist(rendered)}
    assert reparsed[fixtures.POWER_NET_B].sel_state is None
    reloaded = Session()
    reloaded.load(replace(scan, netlist_body=rendered, nets=tuple(parse_netlist(rendered))))
    assert reloaded.nets[fixtures.POWER_NET_B].selected is True
    assert sum(row.enabled for row in reloaded.vrm_rows) == 2

    session.set_selected(fixtures.POWER_NET_B, False)
    session.set_class(fixtures.POWER_NET_B, "ground")
    rendered = session.build_plan({}).netlist_body
    assert rendered is not None
    reparsed = {entry.name: entry for entry in parse_netlist(rendered)}
    assert reparsed[fixtures.POWER_NET_B].group == "GroundNets"
    assert reparsed[fixtures.POWER_NET_B].sel_state == "Unselected"
    reloaded = Session()
    reloaded.load(replace(scan, netlist_body=rendered, nets=tuple(parse_netlist(rendered))))
    assert reloaded.nets[fixtures.POWER_NET_B].net_class == "ground"
    assert reloaded.nets[fixtures.POWER_NET_B].selected is False
    assert all(
        not row.enabled
        for row in reloaded.vrm_rows + reloaded.sink_rows
        if row.net == fixtures.POWER_NET_B
    )


def test_netlist_unknown_destination_group_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"-> PowerNets", b"-> SignalNets", 1))
    with pytest.raises(SpdFormatError, match="destination group"):
        scan_spd(path)


def test_netlist_duplicate_entry_name_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    line = b"\tSIG_CLK_IN::Unselected||DropShape Color = GREEN\n"
    path.write_bytes(path.read_bytes().replace(line, line + line, 1))
    with pytest.raises(SpdFormatError, match="duplicate .NetList entry"):
        scan_spd(path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda data: data.replace(b"OutputCurrent = 1", b"OutputCurrent = 1 Banana = 2", 1),
            "unknown .VRM header key",
        ),
        (
            lambda data: data.replace(b"OutputCurrent = 1", b"OutputCurrent = 1 OutputCurrent = 2", 1),
            "duplicate .VRM attribute",
        ),
        (
            lambda data: data.replace(b"OutputCurrent = 1 ", b"", 1),
            "missing required",
        ),
        (
            lambda data: data.replace(b"OutputCurrent = 1", b"OutputCurrent = banana", 1),
            "invalid numeric",
        ),
        (
            lambda data: data.replace(
                b'Name = "VRM_LGA_ADC_VDD_070_VDDA/0_DGND"',
                b'Name = "VRM_LGA_ADC_VDD_070_VDDA/0_DGND',
                1,
            ),
            "unbalanced quotes",
        ),
    ],
)
def test_existing_block_header_validation_fails_closed(tmp_path: Path, mutate, message: str) -> None:
    path = _build(tmp_path, style="dc")
    path.write_bytes(mutate(path.read_bytes()))
    with pytest.raises(SpdFormatError, match=message):
        scan_spd(path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda data: data.replace(
                b'.Pin Name = "Positive Pin"', b'.Pin Name = "Unknown Pin"', 1
            ),
            "unknown .VRM pin section",
        ),
        (
            lambda data: data.replace(
                b'.Pin Name = "Positive Pin"', b".EndMap\n.Pin Name = \"Positive Pin\"", 1
            ),
            "orphan .EndMap",
        ),
        (
            lambda data: data.replace(
                b'.Pin Name = "Positive Pin"', b".EndPin\n.Pin Name = \"Positive Pin\"", 1
            ),
            "orphan .EndPin",
        ),
        (
            lambda data: data.replace(
                b".Map CircuitName = LGA CircuitPinName = 1\n",
                b".Node Name = Node4001!!1::ADC_VDD_070_VDDA/0\n"
                b".Map CircuitName = LGA CircuitPinName = 1\n",
                1,
            ),
            "orphan .Node",
        ),
        (
            lambda data: data.replace(
                b".Map CircuitName = LGA CircuitPinName = 1\n"
                b".Node Name = Node1001!!1::ADC_VDD_070_VDDA/0\n",
                b".Map CircuitName = LGA CircuitPinName = 1\n"
                b".EndMap\n",
                1,
            ),
            "expected .Node",
        ),
        (
            lambda data: data.replace(
                b".SinkCurrentSource\n.EndSinkCurrentSource\n",
                b".EndSinkCurrentSource\n",
                1,
            ),
            "orphan .EndSinkCurrentSource",
        ),
        (
            lambda data: data.replace(
                b".SinkCurrentSource\n.EndSinkCurrentSource\n",
                b".SinkCurrentSource\n.EndSink\n",
                1,
            ),
            "unexpected content inside .SinkCurrentSource",
        ),
    ],
)
def test_existing_block_structure_validation_fails_closed(
    tmp_path: Path, mutate, message: str
) -> None:
    path = _build(tmp_path, style="dc")
    path.write_bytes(mutate(path.read_bytes()))
    with pytest.raises(SpdFormatError, match=message):
        scan_spd(path)


def test_duplicate_connect_refdes_and_unterminated_headers_fail_closed(tmp_path: Path) -> None:
    duplicate = _build(tmp_path, name="duplicate_connect.spd")
    data = duplicate.read_bytes()
    marker = b".Connect LGA"
    start = data.index(marker)
    end = data.index(b".EndC\n", start) + len(b".EndC\n")
    duplicate.write_bytes(data[:end] + data[start:end] + data[end:])
    with pytest.raises(SpdFormatError, match="duplicate .Connect RefDes"):
        scan_spd(duplicate)

    no_name = _build(tmp_path, name="connect_no_name.spd")
    no_name.write_bytes(no_name.read_bytes().replace(b".Connect ", b".Connect\n", 1))
    with pytest.raises(SpdFormatError, match="no RefDes"):
        scan_spd(no_name)

    unterminated = _build(tmp_path, name="connect_unterminated.spd")
    unterminated.write_bytes(
        unterminated.read_bytes().replace(b".EndC\n", b"", 1)
    )
    with pytest.raises(SpdFormatError, match="not terminated"):
        scan_spd(unterminated)


def test_duplicate_connect_outer_pin_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    needle = b"1 $Package.Node4001!!1::ADC_VDD_070_VDDA/0\n"
    assert needle in data
    path.write_bytes(data.replace(needle, needle + needle, 1))
    with pytest.raises(SpdFormatError, match="duplicate pin"):
        scan_spd(path)


def test_staging_alias_cannot_destroy_source(tmp_path: Path) -> None:
    source = _build(tmp_path, name="design.spd.part")
    original = source.read_bytes()
    scan = scan_spd(source)
    output = tmp_path / "design.spd"
    with pytest.raises(ValueError, match="staging"):
        write_spd(scan, _empty_plan(), output)
    assert source.read_bytes() == original
    assert not output.exists()
    # The alias path is the source itself; it must remain intact.
    assert output.with_name(output.name + ".part").read_bytes() == original


def test_race_created_staging_hardlink_is_not_truncated_or_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclusive staging creation must win a check/open race safely.

    The preflight alias check runs before the free-space check.  Simulate a
    concurrent actor creating ``<output>.part`` as a hardlink to the source in
    that interval; opening with ``xb`` must fail without truncating or cleaning
    up the caller-owned link.
    """
    source = _build(tmp_path)
    original = source.read_bytes()
    scan = scan_spd(source)
    output = tmp_path / "out.spd"
    part = output.with_name(output.name + ".part")

    def create_source_hardlink(_output: Path, _file_size: int) -> None:
        os.link(source, part)

    monkeypatch.setattr(writer_mod, "_check_free_space", create_source_hardlink)
    try:
        with pytest.raises(FileExistsError):
            write_spd(scan, _empty_plan(), output)
        assert source.read_bytes() == original
        assert source.stat().st_size == len(original)
        assert not output.exists()
        assert part.read_bytes() == original
    finally:
        part.unlink(missing_ok=True)


@pytest.mark.parametrize("change", ["prepend", "truncate", "touch"])
def test_stale_scan_fails_before_creating_output(tmp_path: Path, change: str) -> None:
    source = _build(tmp_path)
    scan = scan_spd(source)
    original = source.read_bytes()
    if change == "prepend":
        source.write_bytes(b"X" + original)
    elif change == "truncate":
        source.write_bytes(original[:-1])
    else:
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    output = tmp_path / "out.spd"
    with pytest.raises(ValueError, match="changed"):
        write_spd(scan, _empty_plan(), output)
    assert not output.exists()
    assert not output.with_name(output.name + ".part").exists()


def test_mid_export_source_change_does_not_publish_part(tmp_path: Path, monkeypatch) -> None:
    source = _build(tmp_path)
    scan = scan_spd(source)
    output = tmp_path / "out.spd"
    original_splice = writer_mod._splice

    def splice_then_touch(*args, **kwargs):
        original_splice(*args, **kwargs)
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    monkeypatch.setattr(writer_mod, "_splice", splice_then_touch)
    with pytest.raises(ValueError, match="changed"):
        write_spd(scan, _empty_plan(), output)
    assert not output.exists()
    assert not output.with_name(output.name + ".part").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.replace(b".PowerDC PlotResolution", b".PowerDC PlotResolution\n.PowerDC", 1),
        lambda data: data.replace(b"* PdcElem description lines\n", b"* PdcElem description lines\n* PdcElem duplicate\n", 1),
        lambda data: data.replace(b".EndSpiceNetlist\n", b".EndSpiceNetlist\n.EndSpiceNetlist\n", 1),
        lambda data: data.replace(b".NetList\n", b".NetList\n.NetList\n", 1),
    ],
)
def test_mandatory_structure_duplicates_fail_closed(tmp_path: Path, mutate) -> None:
    path = _build(tmp_path)
    path.write_bytes(mutate(path.read_bytes()))
    with pytest.raises(SpdFormatError):
        scan_spd(path)


def test_mandatory_sections_in_non_monotonic_order_fail_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    pdc_start = data.index(b".PowerDC ")
    pdc_end = data.index(b".EndPowerDC\n", pdc_start) + len(b".EndPowerDC\n")
    net_start = data.index(b".NetList\n")
    net_end = data.index(b".EndNetList\n", net_start) + len(b".EndNetList\n")
    netlist = data[net_start:net_end]
    pdc = data[pdc_start:pdc_end]
    between = data[pdc_end:net_start]
    path.write_bytes(data[:pdc_start] + netlist + between + pdc + data[net_end:])
    with pytest.raises(SpdFormatError):
        scan_spd(path)


def test_mandatory_powerdc_marker_inside_netlist_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    needle = b"\tSIG_CLK_IN::Unselected||DropShape Color = GREEN\n"
    assert needle in data
    path.write_bytes(data.replace(needle, b".PowerDC\n" + needle, 1))
    with pytest.raises(SpdFormatError, match="inside .NetList"):
        scan_spd(path)


def test_existing_dc_mismatched_terminator_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    path.write_bytes(data.replace(b".EndVRM\n", b".EndSink\n", 1))
    with pytest.raises(SpdFormatError):
        scan_spd(path)


def test_existing_dc_missing_identity_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    start = data.index(b".VRM ")
    end = data.index(b".EndVRM\n", start) + len(b".EndVRM\n")
    lines = data[start:end].splitlines(keepends=True)
    broken = b"".join(
        line
        for line in lines
        if not line.startswith((b".Map ", b".Node ", b".EndMap"))
    ).replace(
        b'Name = "VRM_LGA_ADC_VDD_070_VDDA/0_DGND"', b'Name = "BROKEN"', 1
    )
    path.write_bytes(data[:start] + broken + data[end:])
    with pytest.raises(SpdFormatError):
        scan_spd(path)


def test_existing_dc_gap_and_nested_block_fail_closed(tmp_path: Path) -> None:
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    data = data.replace(b".EndVRM\n.VRM ", b".EndVRM\n* unknown gap\n.VRM ", 1)
    path.write_bytes(data)
    with pytest.raises(SpdFormatError):
        scan_spd(path)


def test_existing_dc_nested_block_and_unterminated_block_fail_closed(tmp_path: Path) -> None:
    nested = _build(tmp_path, style="dc", name="nested.spd")
    data = nested.read_bytes().replace(
        b'.Pin Name = "Positive Pin"\n',
        b'.Pin Name = "Positive Pin"\n.VRM Nested\n',
        1,
    )
    nested.write_bytes(data)
    with pytest.raises(SpdFormatError):
        scan_spd(nested)

    unterminated = _build(tmp_path, style="dc", name="unterminated.spd")
    data = unterminated.read_bytes().replace(b".EndVRM\n", b".EndPowerDC\n", 1)
    unterminated.write_bytes(data)
    with pytest.raises(SpdFormatError):
        scan_spd(unterminated)


def test_existing_dc_noncanonical_name_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path, style="dc")
    path.write_bytes(
        path.read_bytes().replace(
            b'Name = "VRM_LGA_ADC_VDD_070_VDDA/0_DGND"', b'Name = "BROKEN"', 1
        )
    )
    with pytest.raises(SpdFormatError, match="canonical name"):
        scan_spd(path)


def test_existing_dc_map_must_match_connect_index(tmp_path: Path) -> None:
    path = _build(tmp_path, style="dc")
    path.write_bytes(
        path.read_bytes().replace(
            b".Node Name = Node1001!!1::ADC_VDD_070_VDDA/0",
            b".Node Name = Node9999!!1::ADC_VDD_070_VDDA/0",
            1,
        )
    )
    with pytest.raises(SpdFormatError, match="does not match .Connect"):
        scan_spd(path)


def test_existing_dc_positive_map_deletion_fails_completeness(tmp_path: Path) -> None:
    """A missing map must not be silently restored by block-name inference."""
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    block_start = data.index(b".VRM ")
    positive_start = data.index(b'.Pin Name = "Positive Pin"', block_start)
    positive_end = data.index(b".EndPin\n", positive_start)
    section = data[positive_start:positive_end]
    map_start = section.index(b".Map ")
    map_end = section.index(b".EndMap\n", map_start) + len(b".EndMap\n")
    assert section[map_start:map_end].count(b".Map ") == 1
    section = section[:map_start] + section[map_end:]
    path.write_bytes(data[:positive_start] + section + data[positive_end:])
    with pytest.raises(SpdFormatError, match="has no maps|incomplete"):
        scan_spd(path)


def test_existing_dc_all_positive_maps_deleted_fails_completeness(tmp_path: Path) -> None:
    """Wholesale regeneration requires every required section's map set."""
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    block_start = data.index(b".VRM ")
    positive_start = data.index(b'.Pin Name = "Positive Pin"', block_start)
    positive_end = data.index(b".EndPin\n", positive_start)
    header = data[positive_start : positive_start + len(b'.Pin Name = "Positive Pin"\n')]
    path.write_bytes(data[:positive_start] + header + data[positive_end:])
    with pytest.raises(SpdFormatError, match="has no maps"):
        scan_spd(path)


def test_connect_outer_pin_mismatch_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes()
    data = data.replace(
        b"1 $Package.Node4001!!1::ADC_VDD_070_VDDA/0",
        b"9 $Package.Node4001!!1::ADC_VDD_070_VDDA/0",
        1,
    )
    path.write_bytes(data)
    with pytest.raises(SpdFormatError, match="outer pin"):
        scan_spd(path)


def test_connect_invalid_node_token_fails_closed(tmp_path: Path) -> None:
    path = _build(tmp_path)
    data = path.read_bytes().replace(
        b"$Package.Node4001!!1::ADC_VDD_070_VDDA/0",
        b"$Package.NotANode!!1::ADC_VDD_070_VDDA/0",
        1,
    )
    path.write_bytes(data)
    with pytest.raises(SpdFormatError, match="malformed .Connect"):
        scan_spd(path)


def test_vrm_component_comes_from_power_and_ground_intersection(tmp_path: Path) -> None:
    path = _build(tmp_path)
    scan = scan_spd(path)
    maps = dict(scan.pin_maps.by_circuit)
    maps["GND_HEAVY"] = {"G_CUSTOM": tuple((str(i), f"g{i}") for i in range(100))}
    maps["COMMON"] = {
        "P_CUSTOM": (("1", "p1"), ("2", "p2")),
        "G_CUSTOM": (("3", "n3"),),
    }
    circuits = scan.circuits + (
        CircuitInfo("GND_HEAVY", 100),
        CircuitInfo("COMMON", 3),
    )
    session = Session()
    session.load(replace(scan, pin_maps=PinMapIndex(maps), circuits=circuits))
    assert session._default_vrm_comp("P_CUSTOM", "G_CUSTOM") == "COMMON"
