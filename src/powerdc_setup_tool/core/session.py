"""`Session` -- single source of truth for config + derived-value propagation
-- owned by chunk 3 (design §B propagation rule, §C autopair/export flow,
§D `build_plan`, §G.1-4 edge cases).

Plain Python, no Qt; headless-testable per design §E `test_session`.

Row keys
--------
Design §B says "`Session` mutators return the list of changed ``(row, col)``
keys so `BaseConfigModel` can emit one batched `dataChanged` per bulk
operation". `Session` cannot know table geometry, so it returns *stable*
``(row_key, field)`` pairs and the Qt models translate them::

    row_key ::= "net:"  <net>
              | "vrm:"  <net> "#" <row_id>
              | "sink:" <net> "#" <row_id>
    field   ::= a `PowerNetConfig`/`VrmConfig`/`SinkConfig` attribute name
              | "name"          (the derived, read-only block-name column)

Build the keys with `net_key`/`vrm_key`/`sink_key`, take them apart with
`split_key`. Every mutator returns a **sorted** ``list[ChangedKey]`` covering
every field it actually moved -- including derived fields it moved indirectly,
so a model can repaint a whole propagation cascade from one return value. A
mutator that adds or removes rows additionally bumps `structure_version`; a
model that sees it change must reset instead of patching cells.

Voltage propagation (design §B, verbatim)
-----------------------------------------
`PowerNetConfig.voltage` is the single upstream value, auto-guessed by
`naming.guess_voltage(net)` (fallback ``1.0``) while ``voltage_override`` is
False. `set_net_voltage` sets ``voltage_override=True`` and pushes the value
into every derived field whose own ``*_override`` is False
(`VrmConfig.nominal_voltage`, `VrmConfig.sense_voltage`,
`SinkConfig.nominal_voltage`). Editing a derived cell sets only that field's
``*_override`` and never propagates upward. `reset_auto(field_keys)` clears
overrides and re-derives.

One refinement inside that rule: an auto `VrmConfig.sense_voltage` tracks its
own row's **effective nominal voltage**, not the net voltage, because spec §9
defines ``{v_sense} = {v_nom}``. The two readings coincide whenever the
nominal is itself auto (the §B case); they differ only after the user
overrides the nominal, where following the row is what the spec asks for.

Deliberate deviations / resolutions (call sites in `tests/test_session.py`)
--------------------------------------------------------------------------
* **Structural overrides.** `VrmConfig`/`SinkConfig` carry ``*_override``
  flags for their numeric fields only, and `core/model.py` belongs to chunk 0,
  so the "user picked this" flag for the non-numeric derived fields (``comp``,
  ``gnet``) lives in `Session._pinned`, a set of `ChangedKey`. It is saved in
  `to_json` and cleared by `reset_auto` exactly like an ``*_override``.
* **Preloaded values (§G.1/§G.2).** A value read out of the input file
  (`.NetList` ``Voltage =``, an existing `.VRM`/`.Sink` header, that block's
  component and ground) is marked as an override **iff it differs from the
  value this session would derive**. A converter default that matches the
  auto-guess stays linked to the net voltage; a hand-edited value survives the
  round trip instead of being silently re-derived (spec §5 documents exactly
  one such hand-edited Sink). `reset_auto` is the one-click way back.
* **Duplicate names (§G.3).** The emitted block name is
  ``VRM_{comp}_{pnet}_{gnet}`` -- `core/pdc_gen.py` derives it from the config
  and cannot carry a suffix. So `display_name` (the read-only Name column)
  disambiguates the 2nd/3rd row sharing a base name with ``_2``/``_3``, while
  `validate()` reports the *exact* collision that would land in the file as
  blocking.
* **`selected` vs `enabled`.** `PowerNetConfig.selected` is the Net Manager's
  "Use" checkbox; `set_selected` mirrors it onto that net's VRM/Sink rows'
  ``enabled``. `build_plan`/`validate` look only at ``enabled``, so a single
  row can still be unchecked on its own tab.
* **Sink component (§G.3).** ``SITE{die}`` is used when that circuit exists
  *and carries the power net*; the "most pins of the power net" fallback
  exists precisely to avoid the 0-pin block §G.4 calls blocking, so a
  ``SITE{die}`` without the net does not win.
* `autopair()`/`derive_all()` return their `ChangedKey` lists (the chunk-0 stub
  typed both as ``None``); `to_json` takes an `indent` keyword.
* **Name-based classification (v0.1.1).** The `.NetList` already carries the
  classification markers PowerSI wrote (§G.1), so `auto_classify_by_name` only
  fills the gaps: it looks at nets whose class is still ``"none"`` and never at
  an already-classified one, `from_input` or not. `classify_net_name` is the
  name-only rule behind it (`AUTO_GROUND_NAME_RE`/`AUTO_POWER_NAME_RE`).
* **Structural bookkeeping (chunk 7).** `_ensure_ground` can materialize a
  brand-new Net Manager row (a ground net that the `.NetList` never mentioned),
  so it bumps `structure_version` like every other row-adding mutator;
  `ui/models.py` keeps its row-count safety net for sessions mutated by hand.
  `_sort_rows` is called by every row-adding path -- `add_vrm_row`/
  `add_sink_row` included -- so a row's table position is always netlist order
  (design §D block order) and never depends on when it was created.
* **Classification origin (v0.1.2).** `PowerNetConfig.class_source` answers the
  Net Manager's *Source* column -- "where did this net's class come from?" --
  with ``"input"`` (the `.NetList`'s own `PowerNets`/`GroundNets` membership,
  or an existing `.VRM`/`.Sink` block, i.e. everything `load` preloads),
  ``"auto"`` (`auto_classify_by_name`, or `_ensure_ground` promoting a net
  because something paired to it), ``"user"`` (`set_class`, the only mutator a
  hand edit reaches) and ``""`` while the net is unclassified. It rides in
  `_NET_FIELDS`, so a change to it comes back as a `ChangedKey` and `to_json`
  carries it for free; `_migrated_class_source` reconstructs it from
  ``from_input`` when a 0.1.1 config is loaded.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from powerdc_setup_tool.core import naming
from powerdc_setup_tool.core.model import (
    ExistingBlock,
    PowerNetConfig,
    ScanResult,
    SinkConfig,
    ValidationIssue,
    VrmConfig,
)
from powerdc_setup_tool.core.netlist import GROUP_NODES, render_netlist
from powerdc_setup_tool.core.pdc_gen import OTHER_CIRCUIT_RE
from powerdc_setup_tool.core.writer import WritePlan

__all__ = [
    "ChangedKey",
    "Session",
    "classify_net_name",
    "net_key",
    "vrm_key",
    "sink_key",
    "split_key",
    "AUTO_CONTROL_NAME_RE",
    "AUTO_GROUND_NAME_RE",
    "AUTO_POWER_NAME_RE",
    "CONFIG_VERSION",
    "DEFAULT_VOLTAGE",
    "DEFAULT_CURRENT",
    "SOURCE_INPUT",
    "SOURCE_AUTO",
    "SOURCE_USER",
    "SOURCE_NONE",
]

# (row_key, field) -- ui/models.py maps these to concrete (row, col) indices.
ChangedKey = tuple[str, str]

#: `to_json`/`from_json` payload version.
CONFIG_VERSION = 1

#: design §B "fallback 1.0" / spec §9 placeholder defaults ``{i_out}``/``{i_sink}``.
DEFAULT_VOLTAGE = 1.0
DEFAULT_CURRENT = 1.0

#: Net classes (`PowerNetConfig.net_class`).
CLASS_POWER = "power"
CLASS_GROUND = "ground"
CLASS_NONE = "none"
_CLASSES = (CLASS_POWER, CLASS_GROUND, CLASS_NONE)

#: v0.1.2 `PowerNetConfig.class_source` -- the Net Manager's *Source* column.
#: ``SOURCE_INPUT`` is written by `load` for a net the `.NetList` itself put in
#: `PowerNets`/`GroundNets`, ``SOURCE_AUTO`` by every automatic pass
#: (`auto_classify_by_name`, `_ensure_ground`), ``SOURCE_USER`` by `set_class`.
#: An unclassified net carries ``SOURCE_NONE`` (the empty string).
SOURCE_INPUT = "input"
SOURCE_AUTO = "auto"
SOURCE_USER = "user"
SOURCE_NONE = ""
_SOURCES = (SOURCE_INPUT, SOURCE_AUTO, SOURCE_USER, SOURCE_NONE)

#: design §C P/G pairing UX: ground candidates are the nets already in
#: `GroundNets` **plus** the nets whose name matches this pattern.
GROUND_NAME_RE = re.compile(r"^(?:D?GND|VSS|GROUND)", re.IGNORECASE)

#: design §G.4: `_PS`/`_GS` remote-sense nets are netlist-only and carry no pins.
SENSE_NET_RE = re.compile(r"_(?:PS|GS)(?:/\d+)?$", re.IGNORECASE)

#: v0.1.1 name-based classification (`auto_classify_by_name`): a **whole** base
#: name (die suffix stripped) that reads as a return net -- `GND`, `AGND2`,
#: `DGND_A`, `VSS`, `DDR_VSSA`, `GROUND`. Deliberately whole-name: a rail called
#: `ADC_VDD_070_GND_REF` is a power net whose name merely mentions a ground.
AUTO_GROUND_NAME_RE = re.compile(r"^(?:A?D?GND[A-Za-z0-9_]*|.*VSS.*|GROUND)$", re.IGNORECASE)

#: v0.1.1 name-based classification: an ``_``-delimited token that *starts* with
#: one of the usual rail spellings (an alphanumeric tail is part of the token,
#: so `VDD` also covers `VDDQ`/`VDDA`/`VDD2`). The real design's power nets are
#: `ADC_VDD_070_VP_HSIO/0` -- matched on the `VDD` token -- while its signal
#: nets (`W_DDR9_BP_C0_DQ[8]/1`, `AONI_GPIO[3]/0`) carry no such token.
AUTO_POWER_NAME_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])"
    r"(?:[AD]?VDD|[AD]?VCC|VPP|VBAT|VREG|VAA|VINT|VSYS|PWR)"
    r"[A-Za-z0-9]*",
    re.IGNORECASE,
)

#: v0.1.1 name-based classification: a rail token does **not** make a power net
#: when the name ends in a control/status marker -- `PWR_GOOD`, `VDD_EN` and
#: `VDDQ_PGOOD` are signals that talk *about* a rail, not the rail itself. No
#: real rail on the design ends this way, so this only ever removes false hits.
AUTO_CONTROL_NAME_RE = re.compile(
    r"_(?:EN|ENABLE|OK|GOOD|PG|PGOOD|GD|RST|RESET|FLT|FAULT)$", re.IGNORECASE
)

#: spec §4 die suffix, stripped before a name is classified.
_DIE_SUFFIX_RE = re.compile(r"/\d+$")

#: design §G.3: the die-side circuits, excluded from the VRM-component pool.
_SITE_PREFIX = "SITE"

_NET_PREFIX = "net:"
_VRM_PREFIX = "vrm:"
_SINK_PREFIX = "sink:"

_KIND_NET = "net"
_KIND_VRM = "vrm"
_KIND_SINK = "sink"

# Snapshot field lists: every mutable field a table can show, per row kind.
_NET_FIELDS = (
    "net_class",
    "paired_gnd",
    "voltage",
    "voltage_override",
    "selected",
    "die",
    "from_input",
    "class_source",
)
_VRM_FIELDS = (
    "gnet",
    "comp",
    "enabled",
    "nominal_voltage",
    "nominal_override",
    "sense_voltage",
    "sense_override",
    "output_current",
    "current_override",
)
_SINK_FIELDS = (
    "gnet",
    "comp",
    "enabled",
    "nominal_voltage",
    "nominal_override",
    "current",
    "current_override",
    "model",
    "pf_mode",
    "pin_equal_current",
)

# Editable fields and the type each `set_field` value is coerced to.
_NET_EDITABLE: dict[str, type] = {
    "net_class": str,
    "paired_gnd": str,
    "voltage": float,
    "selected": bool,
}
_VRM_EDITABLE: dict[str, type] = {
    "gnet": str,
    "comp": str,
    "enabled": bool,
    "nominal_voltage": float,
    "sense_voltage": float,
    "output_current": float,
}
_SINK_EDITABLE: dict[str, type] = {
    "gnet": str,
    "comp": str,
    "enabled": bool,
    "nominal_voltage": float,
    "current": float,
    "model": int,
    "pf_mode": int,
    "pin_equal_current": int,
}

# derived field -> its `*_override` flag (design §B).
_VRM_OVERRIDES = {
    "nominal_voltage": "nominal_override",
    "sense_voltage": "sense_override",
    "output_current": "current_override",
}
_SINK_OVERRIDES = {
    "nominal_voltage": "nominal_override",
    "current": "current_override",
}
#: Derived fields with no dataclass flag; their "user set this" bit lives in
#: `Session._pinned` (see the module docstring).
_PINNABLE = ("comp", "gnet")

_NETLIST_CLASS = {"PowerNets": CLASS_POWER, "GroundNets": CLASS_GROUND}


# --------------------------------------------------------------------------- #
# Row keys
# --------------------------------------------------------------------------- #


def net_key(net: str) -> str:
    """Row key of the Net Manager row for *net*."""
    return f"{_NET_PREFIX}{net}"


def vrm_key(net: str, row_id: int = 0) -> str:
    """Row key of VRM row *row_id* of *net*."""
    return f"{_VRM_PREFIX}{net}#{row_id}"


def sink_key(net: str, row_id: int = 0) -> str:
    """Row key of Sink row *row_id* of *net*."""
    return f"{_SINK_PREFIX}{net}#{row_id}"


def split_key(row_key: str) -> tuple[str, str, int]:
    """``"vrm:VDD/0#1"`` -> ``("vrm", "VDD/0", 1)``; net rows get ``row_id == -1``."""
    if row_key.startswith(_NET_PREFIX):
        return _KIND_NET, row_key[len(_NET_PREFIX) :], -1
    for prefix, kind in ((_VRM_PREFIX, _KIND_VRM), (_SINK_PREFIX, _KIND_SINK)):
        if row_key.startswith(prefix):
            body = row_key[len(prefix) :]
            name, _, row_id = body.rpartition("#")
            if not name:  # no "#" at all -- tolerate "vrm:NET" as row 0
                return kind, body, 0
            try:
                return kind, name, int(row_id)
            except ValueError:
                return kind, body, 0
    raise ValueError(f"not a Session row key: {row_key!r}")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def classify_net_name(net: str) -> str | None:
    """``"ground"`` / ``"power"`` / ``None`` from *net*'s name alone (v0.1.1).

    The name-only half of `Session.auto_classify_by_name`: the die suffix is
    stripped, `AUTO_GROUND_NAME_RE` is tried first (a whole-name match), then
    `AUTO_POWER_NAME_RE` (a rail token anywhere in the name, unless
    `AUTO_CONTROL_NAME_RE` marks the name as a rail *status* signal). ``None``
    means "no confident guess" -- signal nets and, deliberately, the `_PS`/`_GS`
    remote-sense nets, which design §G.4 wants left unclassified even though
    they are spelled like the rail they sense.
    """
    name = str(net or "")
    if not name or SENSE_NET_RE.search(name):
        return None
    base = _DIE_SUFFIX_RE.sub("", name)
    if not base:
        return None
    if AUTO_GROUND_NAME_RE.match(base):
        return CLASS_GROUND
    if AUTO_POWER_NAME_RE.search(base) and not AUTO_CONTROL_NAME_RE.search(base):
        return CLASS_POWER
    return None


def _is_site(circuit: str) -> bool:
    return circuit.upper().startswith(_SITE_PREFIX)


def _same(a: float, b: float) -> bool:
    """Float compare used to decide whether a *file* value is a real edit."""
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _as_float(text: object, default: float) -> float:
    try:
        return float(str(text))
    except (TypeError, ValueError):
        return default


def _as_int(text: object, default: int) -> int:
    try:
        return int(float(str(text)))
    except (TypeError, ValueError):
        return default


def _coerce(value: object, kind: type) -> Any:
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "x")
        return bool(value)
    if kind is float:
        return float(value)  # type: ignore[arg-type]
    if kind is int:
        return int(value)  # type: ignore[arg-type]
    return str(value)


def _lookup(values: Mapping[str, str], key: str) -> str | None:
    """Case-insensitive lookup in an `ExistingBlock.values` mapping."""
    if key in values:
        return values[key]
    lowered = key.lower()
    for name, value in values.items():
        if name.lower() == lowered:
            return value
    return None


def _listed(names: Iterable[str], limit: int = 8) -> str:
    """``"A, B, C and 4 more"`` -- keeps a `ValidationIssue.message` readable."""
    items = list(names)
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


def _migrated_class_source(cfg: PowerNetConfig) -> str:
    """`PowerNetConfig.class_source` for a config restored from JSON (v0.1.2).

    A config written by 0.1.1 has no ``class_source`` at all, so the field is
    reconstructed from the two facts that release *did* save: an unclassified
    net has no origin, and a classified one is ``"input"`` exactly when
    ``from_input`` says the file classified it. Anything else was the tool's
    own doing, which is ``"auto"`` -- never ``"user"``, because guessing a hand
    edit that the old format never recorded would be a lie.
    """
    if cfg.net_class == CLASS_NONE:
        return SOURCE_NONE
    if cfg.class_source in (SOURCE_INPUT, SOURCE_AUTO, SOURCE_USER):
        return cfg.class_source
    return SOURCE_INPUT if cfg.from_input else SOURCE_AUTO


class Session:
    """Owns all `*Config` state derived from a `ScanResult` plus user edits."""

    def __init__(self) -> None:
        self.scan: ScanResult | None = None
        #: net name -> config, in Net Manager display order (netlist order).
        self.nets: dict[str, PowerNetConfig] = {}
        self._order: dict[str, int] = {}
        self._vrms: list[VrmConfig] = []
        self._sinks: list[SinkConfig] = []
        #: (row_key, field) whose auto-derivation is suppressed -- see docstring.
        self._pinned: set[ChangedKey] = set()
        #: bumped whenever rows are added/removed (models must reset, not patch).
        self.structure_version = 0
        #: non-fatal notes produced by `load` (surfaced by `validate` as warnings).
        self.load_warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def load(self, scan: ScanResult) -> None:
        """Reset state from a fresh scan; preload already-classified nets (§G.1)
        and any existing `.VRM`/`.Sink` blocks (§G.2), then derive everything."""
        self.scan = scan
        self.nets = {}
        self._order = {}
        self._vrms = []
        self._sinks = []
        self._pinned = set()
        self.structure_version += 1
        warnings: list[str] = []

        # -- §G.1: the `.NetList` is the master net list and already carries the
        #    classification (spec §2: the SI file is normally classified).
        for entry in scan.nets:
            name = entry.name
            if not name or name in GROUP_NODES or name in self.nets:
                continue
            net_class = _NETLIST_CLASS.get(entry.group or "", CLASS_NONE)
            cfg = PowerNetConfig(
                net=name,
                net_class=net_class,
                voltage=DEFAULT_VOLTAGE,
                selected=True,
                die=naming.die_of(name),
                from_input=net_class != CLASS_NONE,
                # v0.1.2 Source column: the `.NetList` group *is* the origin.
                class_source=SOURCE_INPUT if net_class != CLASS_NONE else SOURCE_NONE,
            )
            cfg.voltage = self._auto_net_voltage(cfg)
            for key, value in entry.attrs:
                if key.lower() == "voltage":
                    loaded = _as_float(value, cfg.voltage)
                    cfg.voltage_override = not _same(loaded, cfg.voltage)
                    cfg.voltage = loaded
                    break
            self._add_net(cfg)

        # Nets that only the `.Connect` pin maps know about: still editable rows
        # (classifying one makes `render_netlist` append a fresh entry for it).
        known = set(self.nets)
        extra = {
            net
            for per_net in scan.pin_maps.by_circuit.values()
            for net in per_net
            if net not in known
        }
        for name in sorted(extra):
            cfg = PowerNetConfig(net=name, net_class=CLASS_NONE, die=naming.die_of(name))
            cfg.voltage = self._auto_net_voltage(cfg)
            self._add_net(cfg)

        self._preload_blocks(scan, warnings)
        self.load_warnings = tuple(warnings)
        self.derive_all()

    def _add_net(self, cfg: PowerNetConfig) -> None:
        self._order[cfg.net] = len(self._order)
        self.nets[cfg.net] = cfg

    def _preload_blocks(self, scan: ScanResult, warnings: list[str]) -> None:
        """§G.2: parse existing block headers into editable rows (never append)."""
        for block in (*scan.existing_vrms, *scan.existing_sinks):
            if not block.net:
                warnings.append(
                    f"ignored {block.kind.upper()} block with no power net: {block.name!r}"
                )
                continue
            cfg = self.nets.get(block.net)
            if cfg is None:
                cfg = PowerNetConfig(
                    net=block.net,
                    net_class=CLASS_POWER,
                    die=naming.die_of(block.net),
                    from_input=True,
                    class_source=SOURCE_INPUT,
                )
                cfg.voltage = self._auto_net_voltage(cfg)
                self._add_net(cfg)
                warnings.append(
                    f"{block.name!r} references {block.net!r}, which is not in the .NetList"
                )
            if cfg.net_class != CLASS_POWER:
                cfg.net_class = CLASS_POWER
                cfg.from_input = True
                # An existing `.VRM`/`.Sink` block is input-provided classification
                # just as much as a `PowerNets` membership is (§G.2).
                cfg.class_source = SOURCE_INPUT
                cfg.voltage = self._auto_net_voltage(cfg)
            # design §C: "each net's ground comes from its parsed block name".
            if block.gnet:
                cfg.paired_gnd = block.gnet
                self._ensure_ground(block.gnet)
            self._append_row(block)

    def _append_row(self, block: ExistingBlock) -> None:
        """Materialize one `ExistingBlock` as a VRM/Sink row (values + overrides)."""
        net = block.net
        net_cfg = self.nets[net]
        gnet = block.gnet or net_cfg.paired_gnd
        auto_comp = (
            self._default_vrm_comp(net, gnet)
            if block.kind == "vrm"
            else self._default_sink_comp(net, net_cfg.die)
        )
        comp = block.comp or auto_comp
        auto_v = net_cfg.voltage

        if block.kind == "vrm":
            row_id = self._next_row_id(self._vrms, net)
            nominal = _as_float(_lookup(block.values, "NominalVoltage"), auto_v)
            sense = _as_float(_lookup(block.values, "SenseVoltage"), nominal)
            current = _as_float(_lookup(block.values, "OutputCurrent"), DEFAULT_CURRENT)
            row = VrmConfig(
                net=net,
                gnet=gnet,
                comp=comp,
                enabled=net_cfg.selected,
                nominal_voltage=nominal,
                nominal_override=not _same(nominal, auto_v),
                sense_voltage=sense,
                sense_override=not _same(sense, nominal),
                output_current=current,
                current_override=not _same(current, DEFAULT_CURRENT),
                row_id=row_id,
            )
            self._vrms.append(row)
            key = vrm_key(net, row_id)
        else:
            row_id = self._next_row_id(self._sinks, net)
            nominal = _as_float(_lookup(block.values, "NominalVoltage"), auto_v)
            current = _as_float(_lookup(block.values, "Current"), DEFAULT_CURRENT)
            row = SinkConfig(  # type: ignore[assignment]
                net=net,
                gnet=gnet,
                comp=comp,
                enabled=net_cfg.selected,
                nominal_voltage=nominal,
                nominal_override=not _same(nominal, auto_v),
                current=current,
                current_override=not _same(current, DEFAULT_CURRENT),
                model=_as_int(_lookup(block.values, "Model"), 2),
                pf_mode=_as_int(_lookup(block.values, "PFMode"), 2),
                pin_equal_current=_as_int(_lookup(block.values, "PinEqualCurrent"), 1),
                row_id=row_id,
            )
            self._sinks.append(row)  # type: ignore[arg-type]
            key = sink_key(net, row_id)

        # Structural fields carry no dataclass flag: pin them only when the file
        # disagrees with what this session would have derived anyway.
        if comp != auto_comp:
            self._pinned.add((key, "comp"))
        if gnet != net_cfg.paired_gnd:
            self._pinned.add((key, "gnet"))

    @staticmethod
    def _next_row_id(rows: list[Any], net: str) -> int:
        used = {row.row_id for row in rows if row.net == net}
        row_id = 0
        while row_id in used:
            row_id += 1
        return row_id

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def net_rows(self) -> list[PowerNetConfig]:
        """Net Manager rows, in `.NetList` order (pin-map-only nets last)."""
        return [self.nets[net] for net in self._ordered_nets()]

    @property
    def vrm_rows(self) -> list[VrmConfig]:
        return list(self._vrms)

    @property
    def sink_rows(self) -> list[SinkConfig]:
        return list(self._sinks)

    def _ordered_nets(self) -> list[str]:
        limit = len(self._order)
        return sorted(self.nets, key=lambda net: (self._order.get(net, limit), net))

    def power_nets(self) -> list[str]:
        """Power nets in `PowerNets` netlist order (design §D block order)."""
        return [n for n in self._ordered_nets() if self.nets[n].net_class == CLASS_POWER]

    def ground_nets(self) -> list[str]:
        """Nets classified ground -- the Paired GND combo's model (design §C).

        Live, not cached: `ui/models.py`'s Paired GND / Ground columns call this
        every time an editor opens, so a net classified ground a moment ago is
        offered immediately (v0.1.2).
        """
        return [n for n in self._ordered_nets() if self.nets[n].net_class == CLASS_GROUND]

    def class_source(self, net: str) -> str:
        """v0.1.2 *Source* column: ``"input"``/``"auto"``/``"user"``/``""``."""
        cfg = self.nets.get(net)
        if cfg is None or cfg.net_class == CLASS_NONE:
            return SOURCE_NONE
        return cfg.class_source if cfg.class_source in _SOURCES else SOURCE_NONE

    def rows_for(self, net: str) -> list[VrmConfig | SinkConfig]:
        """Every VRM/Sink row belonging to *net*."""
        return [row for row in (*self._vrms, *self._sinks) if row.net == net]

    def vrm(self, net: str, row_id: int = 0) -> VrmConfig | None:
        for row in self._vrms:
            if row.net == net and row.row_id == row_id:
                return row
        return None

    def sink(self, net: str, row_id: int = 0) -> SinkConfig | None:
        for row in self._sinks:
            if row.net == net and row.row_id == row_id:
                return row
        return None

    def row(self, row_key: str) -> PowerNetConfig | VrmConfig | SinkConfig | None:
        """Resolve any row key to its config object (``None`` if it is gone)."""
        kind, net, row_id = split_key(row_key)
        if kind == _KIND_NET:
            return self.nets.get(net)
        if kind == _KIND_VRM:
            return self.vrm(net, row_id)
        return self.sink(net, row_id)

    def positive_pins(self, cfg: VrmConfig | SinkConfig) -> tuple[tuple[str, str], ...]:
        """*cfg*'s power-net pins on its component (spec §7, always from `.Connect`)."""
        return self._pins(cfg.comp, cfg.net)

    def negative_pins(self, cfg: VrmConfig | SinkConfig) -> tuple[tuple[str, str], ...]:
        """*cfg*'s ground pins on its component."""
        return self._pins(cfg.comp, cfg.gnet)

    def net_pin_counts(self, net: str) -> tuple[int, int]:
        """``(pins @VRM comp, pins @Sink comp)`` -- design §C net table columns 6/7."""
        vrm_row = self.vrm(net)
        sink_row = self.sink(net)
        cfg = self.nets.get(net)
        gnet = cfg.paired_gnd if cfg else ""
        die = cfg.die if cfg else None
        vrm_comp = vrm_row.comp if vrm_row else self._default_vrm_comp(net, gnet)
        sink_comp = sink_row.comp if sink_row else self._default_sink_comp(net, die)
        return len(self._pins(vrm_comp, net)), len(self._pins(sink_comp, net))

    def counts(self) -> dict[str, int]:
        """Status-bar counters (design §C: ``Nets 3712 · Power 92 · ... · Sink 92``)."""
        classes = [cfg.net_class for cfg in self.nets.values()]
        return {
            "nets": len(self.nets),
            "power": classes.count(CLASS_POWER),
            "ground": classes.count(CLASS_GROUND),
            "unclassified": classes.count(CLASS_NONE),
            "selected": sum(1 for cfg in self.nets.values() if cfg.selected),
            "vrm": sum(1 for row in self._vrms if row.enabled),
            "sink": sum(1 for row in self._sinks if row.enabled),
        }

    # -- derived names ------------------------------------------------- #

    @staticmethod
    def block_name(cfg: VrmConfig | SinkConfig) -> str:
        """The name that will actually be written (spec §6; no dedup suffix)."""
        if isinstance(cfg, VrmConfig):
            return naming.vrm_name(cfg.comp, cfg.net, cfg.gnet)
        return naming.sink_name(cfg.comp, cfg.net, cfg.gnet)

    def display_names(self) -> dict[str, str]:
        """row_key -> Name column text, with §G.3's ``_2``/``_3`` dedup suffixes."""
        out: dict[str, str] = {}
        seen: dict[str, int] = {}
        for rows, key_of in ((self._vrms, vrm_key), (self._sinks, sink_key)):
            for row in rows:
                base = self.block_name(row)
                count = seen.get(base, 0) + 1
                seen[base] = count
                out[key_of(row.net, row.row_id)] = base if count == 1 else f"{base}_{count}"
        return out

    def display_name(self, cfg: VrmConfig | SinkConfig) -> str:
        key = (vrm_key if isinstance(cfg, VrmConfig) else sink_key)(cfg.net, cfg.row_id)
        return self.display_names().get(key, self.block_name(cfg))

    # ------------------------------------------------------------------ #
    # Derivation (design §B / §G.3)
    # ------------------------------------------------------------------ #

    def _pins(self, comp: str, net: str) -> tuple[tuple[str, str], ...]:
        if self.scan is None or not comp or not net:
            return ()
        return self.scan.pin_maps.pins(comp, net)

    def _circuit_names(self) -> list[str]:
        if self.scan is None:
            return []
        names = [info.name for info in self.scan.circuits]
        return names or sorted(self.scan.pin_maps.by_circuit)

    def _best_circuit(self, candidates: Iterable[str], net: str) -> str:
        """Circuit with the most pins of *net*; tie -> ASCII-first; "" if none has any."""
        best = ""
        best_count = 0
        for name in candidates:
            count = len(self._pins(name, net))
            if count > best_count or (count == best_count and count and name < best):
                best, best_count = name, count
        return best

    def _board_circuits(self) -> list[str]:
        """§G.3 VRM-component pool: no `SITE*`, no `^C\\d+_[01]$` discrete."""
        return [
            name
            for name in self._circuit_names()
            if not _is_site(name) and not OTHER_CIRCUIT_RE.match(name)
        ]

    def _default_vrm_comp(self, net: str, gnet: str) -> str:
        """§G.3: non-`SITE`, non-`.OtherCircuit` circuit with the most pins of *gnet*."""
        board = self._board_circuits()
        return (
            self._best_circuit(board, gnet)
            or self._best_circuit(self._circuit_names(), gnet)
            or self._best_circuit(board, net)
            or self._best_circuit(self._circuit_names(), net)
            or VrmConfig.comp  # the dataclass default ("LGA")
        )

    def _default_sink_comp(self, net: str, die: int | None) -> str:
        """§G.3: ``SITE{die}`` when it carries *net*, else the most-pinned circuit."""
        site = naming.sink_component(die if die is not None else naming.die_of(net))
        if self._pins(site, net):
            return site
        return self._best_circuit(self._circuit_names(), net) or site

    def _pairing_component(self) -> str:
        """The board-side circuit ground candidates are ranked on (design §C)."""
        pool = self._board_circuits() or self._circuit_names()
        best = ""
        best_count = -1
        for name in pool:
            per_net = self.scan.pin_maps.by_circuit.get(name, {}) if self.scan else {}
            count = sum(len(pins) for pins in per_net.values())
            if count > best_count or (count == best_count and name < best):
                best, best_count = name, count
        return best

    def _ground_candidates(self) -> list[str]:
        """§C: nets already in `GroundNets` ∪ nets matching `^(D?GND|VSS|GROUND)`."""
        return [
            net
            for net in self._ordered_nets()
            if self.nets[net].net_class == CLASS_GROUND or GROUND_NAME_RE.match(net)
        ]

    def _choose_ground(self, net: str) -> str:
        """Default paired ground: most pins on the VRM component, tie -> ASCII-first."""
        candidates = [g for g in self._ground_candidates() if g != net]
        if not candidates:
            return ""
        comp = self._pairing_component()
        return min(candidates, key=lambda g: (-len(self._pins(comp, g)), g))

    def _ensure_ground(self, gnet: str) -> None:
        """A net used as a paired ground must be in `GroundNets` (spec §2)."""
        cfg = self.nets.get(gnet)
        if cfg is None:
            cfg = PowerNetConfig(
                net=gnet,
                net_class=CLASS_GROUND,
                die=naming.die_of(gnet),
                # The tool decided this net is a ground, not the file or the
                # user picking it from the Class cell (v0.1.2 Source column).
                class_source=SOURCE_AUTO,
            )
            cfg.voltage = self._auto_net_voltage(cfg)
            self._add_net(cfg)
            # A brand-new Net Manager row *is* a structural change: a model that
            # only patched cells would keep a stale row list (design §B "a
            # mutator that adds or removes rows additionally bumps
            # `structure_version`"). `load`/`apply_json` bump once themselves,
            # so the extra bumps they trigger here are harmless.
            self.structure_version += 1
        elif cfg.net_class == CLASS_NONE:
            cfg.net_class = CLASS_GROUND
            cfg.class_source = SOURCE_AUTO
            if not cfg.voltage_override:
                cfg.voltage = self._auto_net_voltage(cfg)

    def _auto_net_voltage(self, cfg: PowerNetConfig) -> float:
        """design §B: `guess_voltage(net)` with a 1.0 fallback; ground nets are 0 V."""
        if cfg.net_class == CLASS_GROUND:
            return 0.0
        guessed = naming.guess_voltage(cfg.net)
        return DEFAULT_VOLTAGE if guessed is None else guessed

    # -- structure ------------------------------------------------------ #

    def _sync_rows(self) -> bool:
        """Create/drop VRM+Sink rows so every power net has at least one of each."""
        changed = False
        power = set(self.power_nets())

        keep_vrms = [row for row in self._vrms if row.net in power]
        keep_sinks = [row for row in self._sinks if row.net in power]
        if len(keep_vrms) != len(self._vrms) or len(keep_sinks) != len(self._sinks):
            gone = {vrm_key(r.net, r.row_id) for r in self._vrms if r.net not in power}
            gone |= {sink_key(r.net, r.row_id) for r in self._sinks if r.net not in power}
            self._pinned = {pin for pin in self._pinned if pin[0] not in gone}
            self._vrms, self._sinks = keep_vrms, keep_sinks
            changed = True

        have_vrm = {row.net for row in self._vrms}
        have_sink = {row.net for row in self._sinks}
        for net in self.power_nets():
            cfg = self.nets[net]
            if not cfg.paired_gnd:
                gnet = self._choose_ground(net)
                if gnet:
                    cfg.paired_gnd = gnet
                    self._ensure_ground(gnet)
            if net not in have_vrm:
                self._vrms.append(
                    VrmConfig(
                        net=net,
                        gnet=cfg.paired_gnd,
                        comp=self._default_vrm_comp(net, cfg.paired_gnd),
                        enabled=cfg.selected,
                    )
                )
                changed = True
            if net not in have_sink:
                self._sinks.append(
                    SinkConfig(
                        net=net,
                        gnet=cfg.paired_gnd,
                        comp=self._default_sink_comp(net, cfg.die),
                        enabled=cfg.selected,
                    )
                )
                changed = True

        self._sort_rows()
        if changed:
            self.structure_version += 1
        return changed

    def _sort_rows(self) -> None:
        """Put VRM/Sink rows in `PowerNets` netlist order (design §D block order).

        Every path that adds a row calls this, so a row's position never depends
        on *when* it was created -- `add_vrm_row`/`add_sink_row` land where
        `_sync_rows` would have put them, not at the end of the table.
        """
        rank = {net: i for i, net in enumerate(self._ordered_nets())}
        limit = len(rank)
        self._vrms.sort(key=lambda r: (rank.get(r.net, limit), r.net, r.row_id))
        self._sinks.sort(key=lambda r: (rank.get(r.net, limit), r.net, r.row_id))

    def _propagate(self, nets: Iterable[str] | None = None) -> None:
        """Recompute every non-overridden derived field (design §B)."""
        scope = None if nets is None else set(nets)
        for net, cfg in self.nets.items():
            if scope is not None and net not in scope:
                continue
            if not cfg.voltage_override:
                cfg.voltage = self._auto_net_voltage(cfg)
        for row in self._vrms:
            if scope is not None and row.net not in scope:
                continue
            cfg = self.nets.get(row.net)
            key = vrm_key(row.net, row.row_id)
            if cfg is not None and (key, "gnet") not in self._pinned:
                row.gnet = cfg.paired_gnd
            if (key, "comp") not in self._pinned:
                row.comp = self._default_vrm_comp(row.net, row.gnet)
            if not row.nominal_override and cfg is not None:
                row.nominal_voltage = cfg.voltage
            if not row.sense_override:
                # spec §9: `{v_sense}` = `{v_nom}` (see module docstring).
                row.sense_voltage = row.nominal_voltage
            if not row.current_override:
                row.output_current = DEFAULT_CURRENT
        for sink in self._sinks:
            if scope is not None and sink.net not in scope:
                continue
            cfg = self.nets.get(sink.net)
            key = sink_key(sink.net, sink.row_id)
            if cfg is not None and (key, "gnet") not in self._pinned:
                sink.gnet = cfg.paired_gnd
            if (key, "comp") not in self._pinned:
                sink.comp = self._default_sink_comp(sink.net, cfg.die if cfg else None)
            if not sink.nominal_override and cfg is not None:
                sink.nominal_voltage = cfg.voltage
            if not sink.current_override:
                sink.current = DEFAULT_CURRENT

    def derive_all(self) -> list[ChangedKey]:
        """Rebuild rows for the current classification and re-derive every
        non-overridden field. Idempotent; safe to call after any structural edit."""
        if self.scan is None and not self.nets:
            return []
        before = self._snapshot()
        self._sync_rows()
        self._propagate()
        return self._diff(before)

    def auto_classify_by_name(self) -> list[ChangedKey]:
        """Classify the still-unclassified nets from their names (v0.1.1).

        Only nets whose `net_class` is ``"none"`` are touched: the `.NetList`'s
        own `PowerNets`/`GroundNets` membership (§G.1, ``from_input``) and every
        hand-made choice are markers this must never second-guess -- it only
        fills the gaps PowerSI left. `classify_net_name` decides per name, so
        `_PS`/`_GS` sense nets stay unclassified (§G.4).

        Newly classified rows then go through the normal derive path
        (`_sync_rows`/`_propagate`), which is what gives a new power net its
        name-guessed voltage, a default paired ground and its VRM/Sink rows.
        """
        before = self._snapshot()
        for net in self._ordered_nets():
            cfg = self.nets[net]
            if cfg.net_class != CLASS_NONE:
                continue
            guess = classify_net_name(net)
            if guess is None:
                continue
            cfg.net_class = guess
            cfg.class_source = SOURCE_AUTO  # v0.1.2 Source column
            if guess != CLASS_POWER:
                cfg.paired_gnd = ""  # same invariant `set_class` keeps
            if not cfg.voltage_override:
                cfg.voltage = self._auto_net_voltage(cfg)
        self._sync_rows()
        self._propagate()
        return self._diff(before)

    def autopair(self, *, force: bool = False) -> list[ChangedKey]:
        """Default `paired_gnd` per power net: the ground candidate with the most
        pins on the VRM component, tie -> ASCII-first (design §C).

        Only fills nets whose paired ground is missing or no longer a ground net,
        so a ground preloaded from an existing block (§G.2) or picked by the user
        survives; ``force=True`` re-pairs every power net.
        """
        before = self._snapshot()
        for net in self.power_nets():
            cfg = self.nets[net]
            current = self.nets.get(cfg.paired_gnd)
            if not force and current is not None and current.net_class == CLASS_GROUND:
                continue
            gnet = self._choose_ground(net)
            if not gnet:
                continue
            cfg.paired_gnd = gnet
            self._ensure_ground(gnet)
            for row in self.rows_for(net):
                key = (vrm_key if isinstance(row, VrmConfig) else sink_key)(row.net, row.row_id)
                self._pinned.discard((key, "gnet"))
        self._sync_rows()
        self._propagate()
        return self._diff(before)

    # ------------------------------------------------------------------ #
    # Mutators
    # ------------------------------------------------------------------ #

    def _require_net(self, net: str) -> PowerNetConfig:
        cfg = self.nets.get(net)
        if cfg is None:
            raise KeyError(f"unknown net: {net!r}")
        return cfg

    def set_net_voltage(self, net: str, voltage: float) -> list[ChangedKey]:
        """Set `PowerNetConfig.voltage`; propagate into non-overridden derived fields."""
        cfg = self._require_net(net)
        before = self._snapshot([net])
        cfg.voltage = float(voltage)
        cfg.voltage_override = True
        self._propagate([net])
        return self._diff(before, [net])

    def set_class(self, net: str, net_class: str) -> list[ChangedKey]:
        """Set `PowerNetConfig.net_class` (``"power"``/``"ground"``/``"none"``).

        This is the *user* entry point (the Class cell, the right-click
        *Classify* submenu), so it stamps `class_source` as ``"user"`` -- and
        clears it back to ``""`` when the net becomes unclassified again. The
        automatic passes (`auto_classify_by_name`, `_ensure_ground`) set the
        field directly and never come through here (v0.1.2 Source column).
        """
        value = str(net_class).strip().lower()
        if value not in _CLASSES:
            raise ValueError(f"net_class must be one of {_CLASSES}, got {net_class!r}")
        cfg = self._require_net(net)
        # Scope: this net, the ground it currently points at, and the ground
        # `_sync_rows` is about to pick for it (nothing else can move).
        scope = {net, cfg.paired_gnd}
        if value == CLASS_POWER and not cfg.paired_gnd:
            scope.add(self._choose_ground(net))
        scope.discard("")
        before = self._snapshot(scope)
        cfg.net_class = value
        cfg.class_source = SOURCE_NONE if value == CLASS_NONE else SOURCE_USER
        if value != CLASS_POWER:
            cfg.paired_gnd = ""
        self._sync_rows()
        self._propagate(scope)
        return self._diff(before, scope)

    def set_paired_ground(self, net: str, gnet: str) -> list[ChangedKey]:
        """Set the paired ground net for *net* (`PowerNetConfig`/`VrmConfig`/`SinkConfig`).

        Also un-pins the rows' own ``gnet`` so a ground preloaded from an existing
        block follows the new choice, and classifies *gnet* as ground (spec §2:
        PowerDC reads the pairing off `GroundNets` membership).
        """
        cfg = self._require_net(net)
        scope = {net, cfg.paired_gnd, str(gnet)}
        scope.discard("")
        before = self._snapshot(scope)
        cfg.paired_gnd = str(gnet)
        if gnet:
            self._ensure_ground(str(gnet))
        for row in self.rows_for(net):
            key = (vrm_key if isinstance(row, VrmConfig) else sink_key)(row.net, row.row_id)
            self._pinned.discard((key, "gnet"))
        self._sync_rows()
        self._propagate(scope)
        return self._diff(before, scope)

    def set_selected(self, net: str, selected: bool) -> list[ChangedKey]:
        """Set the Net Manager "Use" checkbox; mirrors onto that net's rows."""
        cfg = self._require_net(net)
        before = self._snapshot([net])
        cfg.selected = bool(selected)
        for row in self.rows_for(net):
            row.enabled = cfg.selected
        return self._diff(before, [net])

    def set_field(self, row_key: str, field: str, value: object) -> list[ChangedKey]:
        """Generic cell setter -- the entry point `ui/models.py`'s `setData` uses.

        *field* is the config attribute name; the value is coerced to the field's
        type. Editing a derived field sets only that field's ``*_override`` (or
        pins it, for ``comp``/``gnet``) and never propagates upward (design §B).
        """
        kind, net, row_id = split_key(row_key)
        if kind == _KIND_NET:
            if field not in _NET_EDITABLE:
                raise ValueError(f"net rows have no editable field {field!r}")
            coerced = _coerce(value, _NET_EDITABLE[field])
            if field == "voltage":
                return self.set_net_voltage(net, coerced)
            if field == "net_class":
                return self.set_class(net, coerced)
            if field == "paired_gnd":
                return self.set_paired_ground(net, coerced)
            return self.set_selected(net, coerced)

        editable = _VRM_EDITABLE if kind == _KIND_VRM else _SINK_EDITABLE
        overrides = _VRM_OVERRIDES if kind == _KIND_VRM else _SINK_OVERRIDES
        if field not in editable:
            raise ValueError(f"{kind} rows have no editable field {field!r}")
        row = self.vrm(net, row_id) if kind == _KIND_VRM else self.sink(net, row_id)
        if row is None:
            raise KeyError(f"unknown row: {row_key!r}")

        coerced = _coerce(value, editable[field])
        # A per-row ground change also touches the ground net itself (it must be
        # in `GroundNets` for PowerDC to read the pairing, spec §2).
        scope = {net} if field != "gnet" else {net, row.gnet, str(coerced)} - {""}
        before = self._snapshot(scope)
        setattr(row, field, coerced)
        if field in overrides:
            setattr(row, overrides[field], True)
        elif field in _PINNABLE:
            self._pinned.add((row_key, field))
        if field == "gnet" and coerced:
            self._ensure_ground(str(coerced))
        self._propagate(scope)
        return self._diff(before, scope)

    def reset_auto(self, field_keys: Iterable[ChangedKey]) -> list[ChangedKey]:
        """Clear the ``*_override``/pin for *field_keys* and re-derive (design §B).

        A key whose field is ``""`` (or ``"*"``) resets every derived field of
        that row -- what the context menu's "Reset to auto" does on a row
        selection.
        """
        keys = list(field_keys)
        nets = {split_key(row_key)[1] for row_key, _field in keys}
        if not nets:
            return []
        before = self._snapshot(nets)
        for row_key, field in keys:
            kind, net, row_id = split_key(row_key)
            wildcard = field in ("", "*")
            if kind == _KIND_NET:
                cfg = self.nets.get(net)
                if cfg is not None and (wildcard or field == "voltage"):
                    cfg.voltage_override = False
                continue
            row = self.vrm(net, row_id) if kind == _KIND_VRM else self.sink(net, row_id)
            if row is None:
                continue
            overrides = _VRM_OVERRIDES if kind == _KIND_VRM else _SINK_OVERRIDES
            for name, flag in overrides.items():
                if wildcard or field == name:
                    setattr(row, flag, False)
            for name in _PINNABLE:
                if wildcard or field == name:
                    self._pinned.discard((row_key, name))
        self._propagate(nets)
        return self._diff(before, nets)

    # -- row add/remove (design §A: "VRM table + row add/remove") -------- #

    def add_vrm_row(self, net: str) -> str:
        """Add a second/third `.VRM` for *net* (§G.3 `row_id`); returns its row key."""
        cfg = self._require_net(net)
        row_id = self._next_row_id(self._vrms, net)
        self._vrms.append(
            VrmConfig(
                net=net,
                gnet=cfg.paired_gnd,
                comp=self._default_vrm_comp(net, cfg.paired_gnd),
                enabled=cfg.selected,
                row_id=row_id,
            )
        )
        self._sort_rows()
        self.structure_version += 1
        self._propagate([net])
        return vrm_key(net, row_id)

    def add_sink_row(self, net: str) -> str:
        """Add a second/third `.Sink` for *net* (§G.3 `row_id`); returns its row key."""
        cfg = self._require_net(net)
        row_id = self._next_row_id(self._sinks, net)
        self._sinks.append(
            SinkConfig(
                net=net,
                gnet=cfg.paired_gnd,
                comp=self._default_sink_comp(net, cfg.die),
                enabled=cfg.selected,
                row_id=row_id,
            )
        )
        self._sort_rows()
        self.structure_version += 1
        self._propagate([net])
        return sink_key(net, row_id)

    def remove_row(self, row_key: str) -> bool:
        """Drop one VRM/Sink row. The last row of a power net is kept (use
        `set_selected`/``enabled`` to exclude a net from the export)."""
        kind, net, row_id = split_key(row_key)
        if kind == _KIND_NET:
            raise ValueError("net rows cannot be removed; set net_class = 'none'")
        rows: list[Any] = self._vrms if kind == _KIND_VRM else self._sinks
        if sum(1 for row in rows if row.net == net) <= 1:
            return False
        for index, row in enumerate(rows):
            if row.net == net and row.row_id == row_id:
                del rows[index]
                self._pinned = {pin for pin in self._pinned if pin[0] != row_key}
                self.structure_version += 1
                return True
        return False

    # ------------------------------------------------------------------ #
    # Change tracking
    # ------------------------------------------------------------------ #

    def _snapshot(self, nets: Iterable[str] | None = None) -> dict[ChangedKey, object]:
        """Every visible value of the rows in scope, keyed by `ChangedKey`."""
        scope = None if nets is None else set(nets)
        out: dict[ChangedKey, object] = {}
        for net, cfg in self.nets.items():
            if scope is not None and net not in scope:
                continue
            key = net_key(net)
            for field in _NET_FIELDS:
                out[(key, field)] = getattr(cfg, field)
        names = self.display_names()
        for rows, fields, key_of in (
            (self._vrms, _VRM_FIELDS, vrm_key),
            (self._sinks, _SINK_FIELDS, sink_key),
        ):
            for row in rows:
                if scope is not None and row.net not in scope:
                    continue
                key = key_of(row.net, row.row_id)
                for field in fields:
                    out[(key, field)] = getattr(row, field)
                out[(key, "name")] = names.get(key, "")
        return out

    def _diff(
        self, before: dict[ChangedKey, object], nets: Iterable[str] | None = None
    ) -> list[ChangedKey]:
        after = self._snapshot(nets)
        return sorted(
            key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        )

    # ------------------------------------------------------------------ #
    # Validation (design §C export flow, §G.3/§G.4)
    # ------------------------------------------------------------------ #

    def validate(self) -> list[ValidationIssue]:
        """Blocking issues first, then warnings; both carry the nets they implicate."""
        blocking: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        if self.scan is None:
            return [ValidationIssue(True, "No .spd file has been scanned yet.", ())]

        vrms = [row for row in self._vrms if row.enabled]
        sinks = [row for row in self._sinks if row.enabled]
        rows: list[tuple[str, VrmConfig | SinkConfig]] = [
            *(("VRM", row) for row in vrms),
            *(("Sink", row) for row in sinks),
        ]

        # (label -> [(net, "<what> @<comp>")]) so each issue can carry both a
        # readable message and the nets design §C's "uncheck offending rows" needs.
        no_pos: dict[str, list[tuple[str, str]]] = {"VRM": [], "Sink": []}
        no_neg: dict[str, list[tuple[str, str]]] = {"VRM": [], "Sink": []}
        no_gnd: list[str] = []
        bad_v: list[str] = []
        for label, row in rows:
            if not self.positive_pins(row):
                no_pos[label].append((row.net, f"{row.net} @{row.comp or '?'}"))
            gcfg = self.nets.get(row.gnet)
            if not row.gnet or gcfg is None or gcfg.net_class != CLASS_GROUND:
                no_gnd.append(row.net)
            elif not self.negative_pins(row):
                no_neg[label].append((row.net, f"{row.gnet} @{row.comp or '?'}"))
            if row.nominal_voltage <= 0:
                bad_v.append(row.net)
            if isinstance(row, VrmConfig) and row.sense_voltage <= 0:
                bad_v.append(row.net)

        for label in ("VRM", "Sink"):
            for offenders, what in ((no_pos[label], "positive"), (no_neg[label], "ground")):
                if not offenders:
                    continue
                nets = tuple(dict.fromkeys(net for net, _text in offenders))
                blocking.append(
                    ValidationIssue(
                        True,
                        f"{len(offenders)} {label} row(s) have no {what} pins on their "
                        f"component: {_listed(dict.fromkeys(text for _net, text in offenders))}",
                        nets,
                    )
                )
        if no_gnd:
            unique = tuple(dict.fromkeys(no_gnd))
            blocking.append(
                ValidationIssue(
                    True,
                    f"{len(unique)} net(s) have no paired ground net: {_listed(unique)}",
                    unique,
                )
            )
        exported = {row.net for _label, row in rows}
        low = tuple(
            dict.fromkeys(
                [
                    *(
                        net
                        for net in self.power_nets()
                        if net in exported and self.nets[net].voltage <= 0
                    ),
                    *bad_v,
                ]
            )
        )
        if low:
            blocking.append(
                ValidationIssue(True, f"voltage must be > 0: {_listed(low)}", low)
            )

        collisions: dict[str, list[str]] = {}
        for _label, row in rows:
            collisions.setdefault(self.block_name(row), []).append(row.net)
        duplicates = {name: nets for name, nets in collisions.items() if len(nets) > 1}
        if duplicates:
            nets = tuple(dict.fromkeys(net for group in duplicates.values() for net in group))
            listed = _listed(
                f"{name} x{len(group)}" for name, group in sorted(duplicates.items())
            )
            blocking.append(
                ValidationIssue(
                    True,
                    f"duplicate block name(s) -- PowerDC needs them unique: {listed}",
                    nets,
                )
            )

        # -- warnings ------------------------------------------------------ #
        absent = tuple(
            dict.fromkeys(
                row.net
                for _label, row in rows
                if not self.scan.pin_maps.circuits_for(row.net)
            )
        )
        if absent:
            warnings.append(
                ValidationIssue(
                    False,
                    f"{len(absent)} net(s) are absent from the .Connect pin maps "
                    f"(netlist-only nets carry no pins): {_listed(absent)}",
                    absent,
                )
            )
        sense = tuple(
            net
            for net in self._ordered_nets()
            if SENSE_NET_RE.search(net)
            and self.nets[net].net_class != CLASS_NONE
            and self.nets[net].selected
        )
        if sense:
            warnings.append(
                ValidationIssue(
                    False,
                    f"{len(sense)} remote-sense net(s) are classified for export; "
                    f"PowerDC expects them unclassified: {_listed(sense)}",
                    sense,
                )
            )
        if not rows:
            warnings.append(
                ValidationIssue(
                    False,
                    "no VRM/Sink rows are enabled -- the export would add no DC blocks",
                    (),
                )
            )
        for message in (*self.scan.warnings, *self.load_warnings):
            warnings.append(ValidationIssue(False, message, ()))

        return blocking + warnings

    # ------------------------------------------------------------------ #
    # Persistence (design §C Save/Load Config)
    # ------------------------------------------------------------------ #

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the full config -- values *and* override flags (design §C)."""
        payload: dict[str, Any] = {
            "version": CONFIG_VERSION,
            "source": str(self.scan.path) if self.scan is not None else "",
            "nets": [
                {"net": cfg.net, **{field: getattr(cfg, field) for field in _NET_FIELDS}}
                for cfg in self.net_rows
            ],
            "vrms": [self._row_payload(row, _VRM_FIELDS, vrm_key) for row in self._vrms],
            "sinks": [self._row_payload(row, _SINK_FIELDS, sink_key) for row in self._sinks],
        }
        return json.dumps(payload, indent=indent, sort_keys=False)

    def _row_payload(
        self, row: VrmConfig | SinkConfig, fields: tuple[str, ...], key_of: Any
    ) -> dict[str, Any]:
        key = key_of(row.net, row.row_id)
        payload: dict[str, Any] = {"net": row.net, "row_id": row.row_id}
        payload.update({field: getattr(row, field) for field in fields})
        pinned = [name for name in _PINNABLE if (key, name) in self._pinned]
        if pinned:
            payload["pinned"] = pinned
        return payload

    @classmethod
    def from_json(cls, data: str) -> Session:
        """Rebuild a `Session` from `to_json` output (no `ScanResult` attached)."""
        session = cls()
        session.apply_json(data)
        return session

    def apply_json(self, data: str) -> list[ChangedKey]:
        """Overlay a saved config onto this session, keeping any loaded `ScanResult`.

        Unknown keys -- top-level, per-net and per-row -- are ignored, so a config
        written by a newer build still loads. Values are restored verbatim (they
        already *are* the derived values), so no re-derivation runs and every
        override survives the round trip.
        """
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("config JSON must be an object")
        before = self._snapshot()

        self.nets = {}
        self._order = {}
        self._vrms = []
        self._sinks = []
        self._pinned = set()
        self.structure_version += 1

        for item in payload.get("nets") or ():
            if not isinstance(item, dict) or not item.get("net"):
                continue
            cfg = PowerNetConfig(net=str(item["net"]), net_class=CLASS_NONE)
            for field in _NET_FIELDS:
                if field in item:
                    setattr(cfg, field, self._restore(cfg, field, item[field]))
            cfg.class_source = _migrated_class_source(cfg)
            self._add_net(cfg)

        for item in payload.get("vrms") or ():
            row = self._restore_row(item, VrmConfig, _VRM_FIELDS)
            if row is not None:
                self._vrms.append(row)
        for item in payload.get("sinks") or ():
            row = self._restore_row(item, SinkConfig, _SINK_FIELDS)
            if row is not None:
                self._sinks.append(row)

        self._sort_rows()
        return self._diff(before)

    @staticmethod
    def _restore(target: Any, field: str, value: object) -> Any:
        current = getattr(target, field)
        if field == "die":
            return None if value is None else _as_int(value, 0)
        if field == "net_class":
            text = str(value).strip().lower()
            return text if text in _CLASSES else CLASS_NONE
        if field == "class_source":
            text = str(value or "").strip().lower()
            return text if text in _SOURCES else SOURCE_NONE
        if isinstance(current, bool):
            return bool(value)
        if isinstance(current, float):
            return _as_float(value, current)
        if isinstance(current, int):
            return _as_int(value, current)
        return str(value)

    def _restore_row(
        self, item: object, factory: Any, fields: tuple[str, ...]
    ) -> Any | None:
        if not isinstance(item, dict) or not item.get("net"):
            return None
        row = factory(net=str(item["net"]), gnet="", comp="")
        row.row_id = _as_int(item.get("row_id", 0), 0)
        for field in fields:
            if field in item:
                setattr(row, field, self._restore(row, field, item[field]))
        key = (vrm_key if factory is VrmConfig else sink_key)(row.net, row.row_id)
        pinned = item.get("pinned") or ()
        if isinstance(pinned, (list, tuple)):
            for name in pinned:
                if name in _PINNABLE:
                    self._pinned.add((key, str(name)))
        return row

    # ------------------------------------------------------------------ #
    # Export (design §D)
    # ------------------------------------------------------------------ #

    def build_plan(self, opts: Mapping[str, bool | None] | None = None) -> WritePlan:
        """Freeze the current config into a `WritePlan` for `writer.write_spd` (§D).

        *opts* mirrors the design §C export-dialog checkboxes, all default ON:
        ``"other_circuits"``, ``"patch_workflow_key"``, ``"rewrite_netlist"``
        (plus ``"emit_power_voltage"``, default OFF -- spec §2 says power members
        carry no ``Voltage =``).

        ``"other_circuits"`` is tri-state, because `WritePlan.other_circuits` is:

        * ``True`` -- "Emit .OtherCircuit blocks" **ticked**: emit the scanned
          names, i.e. `WritePlan.other_circuits` is a populated tuple, and the
          writer regenerates the run.
        * ``False`` -- the checkbox **unticked**: `()`, i.e. the writer still
          regenerates the run, but from nothing -- it **strips** the blocks.
        * ``None`` -- *no* UI state maps here: `None`, i.e. the writer leaves
          whatever the input carries alone.

        The checkbox is a two-state `QCheckBox`, so the UI only ever produces the
        first two. Unticking it must *remove* the blocks from the output, not
        silently pass through whatever the input happened to carry -- which, for
        an already-DC input, is a full 10 592-line run, the opposite of what the
        user asked for. ``None`` stays reachable for a caller that genuinely
        wants the leave-alone behaviour (`test_writer`'s DC-in/DC-out
        passthrough case builds its `WritePlan` that way).

        The configs are copied, so an export running on a worker thread cannot be
        mutated mid-write by the UI.
        """
        if self.scan is None:
            raise RuntimeError("Session.build_plan() requires a loaded ScanResult")
        options = dict(opts or {})
        scan = self.scan

        vrms = [
            (replace(row), self.positive_pins(row), self.negative_pins(row))
            for row in self._vrms
            if row.enabled
        ]
        sinks = [
            (replace(row), self.positive_pins(row), self.negative_pins(row))
            for row in self._sinks
            if row.enabled
        ]

        netlist_body: str | None = None
        if options.get("rewrite_netlist", True):
            rendered = render_netlist(
                list(scan.nets),
                dict(self.nets),
                emit_power_voltage=bool(options.get("emit_power_voltage", False)),
            )
            # design §G.1 / spec §8: unchanged classification -> copy the original
            # bytes, which is what guarantees the byte-identical `.NetList`.
            if rendered != scan.netlist_body:
                netlist_body = rendered

        # Tri-state (see the docstring table): only an *explicit* None means
        # "leave alone"; a False checkbox means "strip", i.e. an empty tuple.
        emit_other = options.get("other_circuits", True)
        other_circuits: tuple[str, ...] | None
        if emit_other is None:
            other_circuits = None
        elif emit_other:
            other_circuits = tuple(
                name for name in scan.other_circuit_names if OTHER_CIRCUIT_RE.match(name)
            )
        else:
            other_circuits = ()

        return WritePlan(
            vrms=vrms,
            sinks=sinks,
            netlist_body=netlist_body,
            other_circuits=other_circuits,
            patch_workflow_key=bool(options.get("patch_workflow_key", True)),
        )
