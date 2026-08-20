"""`.VRM`/`.Sink`/`.OtherCircuit` block text generation -- owned by chunk 2 (design §A/§D;
spec §9 templates, verbatim).

Pure text generation: no file I/O and no byte-offset logic (that is
`writer.py`'s job). Every rendered string is LF-only and ends with ``"\\n"``, so
concatenating blocks yields the §3 contiguity property (no blank lines between
`.EndVRM` and the next `.VRM`, or between the last `.EndVRM` and the first
`.Sink`) for free.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence

from powerdc_setup_tool.core.model import SinkConfig, VrmConfig
from powerdc_setup_tool.core.naming import sink_name, vrm_name

# One circuit's (pin, node) pairs on one net.
Pins = tuple[tuple[str, str], ...]
# (positive_pins, negative_pins) -- design §D WritePlan.
PinPairs = tuple[Pins, Pins]

# spec §8 row 2 / design §G.2 default filter for `.OtherCircuit` emission.
# Production RefDes spellings include bare ``C123``, slash-die ``C123/0`` /
# ``C123/1`` and the legacy underscore-die ``C123_0`` / ``C123_1`` forms.
# Keep this predicate strict: near misses (other die numbers, extra suffixes,
# or alphanumeric tails) must never be regenerated as discrete circuits.
OTHER_CIRCUIT_RE: re.Pattern[str] = re.compile(
    r"^C(?P<number>\d+)(?:(?P<separator>[/_])(?P<die>[01]))?$"
)

# `NegPinCache` style keys. "sink" additionally carries ` Voltage = inf` (spec §5).
STYLE_VRM = "vrm"
STYLE_SINK = "sink"
_STYLES = (STYLE_VRM, STYLE_SINK)

# spec §5: every Sink `.Node` line carries this suffix (ambiguity A4: "always emit inf").
_VOLTAGE_INF = " Voltage = inf"

_OTHER_CIRCUIT_SORT_RE = OTHER_CIRCUIT_RE


def is_other_circuit(name: str) -> bool:
    """Return whether *name* is an accepted discrete-capacitor RefDes."""
    return OTHER_CIRCUIT_RE.fullmatch(str(name)) is not None


def _num(value: float) -> str:
    """Render an engineering value the way the converter does (``1``, ``0.7``, ``1.2``)."""
    return f"{value:g}"


def _blen(text: str) -> int:
    # `surrogateescape` mirrors how `core/writer.py` actually encodes this text:
    # net/pin names harvested from a non-UTF-8 file carry lone surrogates, which
    # a plain `encode("utf-8")` would reject outright (design §G.6).
    return len(text.encode("utf-8", errors="surrogateescape"))


def _render_maps(comp: str, net: str, pins: Pins, voltage_inf: bool) -> str:
    """The repeated `.Map`/`.Node`/`.EndMap` triple of one `.Pin` section (spec §9)."""
    suffix = _VOLTAGE_INF if voltage_inf else ""
    return "".join(
        f".Map CircuitName = {comp} CircuitPinName = {pin}\n"
        f".Node Name = {node}!!{pin}::{net}{suffix}\n"
        ".EndMap\n"
        for pin, node in pins
    )


def _maps_bytes(comp: str, net: str, pins: Pins, voltage_inf: bool) -> int:
    """UTF-8 size of `_render_maps` without materializing it (design §D memory bound)."""
    if not pins:
        return 0
    constant = (
        _blen(".Map CircuitName =  CircuitPinName = \n")
        + _blen(".Node Name = !!::\n")
        + _blen(".EndMap\n")
        + (_blen(_VOLTAGE_INF) if voltage_inf else 0)
        + _blen(comp)
        + _blen(net)
    )
    total = constant * len(pins)
    for pin, node in pins:
        total += 2 * _blen(pin) + _blen(node)
    return total


def render_vrm(
    cfg: VrmConfig,
    pos_pins: Pins,
    neg_pins: Pins,
    *,
    cache: NegPinCache | None = None,
) -> str:
    """Render one complete `.VRM ... .EndVRM` block (spec §9 template, verbatim).

    *pos_pins* are `cfg.net`'s (pin, node) pairs on `cfg.comp`, *neg_pins* are
    `cfg.gnet`'s. Pass a `NegPinCache` to reuse the (identical across every VRM
    on the same component/ground) negative-pin blob.
    """
    if cache is None:
        negative = _render_maps(cfg.comp, cfg.gnet, neg_pins, False)
    else:
        negative = cache.get(cfg.comp, cfg.gnet, STYLE_VRM, neg_pins)
    return (
        f".VRM NominalVoltage = {_num(cfg.nominal_voltage)}"
        f" SenseVoltage = {_num(cfg.sense_voltage)}"
        f" OutputCurrent = {_num(cfg.output_current)}"
        f' Name = "{vrm_name(cfg.comp, cfg.net, cfg.gnet)}"\n'
        '.Pin Name = "Positive Pin"\n'
        f"{_render_maps(cfg.comp, cfg.net, pos_pins, False)}"
        ".EndPin\n"
        '.Pin Name = "Negative Pin"\n'
        f"{negative}"
        ".EndPin\n"
        '.Pin Name = "Positive Sense Pin"\n'
        ".EndPin\n"
        '.Pin Name = "Negative Sense Pin"\n'
        ".EndPin\n"
        ".EndVRM\n"
    )


def render_sink(
    cfg: SinkConfig,
    pos_pins: Pins,
    neg_pins: Pins,
    *,
    cache: NegPinCache | None = None,
) -> str:
    """Render one complete `.Sink ... .EndSink` block (spec §9 template, verbatim).

    `Model`/`PFMode`/`PinEqualCurrent` are carried verbatim from *cfg* (spec
    ambiguity A3, defaults 2/2/1); the empty `.SinkCurrentSource` pair is always
    emitted (A5).
    """
    if cache is None:
        negative = _render_maps(cfg.comp, cfg.gnet, neg_pins, True)
    else:
        negative = cache.get(cfg.comp, cfg.gnet, STYLE_SINK, neg_pins)
    return (
        f".Sink NominalVoltage = {_num(cfg.nominal_voltage)}"
        f" Current = {_num(cfg.current)}"
        f" Model = {cfg.model} PFMode = {cfg.pf_mode} PinEqualCurrent = {cfg.pin_equal_current}"
        f' Name = "{sink_name(cfg.comp, cfg.net, cfg.gnet)}"\n'
        '.Pin Name = "Positive Pin"\n'
        f"{_render_maps(cfg.comp, cfg.net, pos_pins, True)}"
        ".EndPin\n"
        '.Pin Name = "Negative Pin"\n'
        f"{negative}"
        ".EndPin\n"
        ".SinkCurrentSource\n"
        ".EndSinkCurrentSource\n"
        ".EndSink\n"
    )


def _other_circuit_sort_key(name: str) -> tuple[int, int, int, str]:
    """Sort by (numeric id, die) per design §D step 3; unparsable names sort last."""
    match = _OTHER_CIRCUIT_SORT_RE.fullmatch(name)
    if match is None:
        return (1, 0, 0, name)
    die = match.group("die")
    # Bare RefDes has no die; sort it before the explicit die variants while
    # retaining the exact source spelling as the deterministic final key.
    return (0, int(match.group("number")), -1 if die is None else int(die), name)


def render_other_circuits(names: Iterable[str]) -> str:
    """Render `.OtherCircuit Device = {n} Name = "{n}"` lines, sorted by (numeric id, die)."""
    return "".join(
        f'.OtherCircuit Device = {name} Name = "{name}"\n'
        for name in sorted(names, key=_other_circuit_sort_key)
    )


class NegPinCache:
    """LRU cache (capacity 8) of rendered negative-pin blobs, keyed by ``(comp, gnet, style)``.

    ``style`` is ``"vrm"`` or ``"sink"`` (Sink negative pins additionally carry
    ``Voltage = inf``, spec §5). Blobs are byte-identical across all blocks
    sharing a key (spec §4/§D) -- this cache is what keeps `writer.py`'s peak
    memory bounded per the design §D size budget.

    The key is assumed to determine the pin set (spec §4: the negative-pin blob
    is "identical across VRMs"); the *pins* argument of a hit is not re-checked,
    which is what makes a hit O(1) instead of O(pins).
    """

    def __init__(self, maxsize: int = 8) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self.maxsize = maxsize
        self._entries: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, comp: str, gnet: str, style: str, pins: Pins) -> str:
        if style not in _STYLES:
            raise ValueError(f"style must be one of {_STYLES}, got {style!r}")
        key = (comp, gnet, style)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            self.hits += 1
            return cached
        self.misses += 1
        blob = _render_maps(comp, gnet, pins, style == STYLE_SINK)
        self._entries[key] = blob
        if len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)
        return blob


def estimate_block_bytes(
    cfg: VrmConfig | SinkConfig,
    pos_pins: Pins,
    neg_pins: Pins,
) -> int:
    """Exact UTF-8 size of `render_vrm`/`render_sink` for *cfg*, without rendering it."""
    if isinstance(cfg, VrmConfig):
        header = (
            f".VRM NominalVoltage = {_num(cfg.nominal_voltage)}"
            f" SenseVoltage = {_num(cfg.sense_voltage)}"
            f" OutputCurrent = {_num(cfg.output_current)}"
            f' Name = "{vrm_name(cfg.comp, cfg.net, cfg.gnet)}"\n'
        )
        fixed = (
            '.Pin Name = "Positive Pin"\n'
            ".EndPin\n"
            '.Pin Name = "Negative Pin"\n'
            ".EndPin\n"
            '.Pin Name = "Positive Sense Pin"\n'
            ".EndPin\n"
            '.Pin Name = "Negative Sense Pin"\n'
            ".EndPin\n"
            ".EndVRM\n"
        )
        inf = False
    else:
        header = (
            f".Sink NominalVoltage = {_num(cfg.nominal_voltage)}"
            f" Current = {_num(cfg.current)}"
            f" Model = {cfg.model} PFMode = {cfg.pf_mode}"
            f" PinEqualCurrent = {cfg.pin_equal_current}"
            f' Name = "{sink_name(cfg.comp, cfg.net, cfg.gnet)}"\n'
        )
        fixed = (
            '.Pin Name = "Positive Pin"\n'
            ".EndPin\n"
            '.Pin Name = "Negative Pin"\n'
            ".EndPin\n"
            ".SinkCurrentSource\n"
            ".EndSinkCurrentSource\n"
            ".EndSink\n"
        )
        inf = True
    return (
        _blen(header)
        + _blen(fixed)
        + _maps_bytes(cfg.comp, cfg.net, pos_pins, inf)
        + _maps_bytes(cfg.comp, cfg.gnet, neg_pins, inf)
    )


def other_circuit_bytes(names: Iterable[str]) -> int:
    """UTF-8 size of `render_other_circuits(names)` without materializing it."""
    constant = _blen('.OtherCircuit Device =  Name = ""\n')
    return sum(constant + 2 * _blen(name) for name in names)


def estimate_insert_bytes(
    cfgs: Sequence[VrmConfig | SinkConfig],
    pins: Mapping[str, PinPairs],
    opts: Mapping[str, bool],
    *,
    other_circuit_names: Iterable[str] = (),
) -> int:
    """Estimate total inserted-text size, for export progress (design §D size budget).

    *pins* maps a block name (`VRM_...`/`SINK_...`) -- or, as a fallback, the bare
    net name -- to that block's ``(positive_pins, negative_pins)``. *opts* uses the
    design §C export-dialog keys; only ``"other_circuits"`` affects the size here,
    and the names it would emit are passed separately (a bool cannot carry them).
    """
    total = 0
    for cfg in cfgs:
        if isinstance(cfg, VrmConfig):
            name = vrm_name(cfg.comp, cfg.net, cfg.gnet)
        else:
            name = sink_name(cfg.comp, cfg.net, cfg.gnet)
        pos, neg = pins.get(name) or pins.get(cfg.net) or ((), ())
        total += estimate_block_bytes(cfg, pos, neg)
    if opts.get("other_circuits", True):
        total += other_circuit_bytes(other_circuit_names)
    return total
