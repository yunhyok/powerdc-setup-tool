"""Tests for `core/spd_scan.py` -- design §E "test_spd_scan" row.

Every offset is asserted against the fixture (both `si`/`dc` styles and both
`lf`/`crlf` newlines); anchors must fire only inside `[.PowerDC, .EndPowerDC)`;
the decoys (`.PowerDCFoo`, an out-of-section `* PdcElem` comment, `.EndCTE`
while harvesting `.Connect`, `.EndSinkCurrentSource` while harvesting `.Sink`)
must not match; directives are matched case-insensitively; non-UTF-8 bytes are
tolerated.
"""

from __future__ import annotations

import threading

import pytest

import fixtures
from powerdc_setup_tool.core import spd_scan as spd_scan_mod
from powerdc_setup_tool.core.spd_scan import (
    ScanCancelled,
    SpdFormatError,
    read_region,
    read_region_text,
    scan_spd,
)

STYLES = ("si", "dc")
NEWLINES = ("lf", "crlf")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _build(tmp_path, style="si", newline="lf", name="mini.spd"):
    path = tmp_path / name
    fixtures.build_mini_spd(path, style=style, newline=newline)
    return path


def _eol(newline: str) -> bytes:
    return b"\r\n" if newline == "crlf" else b"\n"


def _line_span(data: bytes, needle: bytes, newline: str, *, last: bool = False) -> tuple[int, int]:
    """(start of the line containing *needle*, offset just past its EOL)."""
    hit = data.rindex(needle) if last else data.index(needle)
    start = data.rfind(b"\n", 0, hit) + 1
    end = data.index(_eol(newline), hit) + len(_eol(newline))
    return start, end


def _rewrite(path, replacement_pairs):
    """Rewrite *path* applying ordered (old, new) byte substitutions."""
    data = path.read_bytes()
    for old, new in replacement_pairs:
        assert old in data, old
        data = data.replace(old, new, 1)
    path.write_bytes(data)
    return data


# --------------------------------------------------------------------------- #
# offsets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("newline", NEWLINES)
def test_every_offset_matches_the_file(tmp_path, style, newline):
    path = _build(tmp_path, style=style, newline=newline)
    data = path.read_bytes()
    scan = scan_spd(path)

    assert scan.path == path
    assert scan.file_size == len(data) == path.stat().st_size
    assert scan.elapsed_s >= 0.0

    # line 1: the hex token only, not the whole "WorkflowKey = ..." phrase.
    key = (fixtures.WORKFLOW_KEY_DC if style == "dc" else fixtures.WORKFLOW_KEY_SI).encode()
    assert scan.workflow_key_span is not None
    start, end = scan.workflow_key_span
    assert data[start:end] == key
    assert data[start - 3 : start] == b" = "

    # .PowerDC bounds are *line starts* on both ends (design §B).
    pdc_start, pdc_end = scan.powerdc_span
    assert data[pdc_start:].startswith(b".PowerDC PlotResolution")
    assert data[pdc_end:].startswith(b".EndPowerDC")
    assert data[pdc_start - 1 : pdc_start] == b"\n"
    assert data[pdc_end - 1 : pdc_end] == b"\n"

    # anchors A and B are *line ends* (include the newline).
    assert scan.anchor_pdc_elem_end == _line_span(data, b"* PdcElem", newline)[1]
    assert scan.anchor_spice_end == _line_span(data, b".EndSpiceNetlist", newline)[1]
    assert pdc_start < scan.anchor_pdc_elem_end < scan.anchor_spice_end < pdc_end

    # netlist spans
    nl_start, nl_end = scan.netlist_span
    body_start, body_end = scan.netlist_body_span
    assert data[nl_start:].startswith(b".NetList")
    assert body_start == nl_start + len(b".NetList") + len(_eol(newline))
    assert data[body_start:].startswith(b"\t")
    assert data[body_end:].startswith(b".EndNetList")
    assert nl_end == body_end + len(b".EndNetList") + len(_eol(newline))
    assert data[nl_end:].startswith(b".NetAlias")


@pytest.mark.parametrize("newline", NEWLINES)
def test_netlist_body_is_lf_normalized_and_span_exact(tmp_path, newline):
    path = _build(tmp_path, newline=newline)
    scan = scan_spd(path)

    region = read_region(path, *scan.netlist_body_span)
    assert region.replace(b"\r\n", b"\n").decode() == scan.netlist_body
    assert "\r" not in scan.netlist_body
    assert scan.netlist_body.endswith("\n")
    assert scan.netlist_body.splitlines() == fixtures._netlist_entries()
    # ScanResult.nets is the parsed body (design §B).
    assert [e.raw for e in scan.nets] == fixtures._netlist_entries()


def test_lf_and_crlf_agree_on_content(tmp_path):
    lf = scan_spd(_build(tmp_path, newline="lf", name="lf.spd"))
    crlf = scan_spd(_build(tmp_path, newline="crlf", name="crlf.spd"))
    assert lf.netlist_body == crlf.netlist_body
    assert lf.pin_maps.by_circuit == crlf.pin_maps.by_circuit
    assert lf.circuits == crlf.circuits
    assert lf.other_circuit_names == crlf.other_circuit_names
    # ... but every crlf offset is strictly larger (one extra byte per line).
    assert crlf.anchor_spice_end > lf.anchor_spice_end
    assert crlf.file_size > lf.file_size


# --------------------------------------------------------------------------- #
# `.Connect` harvest (spec ADDENDUM)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("newline", NEWLINES)
def test_connect_pin_harvest(tmp_path, newline):
    scan = scan_spd(_build(tmp_path, newline=newline))
    pins = scan.pin_maps

    assert pins.pins("LGA", fixtures.POWER_NET_A) == (
        ("1", "Node1001"),
        ("2", "Node1002"),
        ("3", "Node1003"),
    )
    assert pins.pins("LGA", fixtures.GROUND_NET) == (("7", "Node1007"), ("8", "Node1008"))
    assert pins.pins("SITE1", fixtures.POWER_NET_B) == (("1", "Node3001"), ("2", "Node3002"))
    assert pins.pins("SITE0", "NOPE") == ()
    assert pins.pins("NOPE", fixtures.GROUND_NET) == ()

    # node value is the bare id: the spec §9 template rebuilds the rest.
    for _pin, node in pins.pins("SITE0", fixtures.POWER_NET_A):
        assert node.startswith("Node") and "!!" not in node and "::" not in node

    # circuits_for() ordering: most pins first (model.PinMapIndex).
    assert pins.circuits_for(fixtures.GROUND_NET) == ["LGA", "SITE0", "SITE1", "C1_0", "C1_1"]


@pytest.mark.parametrize("newline", NEWLINES)
def test_circuits_and_other_circuits(tmp_path, newline):
    scan = scan_spd(_build(tmp_path, newline=newline))

    assert tuple(c.name for c in scan.circuits) == fixtures.CIRCUITS
    counts = {c.name: c.pin_count for c in scan.circuits}
    # ALI1's single pin has no "::net" -- still a pin of the component.
    assert counts == {"ALI1": 1, "C1_0": 2, "C1_1": 2, "SITE0": 4, "SITE1": 4, "LGA": 8}
    assert scan.other_circuit_names == fixtures.OTHER_CIRCUIT_NAMES == ("C1_0", "C1_1")


def test_pin_without_net_suffix_is_skipped(tmp_path):
    scan = scan_spd(_build(tmp_path))
    # ALI1 has exactly one pin line and it carries no `::net`.
    assert scan.pin_maps.by_circuit["ALI1"] == {}
    assert all("" not in per_net for per_net in scan.pin_maps.by_circuit.values())


def test_endcte_decoy_does_not_terminate_a_connect_block(tmp_path):
    """`.EndCTE` is a different directive (spec ADDENDUM) -- mandatory decoy."""
    path = _build(tmp_path)
    # Move the `.CTE ... .EndCTE` decoy *inside* the LGA `.Connect` block, so a
    # scanner that prefix-matches `.EndC` loses every pin after it.
    data = path.read_bytes()
    data = data.replace(b'.CTE Name = "ThermalDecoy"\n1 SomeField = 1\n.EndCTE\n', b"", 1)
    data = data.replace(
        b"4 $Package.Node1004!!4::ADC_VDD_120_VDDB/1\n",
        b'.CTE Name = "ThermalDecoy"\n1 SomeField = 1\n.EndCTE\n'
        b"4 $Package.Node1004!!4::ADC_VDD_120_VDDB/1\n",
        1,
    )
    path.write_bytes(data)

    scan = scan_spd(path)
    assert scan.pin_maps.pins("LGA", fixtures.POWER_NET_B) == (
        ("4", "Node1004"),
        ("5", "Node1005"),
        ("6", "Node1006"),
    )
    assert scan.pin_maps.pins("LGA", fixtures.GROUND_NET) == (
        ("7", "Node1007"),
        ("8", "Node1008"),
    )


def test_component_blocks_are_not_harvested(tmp_path):
    """`.Component` carries no pin->net data (spec ADDENDUM scanner note)."""
    scan = scan_spd(_build(tmp_path))
    assert set(scan.pin_maps.by_circuit) == set(fixtures.CIRCUITS)
    assert "LGA_PKG" not in scan.pin_maps.by_circuit  # a .Part name, not a RefDes


# --------------------------------------------------------------------------- #
# decoys
# --------------------------------------------------------------------------- #


def test_powerdcfoo_decoy_is_not_a_section(tmp_path):
    path = _build(tmp_path)
    reference = scan_spd(path)
    _rewrite(
        path,
        [
            (
                b"* PowerDC Setup description lines\n",
                b"* PowerDC Setup description lines\n.PowerDCFoo Bar = 1\n.EndPowerDCFoo\n",
            )
        ],
    )
    scan = scan_spd(path)
    data = path.read_bytes()
    assert data[scan.powerdc_span[0] :].startswith(b".PowerDC PlotResolution")
    assert data[scan.powerdc_span[1] :].startswith(b".EndPowerDC\n")
    # the decoy pair sits before .PowerDC and shifted every offset by its size
    shift = len(b".PowerDCFoo Bar = 1\n.EndPowerDCFoo\n")
    assert scan.powerdc_span[0] == reference.powerdc_span[0] + shift
    assert scan.anchor_pdc_elem_end == reference.anchor_pdc_elem_end + shift


def test_pdc_elem_comment_outside_powerdc_is_ignored(tmp_path):
    path = _build(tmp_path)
    reference = scan_spd(path)
    # length-preserving substitution, so every reference offset still applies
    _rewrite(
        path,
        [(b"* Geometry description lines\n", b"* PdcElem  description lines\n")],
    )
    scan = scan_spd(path)
    data = path.read_bytes()
    assert len(data) == reference.file_size
    # the real anchor is still the one inside .PowerDC
    assert scan.powerdc_span[0] < scan.anchor_pdc_elem_end < scan.powerdc_span[1]
    assert data[: scan.anchor_pdc_elem_end].count(b"* PdcElem") == 2
    assert scan.anchor_pdc_elem_end == reference.anchor_pdc_elem_end


def test_spice_anchor_outside_powerdc_is_ignored(tmp_path):
    """Anchor B is matched only inside `[.PowerDC, .EndPowerDC)` (spec §9)."""
    path = _build(tmp_path)
    reference = scan_spd(path)
    _rewrite(
        path,
        [
            (
                b".EndSimuOptionSettings\n",
                b'.SpiceNetlist Name = "Decoy"\n.EndSpiceNetlist\n',
            )
        ],
    )
    scan = scan_spd(path)
    data = path.read_bytes()
    assert data.count(b".EndSpiceNetlist") == 2
    assert scan.powerdc_span[0] < scan.anchor_spice_end < scan.powerdc_span[1]
    shift = len(b'.SpiceNetlist Name = "Decoy"\n.EndSpiceNetlist\n') - len(
        b".EndSimuOptionSettings\n"
    )
    assert scan.anchor_spice_end == reference.anchor_spice_end + shift


def test_file_without_trailing_newline(tmp_path):
    path = _build(tmp_path)
    reference = scan_spd(path)
    data = path.read_bytes()
    assert data.endswith(b".End\n")
    path.write_bytes(data[:-1])

    scan = scan_spd(path)
    assert scan.file_size == reference.file_size - 1
    assert scan.netlist_span == reference.netlist_span
    assert scan.netlist_body == reference.netlist_body
    assert scan.anchor_spice_end == reference.anchor_spice_end


def test_endsinkcurrentsource_does_not_close_the_sink_block(tmp_path):
    path = _build(tmp_path, style="dc")
    data = path.read_bytes()
    scan = scan_spd(path)

    assert len(scan.existing_sinks) == 2
    assert scan.existing_blocks_span is not None
    _start, end = scan.existing_blocks_span
    # ends after the last `.EndSink`, not after `.EndSinkCurrentSource`
    assert data[:end].endswith(b".EndSink\n")
    assert data[end:].startswith(b"\n* ConstraintDisc")


# --------------------------------------------------------------------------- #
# existing VRM/Sink blocks (design §G.2)
# --------------------------------------------------------------------------- #


def test_si_fixture_has_no_existing_blocks(tmp_path):
    scan = scan_spd(_build(tmp_path, style="si"))
    assert scan.existing_vrms == ()
    assert scan.existing_sinks == ()
    assert scan.existing_blocks_span is None
    assert scan.other_circuit_names == ("C1_0", "C1_1")  # from `.Connect`, not `.OtherCircuit`


@pytest.mark.parametrize("newline", NEWLINES)
def test_existing_blocks_span_on_dc_fixture(tmp_path, newline):
    path = _build(tmp_path, style="dc", newline=newline)
    data = path.read_bytes()
    scan = scan_spd(path)

    assert scan.existing_blocks_span is not None
    start, end = scan.existing_blocks_span
    assert start == scan.anchor_spice_end
    assert end == _line_span(data, b".EndSink" + _eol(newline), newline, last=True)[1]
    assert data[start:].startswith(b".VRM ")
    region = read_region_text(path, start, end)
    assert region.count(".EndVRM") == 2
    assert region.count(".EndSink\n") + region.count(".EndSink\r\n") == 2
    assert region.rstrip().endswith(".EndSink")


def test_existing_block_headers_are_parsed(tmp_path):
    scan = scan_spd(_build(tmp_path, style="dc"))

    assert [b.kind for b in scan.existing_vrms] == ["vrm", "vrm"]
    assert [b.kind for b in scan.existing_sinks] == ["sink", "sink"]

    vrm = scan.existing_vrms[0]
    assert vrm.name == f"VRM_LGA_{fixtures.POWER_NET_A}_{fixtures.GROUND_NET}"
    assert (vrm.comp, vrm.net, vrm.gnet) == ("LGA", fixtures.POWER_NET_A, fixtures.GROUND_NET)
    assert vrm.values == {
        "NominalVoltage": "0.7",
        "SenseVoltage": "0.7",
        "OutputCurrent": "1",
    }
    assert "Name" not in vrm.values  # exposed as ExistingBlock.name

    sink = scan.existing_sinks[1]
    assert sink.name == f"SINK_SITE1_{fixtures.POWER_NET_B}_{fixtures.GROUND_NET}"
    assert (sink.comp, sink.net, sink.gnet) == (
        "SITE1",
        fixtures.POWER_NET_B,
        fixtures.GROUND_NET,
    )
    assert sink.values == {
        "NominalVoltage": "1.2",
        "Current": "1",
        "Model": "2",
        "PFMode": "2",
        "PinEqualCurrent": "1",
    }


def test_block_net_names_survive_underscore_ambiguity(tmp_path):
    """`VRM_{comp}_{pnet}_{gnet}` cannot be split by underscore; nodes win."""
    scan = scan_spd(_build(tmp_path, style="dc"))
    for block in scan.existing_vrms + scan.existing_sinks:
        assert "_" in block.net  # the naive rsplit answer would be wrong
        assert block.gnet == fixtures.GROUND_NET
        assert f"{block.comp}_{block.net}_{block.gnet}" in block.name


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #


def test_mixed_case_directives(tmp_path):
    path = _build(tmp_path, style="dc")
    reference = scan_spd(path)
    _rewrite(
        path,
        [
            (b".Connect LGA ", b".CONNECT LGA "),
            (b"7 $Package.Node1007!!7::DGND\n8 $Package.Node1008!!8::DGND\n.EndC\n",
             b"7 $Package.Node1007!!7::DGND\n8 $Package.Node1008!!8::DGND\n.endc\n"),
            (b".PowerDC PlotResolution", b".POWERDC PlotResolution"),
            (b"* PdcElem description lines", b"* pdcelem DESCRIPTION lines"),
            (b".EndSpiceNetlist", b".endSPICEnetlist"),
            (b".NetList\n", b".NETLIST\n"),
            (b".EndNetList\n", b".endnetlist\n"),
            (b".EndPowerDC", b".ENDPOWERDC"),
            (b".EndVRM\n", b".endvrm\n"),
        ],
    )
    scan = scan_spd(path)

    assert scan.powerdc_span == reference.powerdc_span
    assert scan.anchor_pdc_elem_end == reference.anchor_pdc_elem_end
    assert scan.anchor_spice_end == reference.anchor_spice_end
    assert scan.netlist_span == reference.netlist_span
    assert scan.netlist_body_span == reference.netlist_body_span
    assert scan.netlist_body == reference.netlist_body
    assert scan.pin_maps.by_circuit == reference.pin_maps.by_circuit
    assert scan.existing_blocks_span == reference.existing_blocks_span
    assert len(scan.existing_vrms) == 2


def test_non_utf8_bytes_are_tolerated(tmp_path):
    """design §G.6: match on errors="replace"; never blow up, never mis-offset."""
    path = _build(tmp_path)
    reference = scan_spd(path)
    data = path.read_bytes()
    data = data.replace(
        b"* Geometry description lines",
        b"* Geometry \xb5-strip description lines",
        1,
    )
    path.write_bytes(data)
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")

    scan = scan_spd(path)
    shift = len(b" \xb5-strip")
    assert scan.powerdc_span == tuple(v + shift for v in reference.powerdc_span)
    assert scan.anchor_pdc_elem_end == reference.anchor_pdc_elem_end + shift
    assert scan.netlist_body == reference.netlist_body
    assert scan.pin_maps.by_circuit == reference.pin_maps.by_circuit


def test_non_ascii_net_name_warns(tmp_path):
    path = _build(tmp_path)
    _rewrite(path, [(b"3 $Package.Node2003!!3::DGND\n", b"3 $Package.Node2003!!3::DGND\xc2\xb5\n")])
    scan = scan_spd(path)
    assert any("non-ASCII" in w for w in scan.warnings)


def test_clean_fixture_produces_no_warnings(tmp_path):
    for style in STYLES:
        for newline in NEWLINES:
            scan = scan_spd(_build(tmp_path, style=style, newline=newline, name=f"{style}{newline}.spd"))
            assert scan.warnings == (), (style, newline, scan.warnings)


def test_missing_sections_raise(tmp_path):
    cases = {
        "no .PowerDC": (b".PowerDC PlotResolution", b".PowerDQ PlotResolution"),
        "no .EndPowerDC": (b".EndPowerDC", b".EndPowerDQ"),
        "no PdcElem": (b"* PdcElem description lines", b"* PdcElemental description lines"),
        "no .EndSpiceNetlist": (b".EndSpiceNetlist", b".EndSpiceNetlists"),
        "no .NetList": (b".NetList\n", b".NetLists\n"),
        "no .EndNetList": (b".EndNetList\n", b".EndNetLists\n"),
    }
    for i, (label, (old, new)) in enumerate(cases.items()):
        path = _build(tmp_path, name=f"broken{i}.spd")
        _rewrite(path, [(old, new)])
        with pytest.raises(SpdFormatError):
            scan_spd(path)
        assert label  # keeps the case label in the failure output


def test_unterminated_connect_block_fails_closed(tmp_path):
    path = _build(tmp_path)
    _rewrite(path, [(b"8 $Package.Node1008!!8::DGND\n.EndC\n", b"8 $Package.Node1008!!8::DGND\n")])
    with pytest.raises(SpdFormatError, match="not terminated"):
        scan_spd(path)


# --------------------------------------------------------------------------- #
# progress + read_region
# --------------------------------------------------------------------------- #


def test_progress_callback(tmp_path):
    path = _build(tmp_path)
    calls: list[tuple[int, int]] = []
    scan = scan_spd(path, progress=lambda done, total: calls.append((done, total)))

    assert calls[0] == (0, scan.file_size)
    assert calls[-1] == (scan.file_size, scan.file_size)
    assert all(total == scan.file_size for _done, total in calls)
    assert all(0 <= done <= total for done, total in calls)
    assert [done for done, _ in calls] == sorted(done for done, _ in calls)
    # throttled: a 4 KB fixture must not produce a call per line
    assert len(calls) <= 4


def test_scan_without_progress_is_identical(tmp_path):
    path = _build(tmp_path)
    a = scan_spd(path)
    b = scan_spd(path, progress=lambda _done, _total: None)
    assert a.netlist_body == b.netlist_body
    assert a.powerdc_span == b.powerdc_span


def test_read_region(tmp_path):
    path = _build(tmp_path)
    data = path.read_bytes()
    scan = scan_spd(path)

    assert read_region(path, *scan.netlist_span).startswith(b".NetList\n")
    assert read_region(path, *scan.netlist_span).endswith(b".EndNetList\n")
    assert read_region(path, 0, 0) == b""
    assert read_region(path, 5, 9) == data[5:9]
    assert read_region_text(path, *scan.workflow_key_span) == fixtures.WORKFLOW_KEY_SI
    with pytest.raises(ValueError):
        read_region(path, 10, 5)
    with pytest.raises(ValueError):
        read_region(path, -1, 5)


# --------------------------------------------------------------------------- #
# cancellation (design §D's `cancel` hook, scan side)
# --------------------------------------------------------------------------- #


class _CancelAfter:
    """`threading.Event` stand-in that reports "set" from its *n*-th poll on.

    `scan_spd` only ever calls `is_set()`, so counting the calls is the cheapest
    way to observe *when* the scan looked -- which is the property under test.
    """

    def __init__(self, trip_on: int) -> None:
        self.trip_on = trip_on
        self.polls = 0

    def is_set(self) -> bool:
        self.polls += 1
        return self.polls >= self.trip_on


def test_scan_cancel_event_is_optional_and_ignored_when_unset(tmp_path):
    path = _build(tmp_path)
    reference = scan_spd(path)
    scan = scan_spd(path, cancel=threading.Event())
    assert scan.netlist_body == reference.netlist_body
    assert scan.powerdc_span == reference.powerdc_span
    assert scan.pin_maps.by_circuit == reference.pin_maps.by_circuit


def test_scan_cancel_set_up_front_raises_before_reading(tmp_path):
    path = _build(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        scan_spd(path, cancel=cancel)


def test_scan_cancel_midway_raises_and_yields_no_result(tmp_path, monkeypatch):
    """A set event aborts the walk instead of running to EOF.

    The poll granularity is monkeypatched to "every line" (the `test_chunk_boundary`
    idiom) so a fixture far smaller than one real batch can still exercise the
    mid-scan path.
    """
    path = _build(tmp_path)
    monkeypatch.setattr(spd_scan_mod, "_CANCEL_POLL_MASK", 0)

    total_lines = path.read_bytes().count(b"\n")
    cancel = _CancelAfter(trip_on=3)
    with pytest.raises(ScanCancelled):
        scan_spd(path, cancel=cancel)
    # stopped on the third line, nowhere near EOF
    assert cancel.polls == 3 < total_lines


def test_scan_cancel_is_polled_per_line_batch_not_per_line(tmp_path):
    """design §G.5 budget: the event is checked once per `_CANCEL_POLL_LINES`."""
    path = _build(tmp_path)
    total_lines = path.read_bytes().count(b"\n")
    assert total_lines < spd_scan_mod._CANCEL_POLL_LINES  # one batch, by construction

    cancel = _CancelAfter(trip_on=10**9)  # never trips
    scan_spd(path, cancel=cancel)
    # one up-front poll before the file is opened + one for the first (only) batch
    assert cancel.polls == 2, "a sub-batch file must not poll per line"


def test_scan_cancelled_is_not_a_format_error(tmp_path):
    """`SpdFormatError` is a `ValueError`; a cancel must not be mistaken for one."""
    assert issubclass(ScanCancelled, RuntimeError)
    assert not issubclass(ScanCancelled, ValueError)


# --------------------------------------------------------------------------- #
# lossless decoding (design §G.6)
# --------------------------------------------------------------------------- #


def test_netlist_body_round_trips_non_utf8_bytes_exactly(tmp_path):
    """Regression: `netlist_body` becomes *output* when the classification changes.

    Decoding it with ``errors="replace"`` silently rewrote every undecodable
    byte to U+FFFD, so an untouched `.NetList` entry came back out mangled.
    ``surrogateescape`` keeps the original byte recoverable.
    """
    path = _build(tmp_path)
    _rewrite(path, [(b"\tSIG_CLK_IN::", b"\tSIG_CLK\xb5IN::")])
    data = path.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        data.decode("utf-8")

    scan = scan_spd(path)
    assert "�" not in scan.netlist_body
    assert "\tSIG_CLK\udcb5IN::" in scan.netlist_body
    # the whole body re-encodes to exactly the bytes it was read from
    body_start, body_end = scan.netlist_body_span
    assert scan.netlist_body.encode("utf-8", "surrogateescape") == data[body_start:body_end]
    # ... and the parsed entry carries the byte too, not a replacement char
    assert any(entry.name == "SIG_CLK\udcb5IN" for entry in scan.nets)


def test_non_utf8_net_name_agrees_between_pin_index_and_netlist(tmp_path):
    """Both harvests use the same codec, so the two spellings compare equal.

    With one side on ``replace`` and the other on ``surrogateescape`` a
    non-UTF-8 net would exist twice under different names and silently show
    zero pins.
    """
    path = _build(tmp_path)
    # rename the ground net *everywhere* (`.Connect` pin lines and `.NetList`
    # alike), so the file stays internally consistent -- only its encoding is odd
    path.write_bytes(path.read_bytes().replace(fixtures.GROUND_NET.encode(), b"DG\xb5ND"))
    scan = scan_spd(path)

    assert "DG\udcb5ND" in {entry.name for entry in scan.nets}
    assert scan.pin_maps.pins(fixtures.VRM_COMP, "DG\udcb5ND") == (
        ("7", "Node1007"),
        ("8", "Node1008"),
    )
    # the *old*, replacement-char spelling must not appear anywhere
    assert "DG�ND" not in {entry.name for entry in scan.nets}
    assert not any("DG�ND" in nets for nets in scan.pin_maps.by_circuit.values())
