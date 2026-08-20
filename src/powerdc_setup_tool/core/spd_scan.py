"""Single streaming scan pass over a `.spd` file -- owned by chunk 1 (design §A;
spec §9 "Pass 1 - index").

Records byte offsets of every splice point, harvests `.Connect` pin data (spec
ADDENDUM), the netlist body text, and any existing VRM/Sink block headers
(already-DC input, spec §8). Pure stdlib, no Qt.

Memory discipline (design §G.5): the file is opened with a 1 MB buffer and
walked line by line; nothing but the ~300 KB netlist body, the pin index and a
handful of offsets is retained. Every line is first screened by a *bytes*
prefix test; only lines that can possibly matter are decoded.

Decoding is always ``errors="surrogateescape"``, **not** ``errors="replace"``:
`netlist_body` is the one decoded string that can become output (the writer
emits it whenever the classification changed, design §D step 8), and
``"replace"`` is lossy -- a stray ``\\xb5`` in a net name would be written back
out as ``U+FFFD``, mangling a line the user never touched. ``surrogateescape``
maps every undecodable byte to a lone surrogate that
``encode("utf-8", errors="surrogateescape")`` turns back into the original
byte, so untouched entries round-trip byte-exactly (`core/writer.py` encodes
all generated text the same way). The same codec is used for the `.Connect`
pin harvest so a non-UTF-8 net name is spelled identically in the pin index and
in the netlist -- with two different error handlers the two would not compare
equal and the net would silently show zero pins.

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
  `.Map`/`.Node` lines and are checked against the authoritative `.Connect`
  pin/node/net index; empty or incomplete Positive/Negative sections fail
  closed rather than being inferred from the ambiguous block name.
* **`netlist_body`** is LF-normalized (a CRLF input yields the same body text as
  its LF twin). The byte span is still the true source span; output is LF-only
  anyway (design §G.6).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from powerdc_setup_tool.core.model import (
    CircuitInfo,
    ExistingBlock,
    NetEntry,
    PinMapIndex,
    ScanResult,
    SourceIdentity,
)
from powerdc_setup_tool.core import naming
from powerdc_setup_tool.core.netlist import NetlistFormatError, parse_netlist
from powerdc_setup_tool.core.pdc_gen import is_other_circuit

__all__ = [
    "ScanCancelled",
    "SpdFormatError",
    "scan_spd",
    "read_region",
    "read_region_text",
]

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

#: Cancel polling granularity, in lines. A 1.4 GB file is ~25 M lines, so the
#: `threading.Event` is consulted a few thousand times across a full scan:
#: often enough to abort within milliseconds of the user asking, seldom enough
#: that `Event.is_set()` stays invisible in the design §G.5 scan budget.
#: Must stay a power of two -- the poll uses a bitmask, not a modulo.
_CANCEL_POLL_LINES = 4096
_CANCEL_POLL_MASK = _CANCEL_POLL_LINES - 1

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
    "connect_pin": re.compile(
        r"^(\S+)[ \t]+\$Package\.(Node[^!\s]*)!!([^:\s]+)(?:::(\S+))?$"
    ),
}

#: Line 1's hex token, matched on *raw bytes* so the recorded span is a true
#: byte span even when the title line carries non-ASCII bytes (design §G.6).
_WORKFLOW_KEY_RE = re.compile(rb"WorkflowKey\s*=\s*(\S+)", re.IGNORECASE)

_HEADER_VALUE_RE = re.compile(r'"[^"]*"|\S+')
_HEADER_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_NODE_VALUE_RE = re.compile(r"^(?P<node>[^!\s]+)!!(?P<pin>[^:\s]+)::(?P<net>\S+)$")

_BLOCK_HEADER_KEYS: dict[str, tuple[str, ...]] = {
    "vrm": ("NominalVoltage", "SenseVoltage", "OutputCurrent", "Name"),
    "sink": ("NominalVoltage", "Current", "Model", "PFMode", "PinEqualCurrent", "Name"),
}
_BLOCK_FLOAT_KEYS: dict[str, tuple[str, ...]] = {
    "vrm": ("NominalVoltage", "SenseVoltage", "OutputCurrent"),
    "sink": ("NominalVoltage", "Current"),
}
_BLOCK_INT_KEYS: dict[str, tuple[str, ...]] = {
    "vrm": (),
    "sink": ("Model", "PFMode", "PinEqualCurrent"),
}
_BLOCK_PIN_SECTIONS: dict[str, tuple[str, ...]] = {
    "vrm": ("Positive Pin", "Negative Pin", "Positive Sense Pin", "Negative Sense Pin"),
    "sink": ("Positive Pin", "Negative Pin"),
}


class SpdFormatError(ValueError):
    """The file is not a usable `.spd` (a mandatory section/anchor is missing).

    Subclasses :class:`ValueError` so callers that only catch ``ValueError``
    (design §C's export/scan error handling) still work.
    """


class ScanCancelled(RuntimeError):
    """Raised by `scan_spd` when its `cancel` event fires; no `ScanResult` is produced.

    The scan-side twin of `writer.WriteCancelled` (same base class, same
    "the caller asked, this is not an error" contract): `ui.workers.ScanWorker`
    reports it through `failed`, and `ui.main_window` suppresses the error
    dialog for it, exactly as it already does for a cancelled export.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    """Decode one raw line losslessly (design §G.6; see the module docstring).

    ``surrogateescape`` keeps undecodable bytes recoverable, so the decoded
    `netlist_body` can be re-emitted byte-exactly by the writer.
    """
    return raw.decode("utf-8", errors="surrogateescape")


def _source_identity(path: Path) -> SourceIdentity:
    """Capture the identity fields used to reject stale scan offsets."""
    stat = path.stat()
    return SourceIdentity(
        resolved_path=str(path.resolve(strict=False)),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        st_dev=getattr(stat, "st_dev", None),
        st_ino=getattr(stat, "st_ino", None),
    )


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
    if text[: len(name)].casefold() != name.casefold():
        return False
    rest = text[len(name) :]
    return not rest or rest[0] in " \t"


def _is_bare_directive(text: str, name: str) -> bool:
    """True for a directive with no trailing attributes or payload."""
    return _is_directive(text, name) and not text[len(name) :].strip()


def _parse_key_value_pairs(text: str, *, context: str) -> dict[str, str]:
    """Parse a complete whitespace-separated ``Key = Value`` sequence.

    The old scanner used a permissive ``findall`` here.  That silently skipped
    unknown/truncated attributes, which is unsafe when the writer later drops
    and regenerates the whole block.  This helper consumes every byte of the
    sequence and reports the first malformed token instead.
    """
    values: dict[str, str] = {}
    seen_keys: set[str] = set()
    pos = 0
    length = len(text)
    while pos < length:
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            break
        key_match = _HEADER_KEY_RE.match(text, pos)
        if key_match is None:
            raise SpdFormatError(f"malformed {context} attribute near {text[pos:]!r}")
        key = key_match.group(0)
        folded_key = key.casefold()
        if folded_key in seen_keys:
            raise SpdFormatError(f"duplicate {context} attribute {key!r}")
        seen_keys.add(folded_key)
        pos = key_match.end()
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length or text[pos] != "=":
            raise SpdFormatError(f"malformed {context} attribute {key!r}: missing '='")
        pos += 1
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            raise SpdFormatError(f"malformed {context} attribute {key!r}: missing value")
        value_match = _HEADER_VALUE_RE.match(text, pos)
        if value_match is None:
            raise SpdFormatError(f"malformed {context} attribute {key!r}: missing value")
        value = value_match.group(0)
        pos = value_match.end()
        if value.startswith('"') != value.endswith('"'):
            raise SpdFormatError(
                f"malformed {context} attribute {key!r}: unbalanced quotes"
            )
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    return values


def _parse_block_header(text: str, kind: str) -> tuple[str, dict[str, str]]:
    """Validate and split a complete `.VRM`/`.Sink` header.

    Header keys are case-insensitive for compatibility with PowerSI's mixed
    directive casing, but duplicate and unknown keys are rejected because the
    replacement writer cannot preserve them.  Numeric fields are validated
    now, before an existing block span is exposed to the writer.
    """
    directive = ".VRM" if kind == "vrm" else ".Sink"
    if not _is_directive(text, directive):
        raise SpdFormatError(f"malformed .{kind.upper()} header: {text!r}")
    attrs = _parse_key_value_pairs(text[len(directive) :], context=f".{kind.upper()}")
    allowed = {key.casefold(): key for key in _BLOCK_HEADER_KEYS[kind]}
    canonical: dict[str, str] = {}
    for key, value in attrs.items():
        canonical_key = allowed.get(key.casefold())
        if canonical_key is None:
            raise SpdFormatError(
                f"unknown .{kind.upper()} header key {key!r}; block cannot be preserved"
            )
        if canonical_key in canonical:
            raise SpdFormatError(f"duplicate .{kind.upper()} header key {key!r}")
        if not value:
            raise SpdFormatError(f"empty .{kind.upper()} header value for {key!r}")
        canonical[canonical_key] = value
    required = _BLOCK_HEADER_KEYS[kind]
    missing = [key for key in required if key not in canonical]
    if missing:
        raise SpdFormatError(
            f".{kind.upper()} header is missing required key(s): {', '.join(missing)}"
        )
    for key in _BLOCK_FLOAT_KEYS[kind]:
        try:
            number = float(canonical[key])
        except (TypeError, ValueError) as exc:
            raise SpdFormatError(
                f"invalid numeric value for .{kind.upper()} {key}: {canonical[key]!r}"
            ) from exc
        if not math.isfinite(number):
            raise SpdFormatError(
                f"non-finite numeric value for .{kind.upper()} {key}: {canonical[key]!r}"
            )
    for key in _BLOCK_INT_KEYS[kind]:
        if re.fullmatch(r"[+-]?\d+", canonical[key]) is None:
            raise SpdFormatError(
                f"invalid integer value for .{kind.upper()} {key}: {canonical[key]!r}"
            )
    name = canonical.pop("Name")
    if not name:
        raise SpdFormatError(f".{kind.upper()} header Name must not be empty")
    return name, canonical


def _parse_pin_name(text: str, *, kind: str) -> str:
    """Validate a `.Pin Name = "..."` section header and return its name."""
    if not _is_directive(text, ".Pin"):
        raise SpdFormatError(f"malformed .{kind.upper()} pin section: {text!r}")
    payload = text[len(".Pin") :].strip()
    if re.fullmatch(r'Name\s*=\s*"[^"]*"', payload, flags=re.IGNORECASE) is None:
        raise SpdFormatError(f"malformed .{kind.upper()} .Pin header: {text!r}")
    attrs = _parse_key_value_pairs(text[len(".Pin") :], context=f".{kind.upper()} .Pin")
    if set(key.casefold() for key in attrs) != {"name"}:
        raise SpdFormatError(f"malformed .{kind.upper()} .Pin header: {text!r}")
    # `_parse_key_value_pairs` accepts unquoted values for ordinary attrs, but
    # section names are intentionally quoted in the production grammar.  The
    # shape check above keeps malformed or unbalanced names out of the state
    # machine before this value is compared with the expected section.
    name = next(iter(attrs.values()))
    if not name:
        raise SpdFormatError(f"empty .{kind.upper()} .Pin Name")
    return name


def _parse_map_header(text: str, *, kind: str) -> tuple[str, str]:
    """Validate `.Map CircuitName = ... CircuitPinName = ...`."""
    if not _is_directive(text, ".Map"):
        raise SpdFormatError(f"malformed .{kind.upper()} .Map header: {text!r}")
    attrs = _parse_key_value_pairs(text[len(".Map") :], context=f".{kind.upper()} .Map")
    canonical: dict[str, str] = {}
    allowed = {"circuitname": "CircuitName", "circuitpinname": "CircuitPinName"}
    for key, value in attrs.items():
        canonical_key = allowed.get(key.casefold())
        if canonical_key is None:
            raise SpdFormatError(f"unknown .{kind.upper()} .Map key {key!r}")
        if canonical_key in canonical:
            raise SpdFormatError(f"duplicate .{kind.upper()} .Map key {key!r}")
        if not value:
            raise SpdFormatError(f"empty .{kind.upper()} .Map value for {key!r}")
        canonical[canonical_key] = value
    if set(canonical) != {"CircuitName", "CircuitPinName"}:
        raise SpdFormatError(f"incomplete .{kind.upper()} .Map header: {text!r}")
    return canonical["CircuitName"], canonical["CircuitPinName"]


def _parse_node_line(
    text: str, *, kind: str, map_pin: str, expect_voltage_inf: bool
) -> tuple[str, str]:
    """Validate a `.Node` line and return `(node_id, net_name)`."""
    if not _is_directive(text, ".Node"):
        raise SpdFormatError(f"expected .Node after .Map in .{kind.upper()} block")
    attrs = _parse_key_value_pairs(text[len(".Node") :], context=f".{kind.upper()} .Node")
    canonical: dict[str, str] = {}
    allowed = {"name": "Name", "voltage": "Voltage"}
    for key, value in attrs.items():
        canonical_key = allowed.get(key.casefold())
        if canonical_key is None:
            raise SpdFormatError(f"unknown .{kind.upper()} .Node key {key!r}")
        if canonical_key in canonical:
            raise SpdFormatError(f"duplicate .{kind.upper()} .Node key {key!r}")
        canonical[canonical_key] = value
    if "Name" not in canonical:
        raise SpdFormatError(f".{kind.upper()} .Node is missing Name")
    has_inf = canonical.get("Voltage", "").casefold() == "inf"
    if kind == "sink" and has_inf != expect_voltage_inf:
        raise SpdFormatError("Sink .Node must carry `Voltage = inf`")
    if kind == "vrm" and "Voltage" in canonical:
        raise SpdFormatError("VRM .Node must not carry a Voltage attribute")
    match = _NODE_VALUE_RE.fullmatch(canonical["Name"])
    if match is None:
        raise SpdFormatError(f"malformed .{kind.upper()} .Node Name: {canonical['Name']!r}")
    if match.group("pin") != map_pin:
        raise SpdFormatError(
            f".{kind.upper()} .Node pin {match.group('pin')!r} does not match .Map pin {map_pin!r}"
        )
    return match.group("node"), match.group("net")


class _BlockState:
    """Mutable accumulator for the `.VRM`/`.Sink` block currently being walked."""

    __slots__ = (
        "kind",
        "name",
        "values",
        "comp",
        "net",
        "gnet",
        "section",
        "expected_sections",
        "section_index",
        "map_state",
        "map_pin",
        "map_comp",
        "map_pins",
        "section_maps",
        "sink_source_state",
    )

    def __init__(self, kind: str, name: str, values: dict[str, str]) -> None:
        self.kind = kind
        self.name = name
        self.values = values
        self.comp = ""
        self.net = ""
        self.gnet = ""
        self.section = ""
        self.expected_sections = _BLOCK_PIN_SECTIONS[kind]
        self.section_index = -1
        self.map_state = "idle"
        self.map_pin = ""
        self.map_comp = ""
        self.map_pins: set[str] = set()
        self.section_maps: list[tuple[str, str]] = []
        self.sink_source_state = "absent"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def scan_spd(
    path: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> ScanResult:
    """Sequential single pass over *path*; never loads the whole file into memory.

    *progress*, if given, is called with ``(bytes_read, file_size)`` throttled
    to <=20 Hz (design §G.5); it always fires once at 0 and once at EOF.

    *cancel*, if given, is a `threading.Event` polled every
    `_CANCEL_POLL_LINES` lines (and once before the file is opened); when it is
    set the scan raises :class:`ScanCancelled` instead of finishing. This is the
    scan-side twin of `writer.write_spd`'s `cancel` hook -- a scan of the real
    1.4 GB input takes tens of seconds, which is far too long for a window close
    to block on, and a `QThread` torn down mid-run aborts the process.

    Raises :class:`SpdFormatError` when a section the writer must splice into is
    absent (``.PowerDC``/``.EndPowerDC``, the ``* PdcElem`` anchor, the
    ``.EndSpiceNetlist`` anchor, ``.NetList``/``.EndNetList``). Softer oddities
    land in :attr:`ScanResult.warnings`.
    """
    path = Path(path)
    if cancel is not None and cancel.is_set():
        raise ScanCancelled(f"Scan of {path.name} cancelled before it started.")
    started = time.monotonic()
    source_identity = _source_identity(path)
    file_size = source_identity.size

    warnings: list[str] = []

    # -- offsets ----------------------------------------------------------- #
    workflow_key_span: tuple[int, int] | None = None
    powerdc_start = -1
    powerdc_end = -1
    powerdc_starts: list[int] = []
    powerdc_ends: list[int] = []
    anchor_pdc_elem_end = -1
    anchor_spice_end = -1
    pdc_elem_positions: list[int] = []
    spice_end_positions: list[int] = []
    netlist_start = -1
    netlist_body_start = -1
    netlist_body_end = -1
    netlist_end = -1
    netlist_starts: list[int] = []
    netlist_ends: list[int] = []
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
    connect_pins_seen: set[str] | None = None
    block: _BlockState | None = None
    block_run_started = False
    block_run_ended = False
    block_pre_gap = False
    block_last_kind: str | None = None
    non_ascii_seen = False

    # -- progress ---------------------------------------------------------- #
    next_progress_at = _PROGRESS_MIN_BYTES
    last_progress_t = started
    if progress is not None:
        progress(0, file_size)

    def _finish_block(end_offset: int) -> None:
        nonlocal block, last_block_end, block_last_kind
        assert block is not None
        comp, net, gnet = block.comp, block.net, block.gnet
        if not (comp and net and gnet):
            raise SpdFormatError(
                f"{path}: {block.kind.upper()} block {block.name!r} is missing "
                "a component, positive-net, or ground-net identity"
            )
        expected_name = (
            naming.vrm_name(comp, net, gnet)
            if block.kind == "vrm"
            else naming.sink_name(comp, net, gnet)
        )
        if block.name != expected_name:
            raise SpdFormatError(
                f"{path}: {block.kind.upper()} block name {block.name!r} does not "
                f"match canonical name {expected_name!r}"
            )
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
        block_last_kind = block.kind
        block = None

    with open(path, "rb", buffering=_BUFFER_SIZE) as handle:
        offset = 0
        for lineno, raw in enumerate(handle):
            start = offset
            offset += len(raw)

            # Cheap cancel poll: one bitmask test per line, an `Event.is_set()`
            # only once per batch (see `_CANCEL_POLL_LINES`).
            if cancel is not None and (lineno & _CANCEL_POLL_MASK) == 0 and cancel.is_set():
                raise ScanCancelled(f"Scan of {path.name} cancelled.")

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
                    if _is_bare_directive(text, ".endc"):  # `.EndCTE` must not match
                        connect_circuit = None
                        connect_pins_seen = None
                        continue
                    # A second block header or component collection before
                    # `.EndC` means the first block is unterminated.  An
                    # unrelated directive such as the `.EndCTE` thermal decoy
                    # is allowed in legacy exports and must not be mistaken
                    # for `.EndC`.
                    if _is_directive(text, ".connect") or _is_directive(
                        text, ".compcollection"
                    ):
                        raise SpdFormatError(
                            f"{path}: .Connect block {connect_circuit!r} is not "
                            f"terminated by .EndC before {text!r}"
                        )
                    continue
                else:
                    if raw.strip():
                        pin_match = _ANCHORS["connect_pin"].match(_strip_eol(_decode(raw)))
                        if pin_match is None:
                            # Thermal/constraint decoys can legally occur in
                            # a Connect run (the existing fixture exercises
                            # this).  Fail closed only when a line claims to
                            # be a package pin but violates the grammar.
                            if b"$Package." in raw or b"!!" in raw:
                                raise SpdFormatError(
                                    f"{path}: malformed .Connect pin mapping in "
                                    f"{connect_circuit!r}: {_strip_eol(_decode(raw))!r}"
                                )
                            continue
                        assert connect_pins_seen is not None
                        pin, node, inner_pin, net = pin_match.groups()
                        if not node or not inner_pin or pin != inner_pin:
                            raise SpdFormatError(
                                f"{path}: malformed .Connect pin mapping in "
                                f"{connect_circuit!r}: outer pin {pin!r} "
                                f"does not match inner pin {inner_pin!r}"
                            )
                        if pin in connect_pins_seen:
                            raise SpdFormatError(
                                f"{path}: duplicate pin {pin!r} in .Connect "
                                f"block {connect_circuit!r}"
                            )
                        connect_pins_seen.add(pin)
                        circuit_pins[connect_circuit] += 1
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
                        netlist_ends.append(start)
                        continue
                    if _is_directive(text, ".netlist"):
                        netlist_starts.append(start)
                        raise SpdFormatError(f"{path}: nested .NetList section")
                    if _is_directive(text, ".powerdc") or _is_directive(
                        text, ".endpowerdc"
                    ):
                        raise SpdFormatError(
                            f"{path}: .PowerDC marker appears inside .NetList: {text!r}"
                        )
                decoded = _decode(raw)
                if decoded.endswith("\r\n"):
                    decoded = decoded[:-2] + "\n"
                body_parts.append(decoded)
                continue

            # -- inside a `.VRM`/`.Sink` block: cheap bytes screening ------- #
            if block is not None:
                # Existing blocks are removed wholesale during export.  Every
                # byte in this span therefore has to match the known grammar;
                # accepting an unknown directive would silently discard it.
                if raw[:1] != b".":
                    raise SpdFormatError(
                        f"{path}: unexpected data inside .{block.kind.upper()} "
                        f"block {block.name!r}"
                    )
                text = _strip_eol(_decode(raw))

                # A `.SinkCurrentSource` pair is an empty, terminal Sink
                # section.  No other directive or data is valid while open.
                if block.sink_source_state == "open":
                    if _is_bare_directive(text, ".endsinkcurrentsource"):
                        block.sink_source_state = "closed"
                        continue
                    raise SpdFormatError(
                        f"{path}: unexpected content inside .SinkCurrentSource "
                        f"of {block.name!r}"
                    )

                # `.Map` -> `.Node` -> `.EndMap` is a strict three-line unit.
                if block.map_state == "map":
                    if not _is_directive(text, ".node"):
                        raise SpdFormatError(
                            f"{path}: expected .Node immediately after .Map in "
                            f"{block.name!r}"
                        )
                    node_id, net = _parse_node_line(
                        text,
                        kind=block.kind,
                        map_pin=block.map_pin,
                        expect_voltage_inf=block.kind == "sink",
                    )
                    # The existing DC block is regenerated from the harvested
                    # `.Connect` index.  A syntactically valid but different
                    # node/net pair would otherwise be silently replaced by
                    # the writer, so require the exact authoritative mapping.
                    expected_pairs = by_circuit.get(block.map_comp, {}).get(net, ())
                    if (block.map_pin, node_id) not in expected_pairs:
                        raise SpdFormatError(
                            f"{path}: .{block.kind.upper()} map {block.map_comp!r}/"
                            f"{block.map_pin!r} does not match .Connect node/net "
                            f"({node_id!r}, {net!r})"
                        )
                    if block.section == "Positive Pin":
                        if block.net and block.net != net:
                            raise SpdFormatError(
                                f"{path}: .{block.kind.upper()} positive maps span multiple nets"
                            )
                        block.net = net
                    elif block.section == "Negative Pin":
                        if block.gnet and block.gnet != net:
                            raise SpdFormatError(
                                f"{path}: .{block.kind.upper()} negative maps span multiple nets"
                            )
                        block.gnet = net
                    block.section_maps.append((block.map_pin, node_id))
                    block.map_state = "node"
                    continue
                if block.map_state == "node":
                    if not _is_bare_directive(text, ".endmap"):
                        raise SpdFormatError(
                            f"{path}: expected .EndMap after .Node in {block.name!r}"
                        )
                    block.map_state = "idle"
                    block.map_pin = ""
                    block.map_comp = ""
                    continue

                if _is_directive(text, ".endpowerdc"):
                    raise SpdFormatError(
                        f"{path}: unterminated .{block.kind.upper()} block "
                        f"{block.name!r} before .EndPowerDC"
                    )
                if _is_directive(text, ".vrm") or _is_directive(text, ".sink"):
                    raise SpdFormatError(
                        f"{path}: nested .VRM/.Sink block inside "
                        f".{block.kind.upper()} {block.name!r}"
                    )

                if _is_directive(text, ".pin"):
                    if block.section:
                        raise SpdFormatError(
                            f"{path}: nested .Pin section inside {block.name!r}"
                        )
                    expected_index = block.section_index + 1
                    if expected_index >= len(block.expected_sections):
                        raise SpdFormatError(
                            f"{path}: duplicate .Pin section in {block.name!r}"
                        )
                    section_name = _parse_pin_name(text, kind=block.kind)
                    expected_name = block.expected_sections[expected_index]
                    if section_name.casefold() != expected_name.casefold():
                        raise SpdFormatError(
                            f"{path}: out-of-order or unknown .{block.kind.upper()} "
                            f"pin section {section_name!r}; expected {expected_name!r}"
                        )
                    block.section_index = expected_index
                    block.section = expected_name
                    continue

                if _is_bare_directive(text, ".endpin"):
                    if not block.section:
                        raise SpdFormatError(
                            f"{path}: orphan .EndPin in {block.name!r}"
                        )
                    if block.map_state != "idle":
                        raise SpdFormatError(
                            f"{path}: .EndPin before .EndMap in {block.name!r}"
                        )
                    if block.section in ("Positive Pin", "Negative Pin"):
                        if not block.section_maps:
                            raise SpdFormatError(
                                f"{path}: {block.section!r} section in {block.name!r} "
                                "has no maps"
                            )
                        section_net = block.net if block.section == "Positive Pin" else block.gnet
                        expected = by_circuit.get(block.comp, {}).get(section_net, ())
                        if tuple(block.section_maps) != tuple(expected):
                            raise SpdFormatError(
                                f"{path}: {block.section!r} maps in {block.name!r} "
                                "are incomplete or out of order versus .Connect"
                            )
                    block.section = ""
                    block.section_maps = []
                    continue
                if _is_directive(text, ".endpin"):
                    raise SpdFormatError(f"{path}: malformed .EndPin in {block.name!r}")

                if _is_directive(text, ".map"):
                    if not block.section:
                        raise SpdFormatError(
                            f"{path}: orphan .Map outside a .Pin section in {block.name!r}"
                        )
                    if block.section not in ("Positive Pin", "Negative Pin"):
                        raise SpdFormatError(
                            f"{path}: maps are not allowed in {block.section!r}"
                        )
                    comp, pin = _parse_map_header(text, kind=block.kind)
                    if block.comp and comp != block.comp:
                        raise SpdFormatError(
                            f"{path}: .{block.kind.upper()} block {block.name!r} spans "
                            f"multiple circuits ({block.comp!r}, {comp!r})"
                        )
                    if pin in block.map_pins:
                        raise SpdFormatError(
                            f"{path}: duplicate pin {pin!r} in .{block.kind.upper()} "
                            f"block {block.name!r}"
                        )
                    block.map_pins.add(pin)
                    block.comp = block.comp or comp
                    block.map_comp = comp
                    block.map_pin = pin
                    block.map_state = "map"
                    continue

                if _is_directive(text, ".node"):
                    raise SpdFormatError(
                        f"{path}: orphan .Node without .Map in {block.name!r}"
                    )
                if _is_directive(text, ".endmap"):
                    raise SpdFormatError(
                        f"{path}: orphan .EndMap without .Map in {block.name!r}"
                    )

                if _is_directive(text, ".sinkcurrentsource"):
                    if block.kind != "sink" or block.section or (
                        block.section_index != len(block.expected_sections) - 1
                    ) or block.sink_source_state != "absent":
                        raise SpdFormatError(
                            f"{path}: misplaced .SinkCurrentSource in {block.name!r}"
                        )
                    if not _is_bare_directive(text, ".sinkcurrentsource"):
                        raise SpdFormatError(
                            f"{path}: malformed .SinkCurrentSource in {block.name!r}"
                        )
                    block.sink_source_state = "open"
                    continue
                if _is_directive(text, ".endsinkcurrentsource"):
                    raise SpdFormatError(
                        f"{path}: orphan .EndSinkCurrentSource in {block.name!r}"
                    )

                if _is_directive(text, ".endvrm"):
                    if (
                        block.kind != "vrm"
                        or not _is_bare_directive(text, ".endvrm")
                        or block.section
                        or block.section_index != len(block.expected_sections) - 1
                    ):
                        raise SpdFormatError(
                            f"{path}: malformed or premature .EndVRM for {block.name!r}"
                        )
                    _finish_block(offset)
                    continue
                if _is_directive(text, ".endsink"):
                    if (
                        block.kind != "sink"
                        or not _is_bare_directive(text, ".endsink")
                        or block.section
                        or block.section_index != len(block.expected_sections) - 1
                        or block.sink_source_state != "closed"
                    ):
                        raise SpdFormatError(
                            f"{path}: malformed or premature .EndSink for {block.name!r}"
                        )
                    _finish_block(offset)
                    continue

                raise SpdFormatError(
                    f"{path}: unexpected directive inside .{block.kind.upper()} "
                    f"block {block.name!r}: {text!r}"
                )

            # Once anchor B has been reached, a replacement-safe existing DC
            # run may start on the very next byte only.  Blank/comment/unknown
            # lines before the first block are remembered as a gap; lines after
            # a block close the run, so a later VRM/Sink cannot be skipped into
            # the replacement span.
            if in_powerdc and anchor_spice_end >= 0 and start >= anchor_spice_end:
                if not raw.strip():
                    if block_run_started:
                        block_run_ended = True
                    else:
                        block_pre_gap = True
                elif raw[:1] not in _CANDIDATE_FIRST_BYTES:
                    if block_run_started:
                        block_run_ended = True
                    else:
                        block_pre_gap = True

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
                    pdc_elem_positions.append(start)
                elif in_powerdc and _ANCHORS["pdc_elem"].match(text):
                    pdc_elem_positions.append(start)
                    raise SpdFormatError(f"{path}: duplicate * PdcElem anchor")
                if in_powerdc and anchor_spice_end >= 0 and start >= anchor_spice_end:
                    if block_run_started:
                        block_run_ended = True
                    else:
                        block_pre_gap = True
                continue
            if text[:1] != ".":
                continue

            if _is_directive(text, ".connect"):
                parts = text.split()
                name = parts[1] if len(parts) > 1 else ""
                if not name:
                    raise SpdFormatError(f"{path}: .Connect block has no RefDes token")
                if name in by_circuit:
                    raise SpdFormatError(f"{path}: duplicate .Connect RefDes: {name!r}")
                by_circuit[name] = {}
                circuit_pins[name] = 0
                circuit_order.append(name)
                if not non_ascii_seen and not name.isascii():
                    non_ascii_seen = True
                    warnings.append(f"non-ASCII circuit name: {name!r}")
                connect_circuit = name
                connect_pins_seen = set()
                continue

            if _is_directive(text, ".powerdc"):
                powerdc_starts.append(start)
                if in_powerdc:
                    raise SpdFormatError(f"{path}: nested .PowerDC section")
                if powerdc_start < 0:
                    powerdc_start = start
                in_powerdc = True
                continue
            if _is_directive(text, ".endpowerdc"):
                powerdc_ends.append(start)
                if in_powerdc and powerdc_end < 0:
                    powerdc_end = start
                elif not in_powerdc:
                    raise SpdFormatError(f"{path}: .EndPowerDC without .PowerDC")
                in_powerdc = False
                continue

            if in_powerdc:
                if anchor_spice_end < 0 and _is_directive(text, ".endspicenetlist"):
                    anchor_spice_end = offset
                    spice_end_positions.append(start)
                    continue
                if _is_directive(text, ".endspicenetlist"):
                    spice_end_positions.append(start)
                    raise SpdFormatError(f"{path}: duplicate .EndSpiceNetlist anchor")
                if _is_directive(text, ".vrm") or _is_directive(text, ".sink"):
                    kind = "vrm" if text[:4].lower() == ".vrm" else "sink"
                    if block_run_ended or block_pre_gap:
                        raise SpdFormatError(
                            f"{path}: existing DC block run is not contiguous from "
                            "the .EndSpiceNetlist anchor"
                        )
                    if not block_run_started:
                        if start != anchor_spice_end:
                            raise SpdFormatError(
                                f"{path}: existing DC block run does not start "
                                "at .EndSpiceNetlist"
                            )
                        block_run_started = True
                    if block_last_kind == "sink" and kind == "vrm":
                        raise SpdFormatError(
                            f"{path}: VRM block appears after a Sink block"
                        )
                    name, values = _parse_block_header(text, kind)
                    block = _BlockState(kind, name, values)
                    continue

            if _is_directive(text, ".netlist"):
                netlist_starts.append(start)
                if in_netlist or netlist_start >= 0:
                    raise SpdFormatError(f"{path}: duplicate .NetList section")
                netlist_start = start
                netlist_body_start = offset
                in_netlist = True
                continue
            if _is_directive(text, ".endnetlist"):
                netlist_ends.append(start)
                raise SpdFormatError(f"{path}: .EndNetList without .NetList")

            if _is_directive(text, ".vrm") or _is_directive(text, ".sink"):
                raise SpdFormatError(
                    f"{path}: .VRM/.Sink block appears outside .PowerDC"
                )

            if in_powerdc and anchor_spice_end >= 0 and start >= anchor_spice_end:
                if block_run_started:
                    block_run_ended = True
                else:
                    block_pre_gap = True

    # A scan is not a valid source snapshot if the file changed while it was
    # being read.  Do this before exposing any offsets to the writer.
    try:
        current_identity = _source_identity(path)
    except OSError as exc:
        raise SpdFormatError(f"{path}: source disappeared during scan") from exc
    if current_identity != source_identity:
        raise SpdFormatError(f"{path}: source changed during scan; please scan again")

    if progress is not None:
        progress(file_size, file_size)

    if connect_circuit is not None:
        raise SpdFormatError(
            f"{path}: .Connect block {connect_circuit!r} is not terminated by .EndC"
        )
    if block is not None:
        raise SpdFormatError(
            f"{path}: unterminated .{block.kind.upper()} block {block.name!r}"
        )

    # -- mandatory structure ----------------------------------------------- #
    if len(powerdc_starts) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one .PowerDC section, found {len(powerdc_starts)}"
        )
    if len(powerdc_ends) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one .EndPowerDC, found {len(powerdc_ends)}"
        )
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
    if len(pdc_elem_positions) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one * PdcElem anchor inside .PowerDC"
        )
    if len(spice_end_positions) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one .EndSpiceNetlist inside .PowerDC"
        )
    if not (
        powerdc_start < pdc_elem_positions[0] < anchor_spice_end
        and anchor_spice_end <= powerdc_end
    ):
        raise SpdFormatError(
            f"{path}: mandatory .PowerDC anchors are overlapping or non-monotonic"
        )
    if len(netlist_starts) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one .NetList section, found {len(netlist_starts)}"
        )
    if len(netlist_ends) != 1:
        raise SpdFormatError(
            f"{path}: expected exactly one .EndNetList, found {len(netlist_ends)}"
        )
    if netlist_start < 0:
        raise SpdFormatError(f"{path}: no .NetList section")
    if netlist_end < 0:
        raise SpdFormatError(f"{path}: .NetList section is not closed by .EndNetList")
    if not (powerdc_end < netlist_start < netlist_end):
        raise SpdFormatError(
            f"{path}: .PowerDC/.NetList sections overlap or are non-monotonic"
        )

    netlist_body = "".join(body_parts)
    try:
        nets: tuple[NetEntry, ...] = tuple(parse_netlist(netlist_body))
    except NetlistFormatError as exc:
        raise SpdFormatError(f"{path}: malformed .NetList: {exc}") from exc

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
        if info.pin_count >= 2 and is_other_circuit(info.name)
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
        source_identity=source_identity,
    )


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
    """:func:`read_region` decoded as UTF-8 with ``errors="surrogateescape"``.

    Same codec as :func:`_decode` (design §G.6), so a region read back here
    compares equal to the same bytes seen during the scan.
    """
    return read_region(path, start, end).decode("utf-8", errors="surrogateescape")
