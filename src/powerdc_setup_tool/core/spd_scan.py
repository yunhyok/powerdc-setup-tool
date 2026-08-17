"""Single streaming scan pass over a `.spd` file -- owned by chunk 1 (design §A;
spec §9 "Pass 1 - index").

Records byte offsets of every splice point, harvests `.Connect` pin data (spec
ADDENDUM), the netlist body text, and any existing VRM/Sink block headers
(already-DC input, spec §8). Pure stdlib, no Qt.

Memory discipline (design §G.5): the file is opened with a 1 MB buffer and
walked line by line; nothing but the ~300 KB netlist body, the pin index and a
handful of offsets is retained. Every line is first screened by a *bytes*
prefix test; only lines that can possibly matter are decoded, and decoding is
always ``errors="replace"`` (design §G.6 -- decoded text is used for matching
only, never for output).

Resolved ambiguities (see module tests for the pinned behaviour):

* **Node value.** ``PinMapIndex`` stores the *bare* node id (``Node1001``), not
  the whole ``$Package.``-stripped token, because the spec §9 templates spell
  the `.Node` line as ``{node}!!{pin}::{net}`` and rebuild the rest from the
  pin/net context (``core/pdc_gen.py`` and ``tests/fixtures.py`` both do this).
  The two forms are equivalent: spec §7's verified pin-line regex uses a
  backreference for the pin name, so the token is losslessly reconstructable.
* **`CircuitInfo.pin_count`** counts *every* pin line of the `.Connect` block,
  including pins with no ``::net`` suffix (those are skipped for the pin index
  but the component physically has them) -- it is the component's pin count,
  which is what the §A2 "``>= 2`` pins" `.OtherCircuit` filter asks for.
* **`ExistingBlock`** carries ``kind`` in ``{"vrm", "sink"}`` (matching design
  §D's ``NegPinCache`` style keys), ``name`` unquoted, and ``values`` holding
  every ``KEY = VALUE`` pair of the header line *except* ``Name`` (exposed as
  ``.name``). ``comp``/``net``/``gnet`` are read from the block's own first
  `.Map`/`.Node` lines when present -- exact, and immune to the fact that
  ``VRM_{comp}_{pnet}_{gnet}`` cannot be split unambiguously (net names contain
  ``_``) -- falling back to a name split against the known circuits otherwise.
* **`netlist_body`** is LF-normalized (a CRLF input yields the same body text as
  its LF twin). The byte span is still the true source span; output is LF-only
  anyway (design §G.6).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from powerdc_setup_tool.core.model import (
    CircuitInfo,
    ExistingBlock,
    NetEntry,
    PinMapIndex,
    ScanResult,
)
from powerdc_setup_tool.core.netlist import parse_netlist

__all__ = ["SpdFormatError", "scan_spd", "read_region", "read_region_text"]

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Read buffer (design §G.5: "reads sequentially with buffering=1 MB").
_BUFFER_SIZE = 1 << 20

#: Cheap *bytes* gate applied before any decode outside the `.Connect`/`.NetList`
#: runs: directives start with ``.``, comments with ``*``, netlist entries with a
#: TAB. Line 1 (the ``Title WorkflowKey = ...`` line) is handled positionally.
_CANDIDATE_FIRST_BYTES = b".*\t"

#: Progress throttling (design §G.5: "<= 20 Hz").
_PROGRESS_MIN_INTERVAL_S = 0.05
_PROGRESS_MIN_BYTES = 1 << 22

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

#: Anchor patterns applied to *decoded* candidate lines. Directive recognition
#: itself is a token compare (`_is_directive`) rather than a regex, so only the
#: shapes that genuinely need one live here.
_ANCHORS: dict[str, re.Pattern[str]] = {
    # spec §3 anchor A. `\b` keeps `* PdcElemFoo` from matching; the enclosing
    # `[.PowerDC, .EndPowerDC)` test is what rejects the out-of-section decoy.
    "pdc_elem": re.compile(r"^\*\s*PdcElem\b", re.IGNORECASE),
    # spec ADDENDUM: `<pin> $Package.<node>!!<pin>[::<net>]`.
    "connect_pin": re.compile(r"^(\S+)[ \t]+\$Package\.([^!\s]+)!!(\S*)$"),
    # `KEY = VALUE` pairs of a `.VRM`/`.Sink` header line.
    "header_attr": re.compile(r"(\S+)\s*=\s*(\"[^\"]*\"|\S+)"),
    # `.Node Name = <node>!!<pin>::<net>[ Voltage = inf]`.
    "node_name": re.compile(r"^\.Node\s+Name\s*=\s*(\S+)", re.IGNORECASE),
    # `.Map CircuitName = <comp> CircuitPinName = <pin>`.
    "map_circuit": re.compile(r"CircuitName\s*=\s*(\S+)", re.IGNORECASE),
}

#: Line 1's hex token, matched on *raw bytes* so the recorded span is a true
#: byte span even when the title line carries non-ASCII bytes (design §G.6).
_WORKFLOW_KEY_RE = re.compile(rb"WorkflowKey\s*=\s*(\S+)", re.IGNORECASE)

#: design §A2 / §G.2 default `.OtherCircuit` filter. Duplicated (not imported)
#: from `pdc_gen.OTHER_CIRCUIT_RE` to keep `core.spd_scan` free of a dependency
#: on the generator module.
_OTHER_CIRCUIT_RE = re.compile(r"^C\d+_[01]$")

_GROUND_HINT = "Negative Pin"
_POWER_HINT = "Positive Pin"


class SpdFormatError(ValueError):
    """The file is not a usable `.spd` (a mandatory section/anchor is missing).

    Subclasses :class:`ValueError` so callers that only catch ``ValueError``
    (design §C's export/scan error handling) still work.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    """Decode one raw line for *matching only* (design §G.6)."""
    return raw.decode("utf-8", errors="replace")


def _strip_eol(text: str) -> str:
    """Drop a trailing ``\\n``/``\\r\\n`` (and any trailing blanks) from *text*."""
    return text.rstrip("\r\n").rstrip()


def _is_directive(text: str, name: str) -> bool:
    """True when *text* starts with the directive *name* as a whole token.

    Case-insensitive (design §E "mixed-case directives"), and boundary-checked
    so ``.PowerDCFoo`` does not match ``.PowerDC`` and ``.EndCTE`` does not
    match ``.EndC`` (spec ADDENDUM).
    """
    if len(text) < len(name):
        return False
    if text[: len(name)].lower() != name:
        return False
    rest = text[len(name) :]
    return not rest or rest[0] in " \t"


def _split_header(text: str) -> tuple[str, dict[str, str]]:
    """``.VRM K = V ... Name = "X"`` -> (unquoted name, {K: V} without ``Name``)."""
    values: dict[str, str] = {}
    name = ""
    for key, value in _ANCHORS["header_attr"].findall(text):
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if key.lower() == "name":
            name = value
        else:
            values[key] = value
    return name, values


def _split_block_name(
    name: str, prefix: str, circuits: dict[str, int], nets: set[str]
) -> tuple[str, str, str]:
    """Best-effort ``{prefix}{comp}_{pnet}_{gnet}`` split (spec §6).

    Only a fallback: the split is genuinely ambiguous (net names contain ``_``),
    so `scan_spd` prefers the block's own `.Map`/`.Node` lines. Uses the already
    harvested `.Connect` circuit and net names -- they precede `.PowerDC` in the
    file (spec §1) -- to disambiguate, then falls back to a plain rsplit.
    """
    if not name.lower().startswith(prefix.lower()):
        return "", "", ""
    rest = name[len(prefix) :]

    comp = ""
    for candidate in circuits:
        if rest.startswith(candidate + "_") and len(candidate) > len(comp):
            comp = candidate
    if not comp:
        comp, _, rest_after = rest.partition("_")
        rest = rest_after
    else:
        rest = rest[len(comp) + 1 :]

    pnet = ""
    for candidate in nets:
        if rest.startswith(candidate + "_") and len(candidate) > len(pnet):
            pnet = candidate
    if pnet:
        return comp, pnet, rest[len(pnet) + 1 :]
    pnet, _, gnet = rest.rpartition("_")
    return comp, pnet, gnet


class _BlockState:
    """Mutable accumulator for the `.VRM`/`.Sink` block currently being walked."""

    __slots__ = ("kind", "name", "values", "comp", "net", "gnet", "section", "need")

    def __init__(self, kind: str, name: str, values: dict[str, str]) -> None:
        self.kind = kind
        self.name = name
        self.values = values
        self.comp = ""
        self.net = ""
        self.gnet = ""
        self.section = ""
        self.need = True

    def refresh_need(self) -> None:
        self.need = not (self.comp and self.net and self.gnet)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def scan_spd(path: Path, progress: Callable[[int, int], None] | None = None) -> ScanResult:
    """Sequential single pass over *path*; never loads the whole file into memory.

    *progress*, if given, is called with ``(bytes_read, file_size)`` throttled
    to <=20 Hz (design §G.5); it always fires once at 0 and once at EOF.

    Raises :class:`SpdFormatError` when a section the writer must splice into is
    absent (``.PowerDC``/``.EndPowerDC``, the ``* PdcElem`` anchor, the
    ``.EndSpiceNetlist`` anchor, ``.NetList``/``.EndNetList``). Softer oddities
    land in :attr:`ScanResult.warnings`.
    """
    path = Path(path)
    started = time.monotonic()
    file_size = path.stat().st_size

    warnings: list[str] = []

    # -- offsets ----------------------------------------------------------- #
    workflow_key_span: tuple[int, int] | None = None
    powerdc_start = -1
    powerdc_end = -1
    anchor_pdc_elem_end = -1
    anchor_spice_end = -1
    netlist_start = -1
    netlist_body_start = -1
    netlist_body_end = -1
    netlist_end = -1
    last_block_end = -1

    # -- harvest ----------------------------------------------------------- #
    by_circuit: dict[str, dict[str, list[tuple[str, str]]]] = {}
    circuit_pins: dict[str, int] = {}
    circuit_order: list[str] = []
    seen_nets: set[str] = set()
    body_parts: list[str] = []
    existing_vrms: list[ExistingBlock] = []
    existing_sinks: list[ExistingBlock] = []

    # -- state ------------------------------------------------------------- #
    in_powerdc = False
    in_netlist = False
    connect_circuit: str | None = None
    block: _BlockState | None = None
    non_ascii_seen = False

    # -- progress ---------------------------------------------------------- #
    next_progress_at = _PROGRESS_MIN_BYTES
    last_progress_t = started
    if progress is not None:
        progress(0, file_size)

    def _finish_block(end_offset: int) -> None:
        nonlocal block, last_block_end
        assert block is not None
        comp, net, gnet = block.comp, block.net, block.gnet
        if not (comp and net and gnet):
            prefix = "VRM_" if block.kind == "vrm" else "SINK_"
            f_comp, f_net, f_gnet = _split_block_name(
                block.name, prefix, circuit_pins, seen_nets
            )
            comp = comp or f_comp
            net = net or f_net
            gnet = gnet or f_gnet
        entry = ExistingBlock(
            kind=block.kind,
            name=block.name,
            net=net,
            gnet=gnet,
            comp=comp,
            values=block.values,
        )
        (existing_vrms if block.kind == "vrm" else existing_sinks).append(entry)
        last_block_end = end_offset
        block = None

    with open(path, "rb", buffering=_BUFFER_SIZE) as handle:
        offset = 0
        for lineno, raw in enumerate(handle):
            start = offset
            offset += len(raw)

            if progress is not None and offset >= next_progress_at:
                now = time.monotonic()
                if now - last_progress_t >= _PROGRESS_MIN_INTERVAL_S:
                    progress(offset, file_size)
                    last_progress_t = now
                next_progress_at = offset + _PROGRESS_MIN_BYTES

            # -- line 1: the WorkflowKey token (spec §8 row 1) -------------- #
            if lineno == 0:
                match = _WORKFLOW_KEY_RE.search(raw)
                if match is not None:
                    workflow_key_span = (start + match.start(1), start + match.end(1))
                else:
                    warnings.append("no WorkflowKey token on line 1")
                continue

            # -- inside a `.Connect` block: every line is a pin line -------- #
            if connect_circuit is not None:
                if raw[:1] == b".":
                    text = _strip_eol(_decode(raw))
                    if text.lower() == ".endc":  # exact: `.EndCTE` must not match
                        connect_circuit = None
                        continue
                    if _is_directive(text, ".compcollection") or _is_directive(
                        text, ".connect"
                    ):
                        # Unterminated block: recover rather than swallow the file.
                        warnings.append(
                            f".Connect block {connect_circuit!r} is not terminated by .EndC"
                        )
                        connect_circuit = None
                        # fall through to the generic dispatch below
                    else:
                        continue
                else:
                    if raw.strip():
                        pin_match = _ANCHORS["connect_pin"].match(_strip_eol(_decode(raw)))
                        if pin_match is not None:
                            circuit_pins[connect_circuit] += 1
                            pin, node, tail = pin_match.groups()
                            net = tail.partition("::")[2]
                            if net:  # unconnected pins carry no `::net` -- skip
                                by_circuit[connect_circuit].setdefault(net, []).append(
                                    (pin, node)
                                )
                                seen_nets.add(net)
                                if not non_ascii_seen and not net.isascii():
                                    non_ascii_seen = True
                                    warnings.append(f"non-ASCII net name: {net!r}")
                    continue

            # -- inside `.NetList`: collect the body verbatim (LF-normalized) #
            if in_netlist:
                if raw[:1] == b".":
                    text = _strip_eol(_decode(raw))
                    if _is_directive(text, ".endnetlist"):
                        in_netlist = False
                        netlist_body_end = start
                        netlist_end = offset
                        continue
                decoded = _decode(raw)
                if decoded.endswith("\r\n"):
                    decoded = decoded[:-2] + "\n"
                body_parts.append(decoded)
                continue

            # -- inside a `.VRM`/`.Sink` block: cheap bytes screening ------- #
            if block is not None:
                head = raw[:5].lower()
                if head in (b".endv", b".ends", b".endp"):
                    text = _strip_eol(_decode(raw))
                    if _is_directive(text, ".endvrm") or _is_directive(text, ".endsink"):
                        _finish_block(offset)
                        continue
                    if _is_directive(text, ".endpowerdc"):
                        # Unterminated block: recover and let the generic
                        # dispatch below close the `.PowerDC` section.
                        warnings.append(
                            f"unterminated .{block.kind.upper()} block {block.name!r}"
                        )
                        block = None
                    else:
                        # `.EndSinkCurrentSource` is a different directive.
                        continue
                elif block.need and head in (b".node", b".map ", b".pin "):
                    _harvest_block_line(block, _strip_eol(_decode(raw)))
                    continue
                else:
                    continue

            # -- generic dispatch: cheap bytes gate, then decode ------------ #
            if raw[:1] not in _CANDIDATE_FIRST_BYTES:
                continue
            text = _strip_eol(_decode(raw))

            if text[:1] == "*":
                if (
                    in_powerdc
                    and anchor_pdc_elem_end < 0
                    and _ANCHORS["pdc_elem"].match(text)
                ):
                    anchor_pdc_elem_end = offset
                continue
            if text[:1] != ".":
                continue

            if _is_directive(text, ".connect"):
                parts = text.split()
                name = parts[1] if len(parts) > 1 else ""
                if not name:
                    warnings.append(".Connect block with no RefDes token")
                    continue
                if name in by_circuit:
                    warnings.append(f"duplicate .Connect RefDes: {name!r}")
                else:
                    by_circuit[name] = {}
                    circuit_pins[name] = 0
                    circuit_order.append(name)
                    if not non_ascii_seen and not name.isascii():
                        non_ascii_seen = True
                        warnings.append(f"non-ASCII circuit name: {name!r}")
                connect_circuit = name
                continue

            if _is_directive(text, ".powerdc"):
                if powerdc_start < 0:
                    powerdc_start = start
                in_powerdc = True
                continue
            if _is_directive(text, ".endpowerdc"):
                if in_powerdc and powerdc_end < 0:
                    powerdc_end = start
                in_powerdc = False
                continue

            if in_powerdc:
                if anchor_spice_end < 0 and _is_directive(text, ".endspicenetlist"):
                    anchor_spice_end = offset
                    continue
                if _is_directive(text, ".vrm") or _is_directive(text, ".sink"):
                    kind = "vrm" if text[:4].lower() == ".vrm" else "sink"
                    name, values = _split_header(text)
                    block = _BlockState(kind, name, values)
                    continue

            if _is_directive(text, ".netlist"):
                if netlist_start < 0:
                    netlist_start = start
                    netlist_body_start = offset
                    in_netlist = True
                continue

    if progress is not None:
        progress(file_size, file_size)

    if connect_circuit is not None:
        warnings.append(f".Connect block {connect_circuit!r} is not terminated by .EndC")
    if block is not None:
        warnings.append(f"unterminated .{block.kind.upper()} block {block.name!r}")

    # -- mandatory structure ----------------------------------------------- #
    if powerdc_start < 0:
        raise SpdFormatError(f"{path}: no .PowerDC section")
    if powerdc_end < 0:
        raise SpdFormatError(f"{path}: .PowerDC section is not closed by .EndPowerDC")
    if anchor_pdc_elem_end < 0:
        raise SpdFormatError(
            f"{path}: no '* PdcElem description lines' comment inside .PowerDC"
        )
    if anchor_spice_end < 0:
        raise SpdFormatError(f"{path}: no .EndSpiceNetlist inside .PowerDC")
    if netlist_start < 0:
        raise SpdFormatError(f"{path}: no .NetList section")
    if netlist_end < 0:
        raise SpdFormatError(f"{path}: .NetList section is not closed by .EndNetList")

    netlist_body = "".join(body_parts)
    nets: tuple[NetEntry, ...] = tuple(parse_netlist(netlist_body))

    pin_maps = PinMapIndex(
        by_circuit={
            circuit: {net: tuple(pins) for net, pins in per_net.items()}
            for circuit, per_net in by_circuit.items()
        }
    )
    circuits = tuple(CircuitInfo(name=name, pin_count=circuit_pins[name]) for name in circuit_order)
    other_circuit_names = tuple(
        info.name
        for info in circuits
        if info.pin_count >= 2 and _OTHER_CIRCUIT_RE.match(info.name)
    )

    existing_blocks_span: tuple[int, int] | None = None
    if last_block_end > anchor_spice_end:
        # design §D step 6 / §G.2: the whole existing VRM+Sink run is skipped by
        # the writer and regenerated wholesale, so the span starts at anchor B.
        existing_blocks_span = (anchor_spice_end, last_block_end)
    elif last_block_end > 0:
        warnings.append("VRM/Sink blocks found outside the .EndSpiceNetlist anchor")

    return ScanResult(
        path=path,
        file_size=file_size,
        elapsed_s=time.monotonic() - started,
        workflow_key_span=workflow_key_span,
        powerdc_span=(powerdc_start, powerdc_end),
        anchor_pdc_elem_end=anchor_pdc_elem_end,
        anchor_spice_end=anchor_spice_end,
        existing_blocks_span=existing_blocks_span,
        netlist_span=(netlist_start, netlist_end),
        netlist_body_span=(netlist_body_start, netlist_body_end),
        netlist_body=netlist_body,
        nets=nets,
        pin_maps=pin_maps,
        circuits=circuits,
        other_circuit_names=other_circuit_names,
        existing_vrms=tuple(existing_vrms),
        existing_sinks=tuple(existing_sinks),
        warnings=tuple(warnings),
    )


def _harvest_block_line(block: _BlockState, text: str) -> None:
    """Pull ``comp``/``net``/``gnet`` out of a `.VRM`/`.Sink` body line.

    The first `.Map` gives the circuit name; the first `.Node` of the
    ``"Positive Pin"`` / ``"Negative Pin"`` sections gives the power / ground
    net (spec §4/§5). Sense sections are empty, so they contribute nothing.
    """
    if _is_directive(text, ".pin"):
        if f'"{_POWER_HINT}"' in text:
            block.section = "pos"
        elif f'"{_GROUND_HINT}"' in text:
            block.section = "neg"
        else:
            block.section = ""
        return
    if _is_directive(text, ".map"):
        if not block.comp:
            match = _ANCHORS["map_circuit"].search(text)
            if match is not None:
                block.comp = match.group(1)
                block.refresh_need()
        return
    if _is_directive(text, ".node"):
        match = _ANCHORS["node_name"].match(text)
        if match is None:
            return
        net = match.group(1).partition("::")[2]
        if not net:
            return
        if block.section == "pos" and not block.net:
            block.net = net
        elif block.section == "neg" and not block.gnet:
            block.gnet = net
        block.refresh_need()


def read_region(path: Path, start: int, end: int) -> bytes:
    """Return the raw bytes of ``[start, end)`` without holding the file in RAM.

    Lazy, on-demand re-read of a previously indexed span -- e.g. for the
    two-phase-scan fallback in design §G.5. Raw bytes (not text) so callers can
    byte-compare a span against generated output; :func:`read_region_text` is
    the decoding twin.
    """
    if start < 0 or end < start:
        raise ValueError(f"invalid region [{start}, {end})")
    if end == start:
        return b""
    with open(path, "rb", buffering=_BUFFER_SIZE) as handle:
        handle.seek(start)
        return handle.read(end - start)


def read_region_text(path: Path, start: int, end: int) -> str:
    """:func:`read_region` decoded as UTF-8 with ``errors="replace"`` (design §G.6)."""
    return read_region(path, start, end).decode("utf-8", errors="replace")
