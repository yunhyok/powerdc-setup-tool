"""Net-name inference and derived-block naming -- owned by chunk 1 (design §A; spec §6).

Pure string/number logic, no I/O, no Qt.
"""

from __future__ import annotations


def guess_voltage(net: str) -> float | None:
    """`ADC_VDD_<ccc>_...` -> `ccc / 100` volts (spec §6); `None` if unmatched."""
    raise NotImplementedError


def die_of(net: str) -> int | None:
    """Trailing `/0` or `/1` on *net* -> die index; `None` if absent."""
    raise NotImplementedError


def vrm_name(comp: str, pnet: str, gnet: str) -> str:
    """`VRM_{comp}_{pnet}_{gnet}` (spec §6)."""
    raise NotImplementedError


def sink_name(comp: str, pnet: str, gnet: str) -> str:
    """`SINK_{comp}_{pnet}_{gnet}` (spec §6)."""
    raise NotImplementedError


def sink_component(die: int | None) -> str:
    """`SITE{die}` when that circuit exists (spec §6; design §G.3 fallback rule)."""
    raise NotImplementedError


def color_for(i: int) -> str:
    """Cycle the 11-name palette (spec §2) by index."""
    raise NotImplementedError
