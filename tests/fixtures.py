"""Miniature `.spd` fixture builder -- owned by chunk 0 (design §E).

``build_mini_spd`` is the contract chunks 1-3 test against: it must be a
complete, grammar-faithful miniature SPD file, authoritative grammar =
``spd_dc_format_spec.md`` **including the final ADDENDUM** (pin-map blocks
are ``.Connect <RefDes> <PartName> Usage = ... Checked = ...`` / ``.EndC``,
*not* the older guessed ``.C <name> ... .EndC`` shape design.md §E describes
-- the ADDENDUM is device-verified and supersedes that guess).

Pure stdlib, no dependency on ``powerdc_setup_tool`` itself, so this module
runs standalone (``python3 tests/fixtures.py``) without ``PYTHONPATH=src``.

``expected_dc`` is intentionally a thin NotImplementedError stub (see its
docstring) -- golden-output rendering is chunk 2's job once
``core/pdc_gen.py`` and ``core/writer.py`` are real.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

Style = Literal["si", "dc"]
Newline = Literal["lf", "crlf"]

# ---------------------------------------------------------------------------
# Fixture content constants -- exposed so chunk 1-3 tests can assert against
# known values instead of re-deriving them from generated text.
# ---------------------------------------------------------------------------

WORKFLOW_KEY_SI = "0x100000067"
WORKFLOW_KEY_DC = "0x1000000067"

POWER_NET_A = "ADC_VDD_070_VDDA/0"  # 0.70 V, die 0 -- naming.guess_voltage() case
POWER_NET_B = "ADC_VDD_120_VDDB/1"  # 1.20 V, die 1
GROUND_NET = "DGND"
UNCLASSIFIED_NETS: tuple[str, ...] = ("SIG_CLK_IN", "SIG_RESET_N")
# Trailing, out-of-order L1 sense nets (spec §2: "184 trailing L1 sense nets").
SENSE_NETS: tuple[str, ...] = (
    "ADC_VDD_070_VDDA_PS/0",
    "ADC_VDD_070_VDDA_GS/0",
    "ADC_VDD_120_VDDB_PS/1",
    "ADC_VDD_120_VDDB_GS/1",
)

VRM_COMP = "LGA"
SINK_COMP_DIE0 = "SITE0"
SINK_COMP_DIE1 = "SITE1"

# spec §8 row 2 / §9: `.OtherCircuit` emitted for caps matching this pattern.
_OTHER_CIRCUIT_RE = re.compile(r"^C\d+_[01]$")

# One entry per `.Connect` block, in file order: small parts/marks first,
# the large multi-pin circuits last (spec ADDENDUM: "Large circuits (LGA
# ~5.5k pins, SITE0/SITE1) sit at the END of the .Connect run, small ones
# (caps C{n}_{die}, alignment marks) first"). Each pin tuple is
# `(pin_name, node_id, net_or_None)`; `net is None` means the pin line omits
# the `::net` suffix entirely -- an "unconnected" pin per the ADDENDUM,
# which harvesters must skip.
_CONNECT_BLOCKS: tuple[tuple[str, str, tuple[tuple[str, str, str | None], ...]], ...] = (
    ("ALI1", "1998ARROW", (("1", "Node2521457", None),)),
    ("C1_0", "CAP_0402_10UF", (("1", "Node4001", POWER_NET_A), ("2", "Node4002", GROUND_NET))),
    ("C1_1", "CAP_0402_10UF", (("1", "Node5001", POWER_NET_B), ("2", "Node5002", GROUND_NET))),
    (
        "SITE0",
        "DIE_SITE",
        (
            ("1", "Node2001", POWER_NET_A),
            ("2", "Node2002", POWER_NET_A),
            ("3", "Node2003", GROUND_NET),
            ("4", "Node2004", GROUND_NET),
        ),
    ),
    (
        "SITE1",
        "DIE_SITE",
        (
            ("1", "Node3001", POWER_NET_B),
            ("2", "Node3002", POWER_NET_B),
            ("3", "Node3003", GROUND_NET),
            ("4", "Node3004", GROUND_NET),
        ),
    ),
    (
        "LGA",
        "LGA_PKG",
        (
            ("1", "Node1001", POWER_NET_A),
            ("2", "Node1002", POWER_NET_A),
            ("3", "Node1003", POWER_NET_A),
            ("4", "Node1004", POWER_NET_B),
            ("5", "Node1005", POWER_NET_B),
            ("6", "Node1006", POWER_NET_B),
            ("7", "Node1007", GROUND_NET),
            ("8", "Node1008", GROUND_NET),
        ),
    ),
)

CIRCUITS: tuple[str, ...] = tuple(refdes for refdes, _part, _pins in _CONNECT_BLOCKS)
OTHER_CIRCUIT_NAMES: tuple[str, ...] = tuple(
    refdes for refdes, _part, _pins in _CONNECT_BLOCKS if _OTHER_CIRCUIT_RE.match(refdes)
)

_PARTS: tuple[tuple[str, str], ...] = (
    ("LGA_PKG", "IC"),
    ("DIE_SITE", "IC"),
    ("CAP_0402_10UF", "DISCRETE"),
    ("1998ARROW", "IO"),
)
# (refdes, x_mm, y_mm) -- one per `.Connect` refdes, same file order.
_COMPONENTS: tuple[tuple[str, int, int], ...] = (
    ("ALI1", 0, 5),
    ("C1_0", 1, 1),
    ("C1_1", 2, 1),
    ("SITE0", 5, 0),
    ("SITE1", 10, 0),
    ("LGA", 0, 0),
)

_CONSTRAINT_NAMES: tuple[str, ...] = (
    "ConstraintDisc",
    "ConstraintVia",
    "ConstraintPad",
    "ConstraintSizePlating",
    "ConstraintMetal",
    "ConstraintMetalArea",
    "ConstraintTrace",
    "ConstraintLayer",
    "ConstraintLayerArea",
    "ConstraintMetalLayerThick",
    "VRMGroup",
)


def _pins_of(circuit: str, net: str) -> tuple[tuple[str, str], ...]:
    """(pin, node) pairs for *net* on *circuit*, derived from `_CONNECT_BLOCKS`."""
    for refdes, _part, pins in _CONNECT_BLOCKS:
        if refdes == circuit:
            return tuple((pin, node) for pin, node, n in pins if n == net)
    return ()


def _fmt_num(value: float) -> str:
    """Minimal numeric rendering matching observed spec style (``1``, ``0.7``)."""
    return f"{value:g}"


# ---------------------------------------------------------------------------
# Section builders. Each returns text with no leading blank line and exactly
# one trailing "\n"; the assembler in `_render` controls inter-section
# spacing. All internal line joins use "\n" only -- `build_mini_spd` does a
# single global "\n" -> "\r\n" pass at the very end for newline="crlf".
# ---------------------------------------------------------------------------


def _title_line(style: Style) -> str:
    key = WORKFLOW_KEY_DC if style == "dc" else WORKFLOW_KEY_SI
    return f"Title WorkflowKey = {key} - LayoutWorkbench file for version 2000.20\n"


def _cte_decoy() -> str:
    """A `.CTE ... .EndCTE` thermal block, positioned before the `.Connect`
    run (spec ADDENDUM: `.EndCTE` is an unrelated directive that a scanner
    doing a sloppy prefix-match on `.EndC` must NOT confuse with the real
    `.Connect` terminator `.EndC`).
    """
    return '.CTE Name = "ThermalDecoy"\n1 SomeField = 1\n.EndCTE\n'


def _connect_section() -> str:
    """The `.Connect ... .EndC` pin-map run (spec ADDENDUM)."""
    blocks: list[str] = []
    for refdes, part, pins in _CONNECT_BLOCKS:
        lines = [f".Connect {refdes} {part} Usage = 0b1000001000 Checked = 1"]
        for pin, node, net in pins:
            if net is None:
                lines.append(f"{pin} $Package.{node}!!{pin}")
            else:
                lines.append(f"{pin} $Package.{node}!!{pin}::{net}")
        lines.append(".EndC")
        blocks.append("\n".join(lines) + "\n")
    # blank line between blocks, matching the ADDENDUM's verbatim sample
    return "\n".join(blocks)


def _comp_collection_section() -> str:
    lines = [".CompCollection"]
    for name, tags in _PARTS:
        lines.append(f'.Part {name} Tags = "{tags}"')
    for name, x, y in _COMPONENTS:
        lines.append(f".Component {name} {x}mm {y}mm StartLayer = 1 AttachLayer = 1")
        lines.append('+            PropertyType = "0, MoldingCompound, 0, 0, MoldingCompound, 0"')
    lines.append(".EndCompCollection")
    return "\n".join(lines) + "\n"


def _powersi_section() -> str:
    return (
        ".PowerSI\n"
        ".SimuOptionSettings DielectricBufferSize = 0.140604\n"
        ".EndSimuOptionSettings\n"
        ".EndPowerSI\n"
    )


def _vrm_block(pnet: str, gnet: str, comp: str, v_nom: float) -> str:
    """spec §9 `.VRM` template, verbatim shape."""
    pos = _pins_of(comp, pnet)
    neg = _pins_of(comp, gnet)
    v = _fmt_num(v_nom)
    lines = [
        f'.VRM NominalVoltage = {v} SenseVoltage = {v} OutputCurrent = 1 '
        f'Name = "VRM_{comp}_{pnet}_{gnet}"',
        '.Pin Name = "Positive Pin"',
    ]
    for pin, node in pos:
        lines += [
            f".Map CircuitName = {comp} CircuitPinName = {pin}",
            f".Node Name = {node}!!{pin}::{pnet}",
            ".EndMap",
        ]
    lines.append(".EndPin")
    lines.append('.Pin Name = "Negative Pin"')
    for pin, node in neg:
        lines += [
            f".Map CircuitName = {comp} CircuitPinName = {pin}",
            f".Node Name = {node}!!{pin}::{gnet}",
            ".EndMap",
        ]
    lines += [
        ".EndPin",
        '.Pin Name = "Positive Sense Pin"',
        ".EndPin",
        '.Pin Name = "Negative Sense Pin"',
        ".EndPin",
        ".EndVRM",
    ]
    return "\n".join(lines)


def _sink_block(pnet: str, gnet: str, comp: str, v_nom: float) -> str:
    """spec §9 `.Sink` template, verbatim shape."""
    pos = _pins_of(comp, pnet)
    neg = _pins_of(comp, gnet)
    lines = [
        f'.Sink NominalVoltage = {_fmt_num(v_nom)} Current = 1 Model = 2 PFMode = 2 '
        f'PinEqualCurrent = 1 Name = "SINK_{comp}_{pnet}_{gnet}"',
        '.Pin Name = "Positive Pin"',
    ]
    for pin, node in pos:
        lines += [
            f".Map CircuitName = {comp} CircuitPinName = {pin}",
            f".Node Name = {node}!!{pin}::{pnet} Voltage = inf",
            ".EndMap",
        ]
    lines.append(".EndPin")
    lines.append('.Pin Name = "Negative Pin"')
    for pin, node in neg:
        lines += [
            f".Map CircuitName = {comp} CircuitPinName = {pin}",
            f".Node Name = {node}!!{pin}::{gnet} Voltage = inf",
            ".EndMap",
        ]
    lines += [".EndPin", ".SinkCurrentSource", ".EndSinkCurrentSource", ".EndSink"]
    return "\n".join(lines)


def _powerdc_section(style: Style) -> str:
    """`.PowerDC ... .EndPowerDC` (spec §3): comment placeholders, the
    `* PdcElem description lines` / `.EndSpiceNetlist` anchor pair, and
    (style="dc" only) `.OtherCircuit` lines + 2 VRM + 2 Sink blocks,
    contiguous with no blank lines between them (spec §3 contiguity).
    """
    lines = [
        "* PowerDC Setup description lines",
        ".PowerDC PlotResolution = 0.000497 MeshX = 200 MeshY = 200 SimulationType = 1",
        ".TreatPadAsShape Type = 2 ConnectObject = 1 CheckIgnorePinSize = 1 "
        "NodeGapForPad = 5.08e-05 MinNodeNumForPad = 6",
        ".SignOffReportSetting VerticalRangeScale = 85",
        ".ExportDistribution2TxtSetting AllInOneLayer = 0",
        "",
        "* PowerLoss description lines",
        "",
        "* PdcElem description lines",  # anchor A: anchor_pdc_elem_end
    ]
    if style == "dc":
        for name in OTHER_CIRCUIT_NAMES:
            lines.append(f'.OtherCircuit Device = {name} Name = "{name}"')
    lines.append('.SpiceNetlist Name = "SPICENetlist"')
    lines.append(".EndSpiceNetlist")  # anchor B: anchor_spice_end
    if style == "dc":
        lines.append(_vrm_block(POWER_NET_A, GROUND_NET, VRM_COMP, 0.7))
        lines.append(_vrm_block(POWER_NET_B, GROUND_NET, VRM_COMP, 1.2))
        lines.append(_sink_block(POWER_NET_A, GROUND_NET, SINK_COMP_DIE0, 0.7))
        lines.append(_sink_block(POWER_NET_B, GROUND_NET, SINK_COMP_DIE1, 1.2))
    lines.append("")
    for name in _CONSTRAINT_NAMES:
        lines.append(f"* {name} description lines")
    lines.append(".EndPowerDC")
    return "\n".join(lines) + "\n"


def _autoclassify_section() -> str:
    """`.Celsius`/`.OptimizePI`/`.AutoClassifyType` x2/`.CurveProperty` x3/
    `.PostLayoutSetup` (+ `.WhatIfSetup` pair) -- spec §1, unchanged list.
    """
    return (
        ".Celsius\n"
        ".EndCelsius\n"
        ".OptimizePI\n"
        ".EndOptimizePI\n"
        '.AutoClassifyType Name = "Distance to Power Pin" Type = 1 Distance = 1.000000e+01\n'
        '.AutoClassifyType Name = "Setback Distance" Type = 2 Distance = 5.000000e+00\n'
        '.CurveProperty Name = "Curve1"\n'
        ".EndCurveProperty\n"
        '.CurveProperty Name = "Curve2"\n'
        ".EndCurveProperty\n"
        '.CurveProperty Name = "Curve3"\n'
        ".EndCurveProperty\n"
        ".PostLayoutSetup\n"
        ".WhatIfSetup Name = WhatIfDefault\n"
        ".WhatIfSetup Name = WhatIfDefault\n"
        ".EndPostLayoutSetup\n"
    )


def _netlist_entries() -> list[str]:
    """The `.NetList` body: BFS-serialized 2-level tree (spec §2).

    L1 root + L1 plain/group entries (ASCII-sorted, group nodes included in
    that sort) + L2 members (grouped by parent, `->` on each group's first
    child only) + trailing out-of-order L1 sense nets. Per spec §2 ("The 92
    power members carry no Voltage ="), only the ground member gets
    `Voltage = 0`; power members do not.
    """
    return [
        "\t::Unselected||DropShape RiseTime = 0ps %Coupling = 0",
        "\tGroundNets Color = LIME",
        "\tPowerNets Color = RED",
        f"\t{UNCLASSIFIED_NETS[0]}::Unselected||DropShape Color = GREEN",
        f"\t{UNCLASSIFIED_NETS[1]}::Unselected||DropShape Color = YELLOW",
        f"\t{GROUND_NET} -> GroundNets Color = GREEN Voltage = 0",
        f"\t{POWER_NET_A} -> PowerNets Color = RED",
        f"\t{POWER_NET_B} Color = OLIVE",
        f"\t{SENSE_NETS[0]}::Unselected||DropShape Color = DARKCYAN",
        f"\t{SENSE_NETS[1]}::Unselected||DropShape Color = DARKBLUE",
        f"\t{SENSE_NETS[2]}::Unselected||DropShape Color = BLUE",
        f"\t{SENSE_NETS[3]}::Unselected||DropShape Color = FUCHSIA",
    ]


def _netlist_section() -> str:
    lines = [".NetList", *_netlist_entries(), ".EndNetList"]
    return "\n".join(lines) + "\n"


def _netalias_section() -> str:
    """`.NetAlias` block. Spec documents this section only as "byte-identical
    [between SI/DC]" and gives no entry grammar sample; this is a minimal,
    structurally-plausible placeholder (alias -> canonical net).
    """
    return ".NetAlias\n\tAGND -> DGND\n.EndNetAlias\n"


def _tail() -> str:
    return "* Design rules/Clearances description lines\n.End\n"


def _render(style: Style) -> str:
    sections = [
        _title_line(style),
        "* Geometry description lines\n",
        _cte_decoy(),
        "\n",
        _connect_section(),
        "\n",
        _comp_collection_section(),
        _powersi_section(),
        _powerdc_section(style),
        _autoclassify_section(),
        _netlist_section(),
        _netalias_section(),
        _tail(),
    ]
    return "".join(sections)


def build_mini_spd(
    path: str | Path,
    *,
    style: Style = "si",
    newline: Newline = "lf",
    **opts: object,
) -> None:
    """Write a complete miniature `.spd` file to *path* exercising the real
    grammar (design §E; spec, including the `.Connect`-block ADDENDUM).

    ``style="si"`` (default) omits `.OtherCircuit`/`.VRM`/`.Sink`; ``"dc"``
    emits them plus the patched `WorkflowKey`. ``newline="crlf"`` emits CRLF
    throughout (a single global substitution applied after assembly, so every
    line ending -- including inside otherwise-"copied" regions -- flips
    consistently). ``**opts`` is accepted and currently ignored: reserved so
    future chunks (e.g. `test_perf`'s larger synthetic files) can extend this
    signature without breaking callers of the fixed-size miniature shape.
    """
    if style not in ("si", "dc"):
        raise ValueError(f"style must be 'si' or 'dc', got {style!r}")
    if newline not in ("lf", "crlf"):
        raise ValueError(f"newline must be 'lf' or 'crlf', got {newline!r}")
    del opts

    content = _render(style)
    if newline == "crlf":
        content = content.replace("\n", "\r\n")
    Path(path).write_bytes(content.encode("utf-8"))


def expected_dc(*args: object, **kwargs: object) -> str:
    """Golden DC-converted text for a `style="si"` `build_mini_spd` fixture.

    TODO(chunk 2): implement once `core/pdc_gen.py` and `core/writer.py` are
    real. Should render the byte-exact expected output of running
    `writer.write_spd` against a `style="si"` fixture + a resolved config
    (design §D splice plan / spec §9 pass 2), so `test_writer.py` can assert
    ``write_spd(...)`` output ``== expected_dc(...)`` (design §E `test_writer`).
    Signature deliberately left loose (`*args`/`**kwargs`) -- chunk 2 owns
    deciding exactly what inputs it needs (fixture path/style, a `Session` or
    `WritePlan`, `newline`, ...).
    """
    raise NotImplementedError(
        "expected_dc is implemented in chunk 2 (core/pdc_gen.py + core/writer.py)"
    )


if __name__ == "__main__":
    tmp_dir = Path("/tmp")
    for _style in ("si", "dc"):
        out = tmp_dir / f"mini_{_style}.spd"
        build_mini_spd(out, style=_style)  # type: ignore[arg-type]
        print(f"{_style}: {out} ({out.stat().st_size:,} bytes)")
