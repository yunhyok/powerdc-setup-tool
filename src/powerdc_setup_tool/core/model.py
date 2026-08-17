"""Data model — design.md §B, transcribed verbatim.

This module is the contract every other chunk codes against. It has zero Qt
imports and zero I/O: pure dataclasses plus the small amount of logic that
belongs to :class:`PinMapIndex` (declared in §B but not spelled out there).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NetEntry:
    """One parsed ``.NetList`` line."""

    name: str
    group: str | None  # None, or "PowerNets" / "GroundNets"
    group_explicit: bool  # had " -> Group" on this line
    sel_state: str | None  # "Unselected" or None
    view_mode: str | None  # "DropShape" or None
    attrs: tuple[tuple[str, str], ...]  # ordered ("Color", "RED"), ("Voltage", "0")...
    raw: str  # verbatim line, no trailing "\n"
    index: int  # position in body (entry ordinal, not byte offset)


@dataclass(frozen=True)
class CircuitInfo:
    """One ``.Connect`` circuit (see spec ADDENDUM)."""

    name: str
    pin_count: int


@dataclass(frozen=True)
class PinMapIndex:
    """circuit -> net -> ((pin, node), ...)."""

    by_circuit: dict[str, dict[str, tuple[tuple[str, str], ...]]]

    def pins(self, circuit: str, net: str) -> tuple[tuple[str, str], ...]:
        """Return the (pin, node) pairs for *net* on *circuit* (empty if none)."""
        return self.by_circuit.get(circuit, {}).get(net, ())

    def circuits_for(self, net: str) -> list[str]:
        """Circuits carrying *net*, sorted by pin count desc (tie -> ASCII-first)."""
        candidates = [
            (circuit, len(per_net[net]))
            for circuit, per_net in self.by_circuit.items()
            if net in per_net
        ]
        candidates.sort(key=lambda item: (-item[1], item[0]))
        return [circuit for circuit, _count in candidates]


@dataclass
class PowerNetConfig:
    net: str
    net_class: str  # "power" | "ground" | "none"
    paired_gnd: str = ""
    voltage: float = 1.0
    voltage_override: bool = False  # True once user edits the cell
    selected: bool = True
    die: int | None = None
    from_input: bool = False


@dataclass
class VrmConfig:
    net: str
    gnet: str
    comp: str = "LGA"
    enabled: bool = True
    nominal_voltage: float = 1.0
    nominal_override: bool = False
    sense_voltage: float = 1.0
    sense_override: bool = False
    output_current: float = 1.0
    current_override: bool = False
    row_id: int = 0  # allows 2nd VRM for same net


@dataclass
class SinkConfig:
    net: str
    gnet: str
    comp: str
    enabled: bool = True
    nominal_voltage: float = 1.0
    nominal_override: bool = False
    current: float = 1.0
    current_override: bool = False
    model: int = 2
    pf_mode: int = 2
    pin_equal_current: int = 1
    row_id: int = 0


@dataclass(frozen=True)
class ExistingBlock:
    kind: str
    name: str
    net: str
    gnet: str
    comp: str
    values: dict[str, str]


@dataclass(frozen=True)
class ScanResult:
    path: Path
    file_size: int
    elapsed_s: float
    workflow_key_span: tuple[int, int] | None  # byte span of the hex token on line 1
    powerdc_span: tuple[int, int]  # (.PowerDC line start, .EndPowerDC line start)
    anchor_pdc_elem_end: int  # A: end of "* PdcElem description lines\n"
    anchor_spice_end: int  # B: end of ".EndSpiceNetlist\n"
    existing_blocks_span: tuple[int, int] | None  # [B, end of last .EndSink) if input already DC
    netlist_span: tuple[int, int]  # (.NetList line start, after .EndNetList line)
    netlist_body_span: tuple[int, int]  # entries only, excludes both directive lines
    netlist_body: str  # ~300 KB kept in RAM
    nets: tuple[NetEntry, ...]
    pin_maps: PinMapIndex
    circuits: tuple[CircuitInfo, ...]
    other_circuit_names: tuple[str, ...]
    existing_vrms: tuple[ExistingBlock, ...]
    existing_sinks: tuple[ExistingBlock, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ValidationIssue:
    """One result row from ``Session.validate()``.

    §B's code block does not spell out this dataclass's fields; shape below is
    inferred from its call sites: §C's export dialog ("blocking issues ... and
    warnings shown in one dialog with a per-issue 'uncheck offending rows'
    button") and §G.3/§G.4 ("duplicate derived names ... validate() flags exact
    collisions as blocking", "0-pin nets ... blocking ValidationIssue"). Chunk 3
    (session.py) owns the actual validation rules; this is just the carrier.
    """

    blocking: bool  # True = blocks export, False = warning only
    message: str
    nets: tuple[str, ...] = ()  # nets implicated, for "uncheck offending rows"
