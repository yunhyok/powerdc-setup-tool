"""Cross-validation against staged extracts of the **real** 1.4 GB Cadence files.

Everything else in `tests/` is checked against `tests/fixtures.py`, i.e. against
our own reading of `spd_dc_format_spec.md`. This module is the only place the
code meets bytes that PowerDC/Cadence actually produced, so it is the test that
can prove the §9 templates right (or wrong):

* `dc_netlist.txt`     -- the complete real `.NetList` section (4 900 entries).
  `parse_netlist` -> `render_netlist` must be **byte-identical** (design §G.7's
  round-trip guard, at real scale).
* `dc_vrm_first.txt`   -- one complete real `.VRM` block (1.16 MB, 13 159 ground
  pins). Re-rendered from parameters/pins parsed out of the block itself,
  `pdc_gen.render_vrm` must reproduce it **byte for byte**.
* `dc_sink_first.txt`  -- the same for `.Sink` (plus trailing bytes of the next
  block, trimmed at the first `.EndSink`).
* `dc_vrm_sink_headers.txt` -- all 92 + 92 block header lines: every `Name =`
  must round-trip through `naming.vrm_name`/`sink_name`, and
  `naming.guess_voltage` must survive every real net name.
* `dc_powerdc_head.txt` -- the real `.OtherCircuit` run (10 592 lines) against
  `pdc_gen.render_other_circuits`, including its sort order and its position
  between the `* PdcElem` anchor and `.SpiceNetlist`.
* `marks_dc.txt`       -- directive offsets, used to check the section-ordering
  and block-contiguity assumptions `core/spd_scan.py` and `core/writer.py` are
  built on.

The extracts are **read-only staging, never committed** (they are slices of a
customer design), so CI never has them: the whole module skips when the
directory is absent. Point `POWERDC_REAL_EXTRACTS` at another copy to re-run it
elsewhere.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

import pytest

from powerdc_setup_tool.core import naming
from powerdc_setup_tool.core.model import SinkConfig, VrmConfig
from powerdc_setup_tool.core.netlist import parse_netlist, render_netlist
from powerdc_setup_tool.core.pdc_gen import (
    OTHER_CIRCUIT_RE,
    NegPinCache,
    estimate_block_bytes,
    render_other_circuits,
    render_sink,
    render_vrm,
)
from powerdc_setup_tool.core.spd_scan import _split_block_name

EXTRACTS = Path(
    os.environ.get("POWERDC_REAL_EXTRACTS", "/mnt/user-data/uploads/DCR/_extracts")
)

pytestmark = pytest.mark.skipif(
    not EXTRACTS.is_dir(),
    reason=f"real-data extracts not staged at {EXTRACTS} (never committed; absent in CI)",
)

# The real circuits (spec §7 / ADDENDUM): the package plus the two die sites.
REAL_CIRCUITS = ("LGA", "SITE0", "SITE1")
REAL_GROUND = "DGND"
EXPECTED_POWER_NETS = 92


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _read(name: str) -> str:
    """Read one extract as text, asserting the LF-only shape the tools assume."""
    path = EXTRACTS / name
    if not path.is_file():
        pytest.skip(f"extract {name} is not staged")
    raw = path.read_bytes()
    # design §G.6: Cadence writes LF here. If a copy ever arrives CRLF-converted
    # the byte-identity assertions below would be meaningless, so say so loudly.
    assert b"\r" not in raw, f"{name} has CRLF line endings; re-stage it verbatim"
    return raw.decode("utf-8")


def _netlist_body(text: str) -> str:
    """Strip the `.NetList`/`.EndNetList` directive lines off a full section.

    Mirrors `spd_scan`: the body is entries only, LF-normalized, newline
    terminated -- exactly what `ScanResult.netlist_body` holds.
    """
    lines = text.split("\n")
    assert lines[0] == ".NetList", f"unexpected first line {lines[0]!r}"
    end = max(i for i, line in enumerate(lines) if line == ".EndNetList")
    return "\n".join(lines[1:end]) + "\n"


_MAP_RE = re.compile(r"^\.Map CircuitName = (\S+) CircuitPinName = (\S+)$")
_NODE_RE = re.compile(r"^\.Node Name = ([^!]+)!!(\S+?)::(\S+?)( Voltage = inf)?$")
_ATTR_RE = re.compile(r"(\S+)\s*=\s*(\"[^\"]*\"|\S+)")


class _ParsedBlock:
    """A real `.VRM`/`.Sink` block taken apart into exactly what `pdc_gen` needs."""

    def __init__(self, text: str) -> None:
        lines = text.split("\n")
        self.header = lines[0]
        self.values: dict[str, str] = {}
        for key, value in _ATTR_RE.findall(self.header):
            self.values[key] = value[1:-1] if value.startswith('"') else value
        self.name = self.values.pop("Name")

        self.comp = ""
        self.sections: dict[str, list[tuple[tuple[str, str], str, bool]]] = {}
        self.section_order: list[str] = []
        section = ""
        i = 1
        while i < len(lines):
            line = lines[i]
            if line.startswith('.Pin Name = "'):
                section = line.split('"')[1]
                self.sections[section] = []
                self.section_order.append(section)
            elif line.startswith(".Map "):
                mapped = _MAP_RE.match(line)
                node = _NODE_RE.match(lines[i + 1])
                assert mapped and node, f"unparsable pin pair: {line!r} / {lines[i + 1]!r}"
                assert lines[i + 2] == ".EndMap", f"unterminated .Map at {line!r}"
                # spec §7: the `.Node` repeats the pin name -- verified, not assumed.
                assert node.group(2) == mapped.group(2)
                self.comp = self.comp or mapped.group(1)
                assert mapped.group(1) == self.comp, "block spans two circuits"
                self.sections[section].append(
                    ((mapped.group(2), node.group(1)), node.group(3), bool(node.group(4)))
                )
                i += 2
            i += 1

    def pins(self, section: str) -> tuple[tuple[str, str], ...]:
        return tuple(pin for pin, _net, _inf in self.sections[section])

    def net(self, section: str) -> str:
        return self.sections[section][0][1]

    def all_voltage_inf(self, section: str) -> bool:
        return all(inf for _pin, _net, inf in self.sections[section])

    def any_voltage_inf(self, section: str) -> bool:
        return any(inf for _pin, _net, inf in self.sections[section])


def _headers(prefix: str) -> list[str]:
    lines = _read("dc_vrm_sink_headers.txt").split("\n")
    return [line for line in lines if line.startswith(prefix)]


def _block_name(header: str) -> str:
    return header.split('Name = "')[1].rstrip('"')


def _real_power_nets() -> set[str]:
    entries = parse_netlist(_netlist_body(_read("dc_netlist.txt")))
    return {e.name for e in entries if e.group == "PowerNets"}


def _real_net_names() -> set[str]:
    entries = parse_netlist(_netlist_body(_read("dc_netlist.txt")))
    return {e.name for e in entries if e.name}


# --------------------------------------------------------------------------- #
# .NetList -- parse/render round trip at real scale (design §G.7)
# --------------------------------------------------------------------------- #


def test_real_netlist_round_trips_byte_identically() -> None:
    body = _netlist_body(_read("dc_netlist.txt"))
    entries = parse_netlist(body)

    assert len(entries) > 4000, "extract looks truncated"
    assert render_netlist(entries, {}) == body


def test_real_netlist_round_trips_with_the_unchanged_configs_applied() -> None:
    """The §8 "netlist unchanged" property: feeding back the classification the
    file already carries must still reproduce the original bytes exactly, which
    is what makes `Session.build_plan` leave `netlist_body is None`."""
    from powerdc_setup_tool.core.model import PowerNetConfig

    body = _netlist_body(_read("dc_netlist.txt"))
    entries = parse_netlist(body)
    configs = {
        e.name: PowerNetConfig(
            net=e.name,
            net_class={"PowerNets": "power", "GroundNets": "ground"}.get(
                e.group or "", "none"
            ),
        )
        for e in entries
        if e.name
    }
    assert render_netlist(entries, configs) == body


def test_real_netlist_structure_matches_the_spec() -> None:
    entries = parse_netlist(_netlist_body(_read("dc_netlist.txt")))
    groups = Counter(e.group for e in entries)

    assert groups["PowerNets"] == EXPECTED_POWER_NETS  # spec §2: "92 power members"
    assert groups["GroundNets"] == 1
    assert [e.name for e in entries if e.group == "GroundNets"] == [REAL_GROUND]

    # spec §2 / netlist.py: `-> Group` sits on each group's *first* child only.
    explicit = [(e.name, e.group) for e in entries if e.group_explicit]
    assert len(explicit) == 2
    assert {group for _name, group in explicit} == {"PowerNets", "GroundNets"}

    # spec §2: ground members carry `Voltage = 0`, power members carry none.
    ground = next(e for e in entries if e.group == "GroundNets")
    assert ("Voltage", "0") in ground.attrs
    assert not [
        e for e in entries if e.group == "PowerNets" and any(k == "Voltage" for k, _ in e.attrs)
    ]

    # Both group nodes exist as plain L1 entries (so `emit_groups` never fires).
    assert {"PowerNets", "GroundNets"} <= {e.name for e in entries}


# --------------------------------------------------------------------------- #
# .VRM / .Sink -- the §9 templates against Cadence's own bytes
# --------------------------------------------------------------------------- #


def test_real_vrm_block_is_reproduced_byte_for_byte() -> None:
    text = _read("dc_vrm_first.txt")
    assert text.endswith(".EndVRM\n")
    block = _ParsedBlock(text)

    # spec §4: four `.Pin` sections, both sense sections empty.
    assert block.section_order == [
        "Positive Pin",
        "Negative Pin",
        "Positive Sense Pin",
        "Negative Sense Pin",
    ]
    assert block.sections["Positive Sense Pin"] == []
    assert block.sections["Negative Sense Pin"] == []
    # spec §4: no `Voltage = inf` anywhere in a VRM (that is the Sink's marker).
    assert not block.any_voltage_inf("Positive Pin")
    assert not block.any_voltage_inf("Negative Pin")

    cfg = VrmConfig(
        net=block.net("Positive Pin"),
        gnet=block.net("Negative Pin"),
        comp=block.comp,
        nominal_voltage=float(block.values["NominalVoltage"]),
        sense_voltage=float(block.values["SenseVoltage"]),
        output_current=float(block.values["OutputCurrent"]),
    )
    pos = block.pins("Positive Pin")
    neg = block.pins("Negative Pin")

    assert render_vrm(cfg, pos, neg) == text
    # the cached negative-pin path must produce the same bytes (design §D)
    assert render_vrm(cfg, pos, neg, cache=NegPinCache()) == text
    # and the progress estimator must be exact, not approximate
    assert estimate_block_bytes(cfg, pos, neg) == len(text.encode("utf-8"))

    assert naming.vrm_name(cfg.comp, cfg.net, cfg.gnet) == block.name
    assert cfg.comp == "LGA" and cfg.gnet == REAL_GROUND
    assert len(neg) > 10_000, "the real negative-pin blob is the big one"


def test_real_sink_block_is_reproduced_byte_for_byte() -> None:
    raw = _read("dc_sink_first.txt")
    terminator = "\n.EndSink\n"
    cut = raw.index(terminator) + len(terminator)
    text = raw[:cut]
    assert raw[cut:].startswith(".Sink "), "the trailing bytes should be the next block"

    block = _ParsedBlock(text)
    assert block.section_order == ["Positive Pin", "Negative Pin"]
    # spec §5 / ambiguity A4: *every* Sink `.Node` line carries ` Voltage = inf`.
    assert block.all_voltage_inf("Positive Pin")
    assert block.all_voltage_inf("Negative Pin")
    # spec §5 / A5: the empty `.SinkCurrentSource` pair is always present.
    assert "\n.SinkCurrentSource\n.EndSinkCurrentSource\n.EndSink\n" in text

    cfg = SinkConfig(
        net=block.net("Positive Pin"),
        gnet=block.net("Negative Pin"),
        comp=block.comp,
        nominal_voltage=float(block.values["NominalVoltage"]),
        current=float(block.values["Current"]),
        model=int(block.values["Model"]),
        pf_mode=int(block.values["PFMode"]),
        pin_equal_current=int(block.values["PinEqualCurrent"]),
    )
    pos = block.pins("Positive Pin")
    neg = block.pins("Negative Pin")

    assert render_sink(cfg, pos, neg) == text
    assert render_sink(cfg, pos, neg, cache=NegPinCache()) == text
    assert estimate_block_bytes(cfg, pos, neg) == len(text.encode("utf-8"))

    assert naming.sink_name(cfg.comp, cfg.net, cfg.gnet) == block.name
    # spec §6 die rule: the Sink of a `/1` net sits on SITE1.
    assert cfg.comp == naming.sink_component(naming.die_of(cfg.net))
    # spec §A3 defaults, confirmed by the converter's own output
    assert (cfg.model, cfg.pf_mode, cfg.pin_equal_current) == (2, 2, 1)


def test_real_block_defaults_match_the_generator_defaults() -> None:
    """The converter's own numbers for an unedited block: 1 V / 1 V / 1 A."""
    vrm = _ParsedBlock(_read("dc_vrm_first.txt"))
    assert vrm.values == {
        "NominalVoltage": "1",
        "SenseVoltage": "1",
        "OutputCurrent": "1",
    }
    sink_raw = _read("dc_sink_first.txt")
    sink = _ParsedBlock(sink_raw[: sink_raw.index("\n.EndSink\n") + len("\n.EndSink\n")])
    assert sink.values == {
        "NominalVoltage": "1",
        "Current": "1",
        "Model": "2",
        "PFMode": "2",
        "PinEqualCurrent": "1",
    }


# --------------------------------------------------------------------------- #
# block naming across all 92 + 92 blocks (spec §6)
# --------------------------------------------------------------------------- #


def test_every_real_block_name_round_trips_through_naming() -> None:
    power = _real_power_nets()
    known_nets = _real_net_names()
    circuits = dict.fromkeys(REAL_CIRCUITS, 0)

    vrms = _headers(".VRM ")
    sinks = _headers(".Sink ")
    assert len(vrms) == EXPECTED_POWER_NETS
    assert len(sinks) == EXPECTED_POWER_NETS

    comps: Counter[str] = Counter()
    grounds: Counter[str] = Counter()
    seen_pnets: set[str] = set()
    for header, prefix, build in (
        *((h, "VRM_", naming.vrm_name) for h in vrms),
        *((h, "SINK_", naming.sink_name) for h in sinks),
    ):
        name = _block_name(header)
        comp, pnet, gnet = _split_block_name(name, prefix, circuits, known_nets)
        assert build(comp, pnet, gnet) == name, f"{name!r} does not round-trip"
        assert pnet in power, f"{pnet!r} is not a PowerNets member"
        comps[comp] += 1
        grounds[gnet] += 1
        seen_pnets.add(pnet)

    assert seen_pnets == power  # every power net gets exactly one VRM and one Sink
    assert grounds == Counter({REAL_GROUND: 2 * EXPECTED_POWER_NETS})
    assert comps == Counter({"LGA": 92, "SITE0": 46, "SITE1": 46})


def test_every_real_sink_component_follows_the_die_rule() -> None:
    """spec §6: `SINK_SITE{die}_...` where `{die}` is the net's `/0`/`/1` suffix."""
    known_nets = _real_net_names()
    circuits = dict.fromkeys(REAL_CIRCUITS, 0)
    for header in _headers(".Sink "):
        comp, pnet, _gnet = _split_block_name(_block_name(header), "SINK_", circuits, known_nets)
        assert comp == naming.sink_component(naming.die_of(pnet))


# --------------------------------------------------------------------------- #
# naming.guess_voltage over the real net names (spec §6)
# --------------------------------------------------------------------------- #


def test_guess_voltage_survives_every_real_net_name() -> None:
    """No exception on any of the ~4 900 real names, and no silly value."""
    for net in _real_net_names():
        value = naming.guess_voltage(net)
        assert value is None or 0.0 < value < 10.0, f"{net!r} -> {value!r}"
        naming.die_of(net)  # must not raise either


def test_guess_voltage_resolves_every_real_power_net() -> None:
    power = _real_power_nets()
    voltages = {net: naming.guess_voltage(net) for net in power}
    unresolved = sorted(net for net, value in voltages.items() if value is None)
    assert not unresolved, f"power nets with no ADC_VDD_<ccc> code: {unresolved}"

    # spec §6 observed code set -- nothing outside it, and every one of them used.
    assert set(voltages.values()) == {0.5, 0.55, 0.7, 0.75, 1.05, 1.2, 1.8}
    # the named case from the brief
    hsio = {net: v for net, v in voltages.items() if net.startswith("ADC_VDD_070_VP_HSIO")}
    assert hsio and set(hsio.values()) == {0.7}


def test_every_real_power_net_carries_a_die_suffix() -> None:
    dies = Counter(naming.die_of(net) for net in _real_power_nets())
    assert set(dies) == {0, 1}
    assert dies[0] == dies[1] == EXPECTED_POWER_NETS // 2


# --------------------------------------------------------------------------- #
# .OtherCircuit run (design §A2/§D step 3)
# --------------------------------------------------------------------------- #


def test_real_other_circuit_run_matches_the_generator() -> None:
    lines = _read("dc_powerdc_head.txt").split("\n")
    run = [line for line in lines if line.startswith(".OtherCircuit ")]
    assert len(run) > 10_000, "extract looks truncated"

    names = [re.match(r"\.OtherCircuit Device = (\S+) ", line).group(1) for line in run]
    assert all(OTHER_CIRCUIT_RE.match(name) for name in names)
    # Text *and* sort order (design §D step 3: "sorted by (numeric id, die)").
    assert render_other_circuits(names) == "\n".join(run) + "\n"

    # design §D steps 2-4: the run sits between anchor A and `.SpiceNetlist`.
    anchor = lines.index("* PdcElem description lines")
    assert lines[anchor + 1] == run[0]
    assert lines[lines.index(run[-1]) + 1].startswith(".SpiceNetlist ")


# --------------------------------------------------------------------------- #
# marks_dc.txt -- the layout `spd_scan`/`writer` are built on
# --------------------------------------------------------------------------- #


def _marks() -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in _read("marks_dc.txt").split("\n"):
        if not line.strip():
            continue
        offset, _, token = line.partition(":")
        rows.append((int(offset), token.strip()))
    return rows


def test_marks_confirm_the_section_order_the_scanner_assumes() -> None:
    rows = _marks()
    first = {token: offset for offset, token in reversed(rows)}
    counts = Counter(token for _offset, token in rows)

    # one and only one of each structural directive (spec §1)
    for token in (".PowerSI", ".EndPowerSI", ".PowerDC", ".EndPowerDC", ".SpiceNetlist"):
        assert counts[token] == 1, token
    for token in (".NetList", ".EndNetList", ".NetAlias", ".EndNetAlias"):
        assert counts[token] == 1, token

    # `scan_spd` matches its anchors only inside [.PowerDC, .EndPowerDC) and
    # reads the `.NetList` afterwards; `write_spd` splices in that same order.
    assert (
        first[".PowerSI"]
        < first[".EndPowerSI"]
        < first[".PowerDC"]
        < first[".SpiceNetlist"]
        < first[".VRM"]
        < first[".Sink"]
        < first[".EndPowerDC"]
        < first[".NetList"]
        < first[".EndNetList"]
        < first[".NetAlias"]
    )
    # design §G.1: the netlist is *outside* the PowerDC section, so rewriting it
    # is an independent splice step (design §D steps 7-9).
    assert first[".NetList"] > first[".EndPowerDC"]


def test_marks_confirm_the_block_run_is_contiguous() -> None:
    """design §D step 5 / spec §3: no blank line between blocks -- byte-verified."""
    rows = _marks()
    vrm = [o for o, t in rows if t == ".VRM"]
    end_vrm = [o for o, t in rows if t == ".EndVRM"]
    sink = [o for o, t in rows if t == ".Sink"]
    end_sink = [o for o, t in rows if t == ".EndSink"]

    assert len(vrm) == len(end_vrm) == EXPECTED_POWER_NETS
    assert len(sink) == len(end_sink) == EXPECTED_POWER_NETS

    # `.EndVRM\n` is 8 bytes, `.EndSink\n` is 9: consecutive blocks touch exactly.
    assert {b - a for a, b in zip(end_vrm, vrm[1:])} == {len(".EndVRM\n")}
    assert {b - a for a, b in zip(end_sink, sink[1:])} == {len(".EndSink\n")}
    # ... and the VRM run runs straight into the Sink run.
    assert sink[0] - end_vrm[-1] == len(".EndVRM\n")
    # every VRM precedes every Sink (design §D step 5 emits VRMs first)
    assert end_vrm[-1] < sink[0]


def test_marks_confirm_the_spice_anchor_abuts_the_first_block() -> None:
    """`anchor_spice_end` is where `write_spd` starts emitting blocks: in the
    real DC file the first `.VRM` begins immediately after `.EndSpiceNetlist`."""
    rows = _marks()
    first = {token: offset for offset, token in reversed(rows)}
    spice_line = '.SpiceNetlist Name = "SPICENetlist"\n'
    end_spice_line = ".EndSpiceNetlist\n"
    assert first[".VRM"] - first[".SpiceNetlist"] == len(spice_line) + len(end_spice_line)


def test_real_block_counts_agree_across_every_extract() -> None:
    """92 power nets -> 92 VRM + 92 Sink blocks -> 184 headers. One number."""
    counts = Counter(token for _offset, token in _marks())
    assert counts[".VRM"] == counts[".Sink"] == EXPECTED_POWER_NETS
    assert counts[".SinkCurrentSource"] == EXPECTED_POWER_NETS
    assert len(_real_power_nets()) == EXPECTED_POWER_NETS
    assert len(_headers(".VRM ")) + len(_headers(".Sink ")) == 2 * EXPECTED_POWER_NETS
