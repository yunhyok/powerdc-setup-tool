"""Net-name inference and derived-block naming -- design §A; spec §6.

Pure string/number logic, no I/O, no Qt.
"""

from __future__ import annotations

import re

# spec §2: "Colors cycle over 11 names".
COLOR_CYCLE: tuple[str, ...] = (
    "RED",
    "GREEN",
    "YELLOW",
    "OLIVE",
    "FUCHSIA",
    "DARKRED",
    "DARKMAGENTA",
    "DARKGREEN",
    "DARKCYAN",
    "DARKBLUE",
    "BLUE",
)

# spec §6: `ADC_VDD_<ccc>_<rail>/<die>` where <ccc>/100 = volts (050 055 070 075
# 105 120 180 observed). The code must be exactly three digits and be delimited
# on both sides (`_VDD_` before, `_` / `/` / end after) -- anything else is not a
# confident match and yields None rather than a wrong default.
_VOLTAGE_CODE_RE = re.compile(r"(?:^|_)VDD_(\d{3})(?=$|[_/])", re.IGNORECASE)

# spec §4: "{NetName} ends in /0 or /1 = die instance".
_DIE_RE = re.compile(r"/(\d+)$")


def guess_voltage(net: str) -> float | None:
    """``..._VDD_<ccc>_...`` -> ``ccc / 100`` volts (spec §6); ``None`` if unmatched.

    ``ADC_VDD_070_VP_HSIO/0`` -> ``0.70``. Returns ``None`` when the name has no
    three-digit ``VDD`` code, and also when it carries two *different* codes --
    an ambiguous name is not a confident match, and callers fall back to 1.0 V
    (design §B "auto-guessed ... fallback 1.0").
    """
    if not net:
        return None
    codes = {match.group(1) for match in _VOLTAGE_CODE_RE.finditer(net)}
    if len(codes) != 1:
        return None
    return int(codes.pop()) / 100.0


def die_of(net: str) -> int | None:
    """Trailing ``/0`` or ``/1`` on *net* -> die index; ``None`` if absent."""
    if not net:
        return None
    match = _DIE_RE.search(net)
    return int(match.group(1)) if match else None


def vrm_name(comp: str, pnet: str, gnet: str) -> str:
    """``VRM_{comp}_{pnet}_{gnet}`` (spec §6)."""
    return f"VRM_{comp}_{pnet}_{gnet}"


def sink_name(comp: str, pnet: str, gnet: str) -> str:
    """``SINK_{comp}_{pnet}_{gnet}`` (spec §6)."""
    return f"SINK_{comp}_{pnet}_{gnet}"


def sink_component(die: int | None) -> str:
    """``SITE{die}`` (spec §6 die-selection rule: ``/0`` -> ``SITE0``, ``/1`` -> ``SITE1``).

    A net with no die suffix (``die is None``) has no site of its own; ``SITE0``
    is returned as the deterministic default. Whether that circuit actually
    exists is the caller's problem -- design §G.3 has ``Session`` fall back to
    the circuit with the most pins of the power net when it does not.
    """
    return f"SITE{0 if die is None else die}"


def color_for(i: int) -> str:
    """Cycle the 11-name palette (spec §2) by index."""
    return COLOR_CYCLE[i % len(COLOR_CYCLE)]
