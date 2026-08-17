"""`core/writer.py` -- design §E `test_writer`; design §D splice plan.

Chunk 1's scanner is deliberately *not* imported: every `ScanResult` here is
built from offsets found with `bytes.find`/`bytes.index` on the fixture itself,
so these tests pin the writer's contract independently of the scanner.
"""

from __future__ import annotations

import dataclasses
import shutil
import threading
from pathlib import Path

import pytest

import fixtures
from fixtures import GROUND_NET, POWER_NET_A, POWER_NET_B, build_mini_spd, expected_dc
from powerdc_setup_tool.core import writer as writer_mod
from powerdc_setup_tool.core.model import (
    CircuitInfo,
    PinMapIndex,
    ScanResult,
    SinkConfig,
    VrmConfig,
)
from powerdc_setup_tool.core.naming import die_of, guess_voltage, sink_component
from powerdc_setup_tool.core.writer import (
    InsufficientDiskSpaceError,
    WriteCancelled,
    WritePlan,
    write_spd,
)

VRM_COMP = fixtures.VRM_COMP


# ---------------------------------------------------------------------------
# Fixture -> ScanResult, by hand (no `spd_scan` import)
# ---------------------------------------------------------------------------


def _pin_maps() -> PinMapIndex:
    by_circuit: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
    for refdes, _part, pins in fixtures._CONNECT_BLOCKS:
        per_net: dict[str, list[tuple[str, str]]] = {}
        for pin, node, net in pins:
            if net is None:  # ADDENDUM: unconnected pins have no `::net` suffix
                continue
            per_net.setdefault(net, []).append((pin, node))
        by_circuit[refdes] = {net: tuple(items) for net, items in per_net.items()}
    return PinMapIndex(by_circuit=by_circuit)


def _scan_fixture(path: Path, *, style: str = "si", newline: str = "lf") -> ScanResult:
    """Build a `ScanResult` for *path* by locating each design §D anchor by hand."""
    data = path.read_bytes()
    nl = b"\r\n" if newline == "crlf" else b"\n"

    def line_end(token: bytes) -> int:
        start = data.index(token)
        return data.index(nl, start) + len(nl)

    key = (fixtures.WORKFLOW_KEY_DC if style == "dc" else fixtures.WORKFLOW_KEY_SI).encode()
    key_start = data.index(key)

    netlist_start = data.index(b".NetList" + nl)
    body_start = netlist_start + len(b".NetList" + nl)
    end_netlist = data.index(b".EndNetList" + nl, body_start)

    anchor_spice_end = line_end(b".EndSpiceNetlist")
    if style == "dc":
        last_end_sink = data.rindex(b".EndSink" + nl) + len(b".EndSink" + nl)
        existing = (anchor_spice_end, last_end_sink)
    else:
        existing = None

    pin_maps = _pin_maps()
    return ScanResult(
        path=path,
        file_size=len(data),
        elapsed_s=0.0,
        workflow_key_span=(key_start, key_start + len(key)),
        powerdc_span=(data.index(b".PowerDC "), data.index(b".EndPowerDC")),
        anchor_pdc_elem_end=line_end(b"* PdcElem description lines"),
        anchor_spice_end=anchor_spice_end,
        existing_blocks_span=existing,
        netlist_span=(netlist_start, end_netlist + len(b".EndNetList" + nl)),
        netlist_body_span=(body_start, end_netlist),
        netlist_body=data[body_start:end_netlist].decode("utf-8").replace("\r\n", "\n"),
        nets=(),
        pin_maps=pin_maps,
        circuits=tuple(
            CircuitInfo(name=name, pin_count=sum(len(p) for p in nets.values()))
            for name, nets in pin_maps.by_circuit.items()
        ),
        other_circuit_names=fixtures.OTHER_CIRCUIT_NAMES,
        existing_vrms=(),
        existing_sinks=(),
        warnings=(),
    )


def _plan(
    scan: ScanResult,
    *,
    other_circuits: bool = True,
    patch_workflow_key: bool = True,
    netlist_body: str | None = None,
) -> WritePlan:
    """The canonical config `expected_dc()` is the golden output of (design §D)."""
    vrms: list[tuple[VrmConfig, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]] = []
    sinks: list[tuple[SinkConfig, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]] = []
    for net in (POWER_NET_A, POWER_NET_B):
        volts = guess_voltage(net) or 1.0
        vrms.append(
            (
                VrmConfig(
                    net=net,
                    gnet=GROUND_NET,
                    comp=VRM_COMP,
                    nominal_voltage=volts,
                    sense_voltage=volts,
                    output_current=1.0,
                ),
                scan.pin_maps.pins(VRM_COMP, net),
                scan.pin_maps.pins(VRM_COMP, GROUND_NET),
            )
        )
        comp = sink_component(die_of(net))
        sinks.append(
            (
                SinkConfig(
                    net=net, gnet=GROUND_NET, comp=comp, nominal_voltage=volts, current=1.0
                ),
                scan.pin_maps.pins(comp, net),
                scan.pin_maps.pins(comp, GROUND_NET),
            )
        )
    return WritePlan(
        vrms=vrms,
        sinks=sinks,
        netlist_body=netlist_body,
        other_circuits=fixtures.OTHER_CIRCUIT_NAMES if other_circuits else None,
        patch_workflow_key=patch_workflow_key,
    )


def _convert(
    tmp_path: Path,
    *,
    style: str = "si",
    newline: str = "lf",
    name: str = "in.spd",
    **plan_kwargs: object,
) -> bytes:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / name
    build_mini_spd(source, style=style, newline=newline)  # type: ignore[arg-type]
    scan = _scan_fixture(source, style=style, newline=newline)
    out = tmp_path / f"{Path(name).stem}_DC.spd"
    write_spd(scan, _plan(scan, **plan_kwargs), out)  # type: ignore[arg-type]
    assert not (tmp_path / f"{out.name}.part").exists()
    return out.read_bytes()


# ---------------------------------------------------------------------------
# The golden round trip
# ---------------------------------------------------------------------------


def test_si_fixture_converts_to_expected_dc(tmp_path: Path) -> None:
    assert _convert(tmp_path) == expected_dc().encode("utf-8")


def test_output_has_no_cr_bytes(tmp_path: Path) -> None:
    assert b"\r" not in _convert(tmp_path)


def test_crlf_input_yields_identical_lf_output(tmp_path: Path) -> None:
    # design §G.6: output is unconditionally LF-only, including copied regions.
    out = _convert(tmp_path, newline="crlf")
    assert b"\r" not in out
    assert out == expected_dc().encode("utf-8")


def test_expected_dc_is_the_dc_fixture(tmp_path: Path) -> None:
    # The SI/DC miniatures are a converted pair, so this also cross-checks that
    # `pdc_gen`'s §9 templates agree with the ones `fixtures.py` spells out.
    dc = tmp_path / "dc.spd"
    build_mini_spd(dc, style="dc")
    assert dc.read_bytes() == expected_dc().encode("utf-8")


def test_splice_order_matches_design_d(tmp_path: Path) -> None:
    text = _convert(tmp_path).decode("utf-8")
    assert '* PdcElem description lines\n.OtherCircuit Device = C1_0 Name = "C1_0"\n' in text
    assert '.OtherCircuit Device = C1_1 Name = "C1_1"\n.SpiceNetlist Name =' in text
    assert ".EndSpiceNetlist\n.VRM " in text  # spec §3 contiguity: 0 blank lines
    assert ".EndVRM\n.VRM " in text
    assert ".EndVRM\n.Sink " in text
    assert ".EndSink\n.Sink " in text
    assert text.index(".VRM ") < text.index(".Sink ")  # all VRMs, then all Sinks
    assert text.count(".VRM ") == text.count(".EndVRM\n") == 2
    assert text.count(".Sink ") == text.count(".EndSink\n") == 2
    assert "VRM_LGA_" + POWER_NET_A + "_DGND" in text
    assert "SINK_SITE0_" + POWER_NET_A + "_DGND" in text
    assert "SINK_SITE1_" + POWER_NET_B + "_DGND" in text
    assert "\n\n.VRM" not in text and "\n\n.Sink" not in text


def test_untouched_regions_are_copied_verbatim(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    original = source.read_bytes()
    out = _convert(tmp_path)
    # everything from `.Celsius` to EOF is diff-clean per spec §8
    tail = original[original.index(b".Celsius") :]
    assert out.endswith(tail)
    # and the `.Connect` pin-map region is untouched too
    head = original[original.index(b".CTE") : original.index(b".CompCollection")]
    assert head in out


# ---------------------------------------------------------------------------
# Idempotence (design §E / §G.2)
# ---------------------------------------------------------------------------


def test_dc_input_regenerates_wholesale_and_is_idempotent(tmp_path: Path) -> None:
    from_si = _convert(tmp_path / "a", style="si")
    from_dc = _convert(tmp_path / "b", style="dc")
    assert from_dc == from_si
    text = from_dc.decode("utf-8")
    assert text.count(".EndVRM\n") == 2  # not 4: existing blocks were skipped
    assert text.count(".EndSink\n") == 2
    assert text.count('.OtherCircuit Device = C1_0 ') == 1  # not duplicated either


def test_dc_input_keeps_existing_other_circuits_when_not_regenerating(tmp_path: Path) -> None:
    # `other_circuits=None` means "leave whatever the input has alone".
    out = _convert(tmp_path, style="dc", other_circuits=False)
    assert out.count(b'.OtherCircuit Device = C1_0 Name = "C1_0"\n') == 1
    assert out == expected_dc().encode("utf-8")  # the input's own run, carried through


# ---------------------------------------------------------------------------
# Export options (design §C / §D steps 1, 3, 8)
# ---------------------------------------------------------------------------


def test_workflow_key_patch_on_and_off(tmp_path: Path) -> None:
    patched = _convert(tmp_path / "on", patch_workflow_key=True).decode("utf-8")
    assert patched.startswith(f"Title WorkflowKey = {fixtures.WORKFLOW_KEY_DC} - ")

    untouched = _convert(tmp_path / "off", patch_workflow_key=False)
    assert untouched == expected_dc(patch_workflow_key=False).encode("utf-8")
    assert untouched.startswith(f"Title WorkflowKey = {fixtures.WORKFLOW_KEY_SI} - ".encode())


def test_workflow_key_patch_skipped_when_span_is_absent(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = dataclasses.replace(_scan_fixture(source), workflow_key_span=None)
    out = tmp_path / "out.spd"
    write_spd(scan, _plan(scan, patch_workflow_key=True), out)
    assert out.read_bytes() == expected_dc(patch_workflow_key=False).encode("utf-8")


def test_other_circuits_disabled(tmp_path: Path) -> None:
    out = _convert(tmp_path, other_circuits=False)
    assert out == expected_dc(other_circuits=False).encode("utf-8")
    assert b".OtherCircuit" not in out


def test_netlist_body_none_copies_the_original_bytes(tmp_path: Path) -> None:
    # design §G.1 / spec §8: the netlist must come out byte-identical.
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"
    write_spd(scan, _plan(scan, netlist_body=None), out)
    original = source.read_bytes()
    body = original[scan.netlist_body_span[0] : scan.netlist_body_span[1]]
    assert body in out.read_bytes()


def test_netlist_body_replaced(tmp_path: Path) -> None:
    body = (
        "\t::Unselected||DropShape RiseTime = 0ps %Coupling = 0\n"
        "\tGroundNets Color = LIME\n"
        "\tPowerNets Color = RED\n"
        f"\t{GROUND_NET} -> GroundNets Color = GREEN Voltage = 0\n"
        f"\t{POWER_NET_A} -> PowerNets Color = RED\n"
    )
    out = _convert(tmp_path, netlist_body=body)
    assert out == expected_dc(netlist_body=body).encode("utf-8")
    assert b".NetList\n\t::Unselected" in out
    assert b"Color = RED\n.EndNetList\n" in out


def test_netlist_body_is_lf_normalized_and_newline_terminated(tmp_path: Path) -> None:
    out = _convert(tmp_path, netlist_body="\tDGND -> GroundNets Color = GREEN Voltage = 0\r\n")
    assert b"\r" not in out
    assert out == expected_dc(
        netlist_body="\tDGND -> GroundNets Color = GREEN Voltage = 0\n"
    ).encode("utf-8")

    no_eol = _convert(tmp_path / "b", netlist_body="\tDGND -> GroundNets Color = GREEN")
    assert b"Color = GREEN\n.EndNetList\n" in no_eol


# ---------------------------------------------------------------------------
# Guards: same path, cancel, disk space, failure cleanup
# ---------------------------------------------------------------------------


def test_same_path_raises_value_error(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    original = source.read_bytes()
    scan = _scan_fixture(source)
    plan = _plan(scan)

    with pytest.raises(ValueError, match="differ from the source"):
        write_spd(scan, plan, source)
    with pytest.raises(ValueError, match="differ from the source"):
        write_spd(scan, plan, tmp_path / "." / "in.spd")
    with pytest.raises(ValueError, match="differ from the source"):
        write_spd(scan, plan, tmp_path / "sub" / ".." / "in.spd")

    assert source.read_bytes() == original
    assert not (tmp_path / "in.spd.part").exists()


def test_cancel_before_write_deletes_partial(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(WriteCancelled):
        write_spd(scan, _plan(scan), out, cancel=cancel)

    assert not out.exists()
    assert not (tmp_path / "out.spd.part").exists()
    assert list(tmp_path.iterdir()) == [source]


def test_cancel_mid_write_deletes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 8)  # many chunks -> many polls
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"
    cancel = threading.Event()

    with pytest.raises(WriteCancelled):
        write_spd(scan, _plan(scan), out, progress=lambda _d, _t: cancel.set(), cancel=cancel)

    assert not out.exists()
    assert not (tmp_path / "out.spd.part").exists()


def test_failure_deletes_partial_and_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("render exploded")

    monkeypatch.setattr(writer_mod, "render_vrm", boom)
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"

    with pytest.raises(RuntimeError, match="render exploded"):
        write_spd(scan, _plan(scan), out)

    assert not out.exists()
    assert not (tmp_path / "out.spd.part").exists()


def test_free_space_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"

    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        writer_mod.shutil,
        "disk_usage",
        lambda _p: type(usage)(usage.total, usage.used, scan.file_size),  # < size x 1.25
    )
    with pytest.raises(InsufficientDiskSpaceError, match="Not enough free space"):
        write_spd(scan, _plan(scan), out)

    assert not out.exists()
    assert not (tmp_path / "out.spd.part").exists()


def test_output_is_written_atomically_over_an_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"
    out.write_bytes(b"stale contents")

    write_spd(scan, _plan(scan), out)

    assert out.read_bytes() == expected_dc().encode("utf-8")
    assert not (tmp_path / "out.spd.part").exists()


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def test_progress_is_monotonic_and_completes(tmp_path: Path) -> None:
    source = tmp_path / "in.spd"
    build_mini_spd(source, style="si")
    scan = _scan_fixture(source)
    out = tmp_path / "out.spd"
    calls: list[tuple[int, int]] = []

    write_spd(scan, _plan(scan), out, progress=lambda done, total: calls.append((done, total)))

    assert calls, "progress must be reported at least once"
    assert [done for done, _total in calls] == sorted(done for done, _total in calls)
    assert all(0 <= done <= total for done, total in calls)
    done, total = calls[-1]
    assert done == total == out.stat().st_size


def _progress_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, style: str
) -> tuple[int, int]:
    """(estimated denominator, real output size) for a conversion of *style*."""
    monkeypatch.setattr(writer_mod, "_PROGRESS_MIN_INTERVAL_S", -1.0)  # report every chunk
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 64)
    source = tmp_path / "in.spd"
    build_mini_spd(source, style=style)  # type: ignore[arg-type]
    scan = _scan_fixture(source, style=style)
    out = tmp_path / "out.spd"
    calls: list[tuple[int, int]] = []
    write_spd(scan, _plan(scan), out, progress=lambda d, t: calls.append((d, t)))
    estimated = {total for _done, total in calls[:-1]}
    assert len(estimated) == 1, "the denominator must not move mid-write"
    return estimated.pop(), out.stat().st_size


def test_progress_denominator_is_exact_for_a_fresh_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estimated, actual = _progress_totals(tmp_path, monkeypatch, style="si")
    assert estimated == actual


def test_progress_denominator_accounts_for_the_skipped_block_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A DC re-run drops the existing VRM/Sink run; without that subtraction the
    # bar would stall near half. The only remaining over-estimate is the input's
    # own `.OtherCircuit` run, which cannot be measured without re-reading it.
    estimated, actual = _progress_totals(tmp_path, monkeypatch, style="dc")
    stale_other_circuits = sum(
        len(f'.OtherCircuit Device = {name} Name = "{name}"\n'.encode())
        for name in fixtures.OTHER_CIRCUIT_NAMES
    )
    assert estimated - actual == stale_other_circuits
