"""Streaming splice writer -- owned by chunk 2 (design §D).

Ports the CRLF/CR -> LF, chunk-boundary-safe normalization verbatim from the
sibling `spd-model-injector` repo's `core/spd.py` (see `old_repo_brief.md`):
`_normalize_chunk` carries a trailing ``\\r`` across the chunk boundary in a
`pending_cr` flag so a ``\\r\\n`` split by the 1 MB read window still collapses
to a single ``\\n``.

Output is unconditionally LF-only, including inside copied regions (design §G.6,
a hard Cadence requirement); it is written to ``<output>.part`` and renamed onto
*output* only after the last byte lands (design §G.8).
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from powerdc_setup_tool.core.model import ScanResult, SinkConfig, SourceIdentity, VrmConfig
from powerdc_setup_tool.core.pdc_gen import (
    NegPinCache,
    estimate_block_bytes,
    other_circuit_bytes,
    render_other_circuits,
    render_sink,
    render_vrm,
)

# `((pin, node), ...)` -- one circuit's pins on one net.
PinPairs = tuple[tuple[str, str], ...]

_CHUNK_SIZE = 1024 * 1024

# spec §8 row 1 / ambiguity A9: the PowerDC value of the line-1 workflow bitmask.
WORKFLOW_KEY_DC = "0x1000000067"

# design §G.8 pre-flight: require input size x this factor free on the target volume.
FREE_SPACE_FACTOR = 1.25

# design §G.5: throttle progress callbacks to <= 20 Hz.
_PROGRESS_MIN_INTERVAL_S = 0.05

# Existing `.OtherCircuit` lines are regenerated wholesale (see `_splice` step 4).
_OTHER_CIRCUIT_LINE_RE = re.compile(rb"^\.OtherCircuit(?:[ \t]|\n|\r|$)", re.IGNORECASE)


class WriteCancelled(RuntimeError):
    """Raised by `write_spd` when its `cancel` event fires; the partial file is deleted."""


class InsufficientDiskSpaceError(OSError):
    """Raised by `write_spd`'s pre-flight check (design §G.8) before any byte is written."""


@dataclass(frozen=True)
class WritePlan:
    """Everything `write_spd` needs; built by `Session.build_plan()` (design §D).

    The writer performs no config logic: `vrms`/`sinks` already carry their
    resolved positive/negative pin tuples, in the order they must be emitted, and
    already exclude disabled rows.

    `netlist_body is None` means "copy the original bytes" (design §G.1, which
    guarantees the spec §8 "netlist unchanged" property). `other_circuits is None`
    means "leave whatever the input has alone"; any tuple (including an empty one)
    means "regenerate the run", which also drops the input's existing
    `.OtherCircuit` lines.
    """

    vrms: list[tuple[VrmConfig, PinPairs, PinPairs]]
    sinks: list[tuple[SinkConfig, PinPairs, PinPairs]]
    netlist_body: str | None
    other_circuits: tuple[str, ...] | None
    patch_workflow_key: bool


def write_spd(
    scan: ScanResult,
    plan: WritePlan,
    output: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Single forward-pass splice from `scan.path` to `output` (design §D steps 1-9).

    Writes to `<output>.part`, renamed on success, deleted on error/cancel
    (design §G.8). Raises `ValueError` if `output` resolves to `scan.path`,
    `InsufficientDiskSpaceError` if the target volume is too small, and
    `WriteCancelled` if *cancel* fires (polled per chunk and per block).
    """
    source = Path(scan.path)
    output = Path(output)
    part = output.with_name(output.name + ".part")
    _ensure_output_is_not_source(source, output, part)
    expected_identity = scan.source_identity
    _verify_source_stable(source, scan, expected_identity)
    _check_free_space(output, scan.file_size)

    reporter = _Progress(progress, scan.file_size + _plan_size_delta(scan, plan))
    staging_created = False
    try:
        with (
            source.open("rb", buffering=_CHUNK_SIZE) as src,
            # ``x`` is important here: the alias check above is only a
            # snapshot.  A caller, filesystem watcher, or another process can
            # create a hardlink/symlink at ``part`` after that check and before
            # this open.  Truncating with ``wb`` would then destroy the source
            # through the alias.  Exclusive creation gives us a new inode and
            # leaves any pre-existing staging path untouched on failure.
            part.open("xb", buffering=_CHUNK_SIZE) as dst,
        ):
            staging_created = True
            _splice(scan, plan, src, dst, reporter, cancel)
        # Do not publish a mixed snapshot if the source was edited while the
        # potentially long export was in flight.  The staging file is removed
        # by the exception path and the source remains untouched.
        _verify_source_stable(source, scan, expected_identity)
        os.replace(part, output)
        staging_created = False
    except BaseException:
        # Never remove a stale/external ``.part`` that this call did not create.
        # In particular, ``part.open('xb')`` can fail after a race-created
        # hardlink is in place; unlinking unconditionally would erase that
        # caller-owned path (and, before this guard, could erase the source).
        if staging_created:
            part.unlink(missing_ok=True)
        raise
    reporter.finish()


# ---------------------------------------------------------------------------
# The splice itself (design §D steps 1-9)
# ---------------------------------------------------------------------------


class _Cursor:
    """Write-side state threaded through one splice: dst + pending_cr + progress."""

    def __init__(
        self,
        dst: BinaryIO,
        reporter: _Progress,
        cancel: threading.Event | None,
    ) -> None:
        self.dst = dst
        self.reporter = reporter
        self.cancel = cancel
        self.pending_cr = False

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise WriteCancelled("Export cancelled; the partial output was deleted.")

    def flush_pending_cr(self) -> None:
        """Emit the ``\\n`` a carried-over ``\\r`` stands for (before generated text
        or a discontinuous jump, where the next byte cannot complete its CRLF)."""
        if self.pending_cr:
            self.dst.write(b"\n")
            self.reporter.add(1)
            self.pending_cr = False

    def emit(self, text: str) -> None:
        """Write generated text, LF-normalized (verbatim otherwise -- callers that
        need a terminating newline, e.g. a replacement netlist body, run the text
        through `_normalize_text` first; the WorkflowKey token must *not* get one)."""
        self.check_cancel()
        self.flush_pending_cr()
        if not text:
            return
        data = _encode(text.replace("\r\n", "\n").replace("\r", "\n"))
        self.dst.write(data)
        self.reporter.add(len(data))

    def copy(self, src: BinaryIO, start: int, end: int | None) -> None:
        """Copy the raw byte range [start, end) (or to EOF when *end* is None)."""
        self.check_cancel()
        self.flush_pending_cr()
        self.pending_cr = _copy_range(
            src,
            self.dst,
            start,
            end,
            self.pending_cr,
            on_chunk=self.reporter.add,
            cancel=self.cancel,
        )

    def copy_without_other_circuits(self, src: BinaryIO, start: int, end: int) -> None:
        """Copy [start, end) line-by-line, dropping existing `.OtherCircuit` lines."""
        self.check_cancel()
        self.flush_pending_cr()
        self.pending_cr = _copy_range_filtered(
            src,
            self.dst,
            start,
            end,
            self.pending_cr,
            on_chunk=self.reporter.add,
            cancel=self.cancel,
        )


def _splice(
    scan: ScanResult,
    plan: WritePlan,
    src: BinaryIO,
    dst: BinaryIO,
    reporter: _Progress,
    cancel: threading.Event | None,
) -> None:
    cur = _Cursor(dst, reporter, cancel)
    cur.check_cancel()

    # 1. line-1 WorkflowKey patch -- skipped entirely (no split) when off/absent.
    cursor = 0
    if plan.patch_workflow_key and scan.workflow_key_span is not None:
        key_start, key_end = scan.workflow_key_span
        cur.copy(src, cursor, key_start)
        cur.emit(WORKFLOW_KEY_DC)
        cursor = key_end

    # 2. ... up to anchor A (end of "* PdcElem description lines\n").
    cur.copy(src, cursor, scan.anchor_pdc_elem_end)

    # 3. the regenerated `.OtherCircuit` run.
    regenerate_other = plan.other_circuits is not None
    if regenerate_other:
        cur.emit(render_other_circuits(plan.other_circuits or ()))

    # 4. anchor A -> anchor B (end of ".EndSpiceNetlist\n"). When step 3 emitted a
    #    fresh run, the input's own `.OtherCircuit` lines are dropped on the way
    #    past -- the same "regenerate wholesale" rule design §G.2 states for
    #    VRM/Sink blocks, and what makes a DC-in/DC-out re-run idempotent instead
    #    of doubling the run. Without regeneration the range is copied verbatim.
    if regenerate_other:
        cur.copy_without_other_circuits(src, scan.anchor_pdc_elem_end, scan.anchor_spice_end)
    else:
        cur.copy(src, scan.anchor_pdc_elem_end, scan.anchor_spice_end)

    # 5. every VRM block, then every Sink block: contiguous, no blank lines (spec §3).
    cache = NegPinCache()
    for cfg, pos_pins, neg_pins in plan.vrms:
        cur.emit(render_vrm(cfg, pos_pins, neg_pins, cache=cache))
    for sink_cfg, pos_pins, neg_pins in plan.sinks:
        cur.emit(render_sink(sink_cfg, pos_pins, neg_pins, cache=cache))

    # 6. skip the input's existing VRM/Sink run, if any (design §G.2).
    cursor = (
        scan.existing_blocks_span[1]
        if scan.existing_blocks_span is not None
        else scan.anchor_spice_end
    )

    # 7. ... up to the netlist body.
    body_start, body_end = scan.netlist_body_span
    cur.copy(src, cursor, body_start)

    # 8. the netlist body: rewritten, or the original bytes when unchanged (§G.1).
    if plan.netlist_body is None:
        cur.copy(src, body_start, body_end)
    else:
        cur.emit(_normalize_text(plan.netlist_body))

    # 9. ... and the rest of the file.
    cur.copy(src, body_end, None)
    cur.flush_pending_cr()


# ---------------------------------------------------------------------------
# Byte copying -- ported from spd-model-injector `core/spd.py`
# ---------------------------------------------------------------------------


def _copy_range(
    src: BinaryIO,
    dst: BinaryIO,
    start: int,
    end: int | None,
    pending_cr: bool,
    *,
    on_chunk: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> bool:
    """Copy raw bytes `[start, end)` from *src* to *dst*, LF-normalizing (design §D).

    *end* may be ``None`` to copy through EOF. Returns the updated `pending_cr`
    flag for the next call.
    """
    src.seek(start)
    remaining = None if end is None else end - start
    while remaining is None or remaining > 0:
        if cancel is not None and cancel.is_set():
            raise WriteCancelled("Export cancelled; the partial output was deleted.")
        size = _CHUNK_SIZE if remaining is None else min(_CHUNK_SIZE, remaining)
        chunk = src.read(size)
        if not chunk:
            break
        if remaining is not None:
            remaining -= len(chunk)
        normalized, pending_cr = _normalize_chunk(chunk, pending_cr)
        dst.write(normalized)
        if on_chunk is not None:
            on_chunk(len(normalized))
    return pending_cr


def _copy_range_filtered(
    src: BinaryIO,
    dst: BinaryIO,
    start: int,
    end: int,
    pending_cr: bool,
    *,
    on_chunk: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> bool:
    """Copy `[start, end)` line-by-line, LF-normalizing and dropping `.OtherCircuit` lines.

    Only used for the anchor A -> anchor B range, which is the `* PdcElem`
    preamble (~505 KB on the real file) -- bounded enough to walk by line.
    """
    src.seek(start)
    remaining = end - start
    while remaining > 0:
        if cancel is not None and cancel.is_set():
            raise WriteCancelled("Export cancelled; the partial output was deleted.")
        raw = src.readline(remaining)
        if not raw:
            break
        remaining -= len(raw)
        # Normalize *first*, then split: a CR-only input has no ``\n`` for
        # `readline` to stop at, so one "line" here can be the whole range.
        normalized, pending_cr = _normalize_chunk(raw, pending_cr)
        parts = normalized.split(b"\n")
        tail = parts.pop()  # unterminated remainder (b"" when the read ended on \n)
        kept = b"".join(
            part + b"\n" for part in parts if not _OTHER_CIRCUIT_LINE_RE.match(part + b"\n")
        )
        if tail:
            if _OTHER_CIRCUIT_LINE_RE.match(tail):
                pending_cr = False  # the dropped line owns its own terminator
            else:
                kept += tail
        if kept:
            dst.write(kept)
            if on_chunk is not None:
                on_chunk(len(kept))
    return pending_cr


def _normalize_chunk(chunk: bytes, pending_cr: bool) -> tuple[bytes, bool]:
    """CRLF/CR -> LF, carrying a pending trailing ``\\r`` across chunk boundaries."""
    prefix = b""
    if pending_cr:
        if chunk[:1] == b"\n":
            chunk = chunk[1:]
        prefix = b"\n"
    trailing_cr = chunk.endswith(b"\r")
    if trailing_cr:
        chunk = chunk[:-1]
    normalized = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return prefix + normalized, trailing_cr


def _normalize_text(text: str) -> str:
    """LF-normalize generated text and guarantee a trailing newline."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or normalized.endswith("\n"):
        return normalized
    return normalized + "\n"


def _encode(text: str) -> bytes:
    """UTF-8 encode generated text, mirroring `spd_scan`'s ``surrogateescape`` decode.

    `plan.netlist_body` is a re-rendered copy of `ScanResult.netlist_body`, which
    `core/spd_scan.py` decodes with ``errors="surrogateescape"`` so that bytes
    the file's own encoding cannot explain survive as lone surrogates. Encoding
    with the same handler turns them back into the original bytes, which is what
    makes a `.NetList` line the user never touched come out byte-identical even
    though a *different* net's classification forced the whole body to be
    rewritten (design §G.6 "copy untouched regions byte-for-byte").
    """
    return text.encode("utf-8", errors="surrogateescape")


def _path_alias(first: Path, second: Path) -> bool:
    """Best-effort alias test covering existing hardlinks and symlinks."""
    try:
        if first.exists() and second.exists() and os.path.samefile(first, second):
            return True
    except OSError:
        pass
    try:
        return first.resolve(strict=False) == second.resolve(strict=False)
    except OSError:
        return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
            os.path.abspath(str(second))
        )


def _ensure_output_is_not_source(
    source: Path, output: Path, part: Path | None = None
) -> None:
    """Reject output *and staging* paths that alias the source before any write."""
    candidates = (output, part) if part is not None else (output,)
    if any(_path_alias(source, candidate) for candidate in candidates):
        raise ValueError(
            "Output path (including its staging file) must differ from the source "
            "SPD path; writing would destroy the source."
        )


def _capture_source_identity(path: Path) -> SourceIdentity:
    stat = path.stat()
    return SourceIdentity(
        resolved_path=str(path.resolve(strict=False)),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        st_dev=getattr(stat, "st_dev", None),
        st_ino=getattr(stat, "st_ino", None),
    )


def _verify_source_stable(
    source: Path, scan: ScanResult, expected: SourceIdentity | None
) -> None:
    """Raise before staging/publish when the scanned source is no longer stable."""
    try:
        actual = _capture_source_identity(source)
    except OSError as exc:
        raise ValueError(f"Source SPD disappeared or is unreadable: {source}") from exc
    if expected is not None:
        if actual != expected:
            raise ValueError(
                "Source SPD changed after scanning; scan it again before exporting."
            )
        return
    # Hand-built ScanResult fixtures from older callers predate the identity
    # field.  At minimum, refuse an offset-bearing plan when the file length no
    # longer matches that scan snapshot.
    if actual.size != scan.file_size:
        raise ValueError(
            "Source SPD size changed after scanning; scan it again before exporting."
        )


# ---------------------------------------------------------------------------
# Pre-flight + progress
# ---------------------------------------------------------------------------


def _check_free_space(output: Path, file_size: int) -> None:
    """Refuse up front when the target volume cannot hold the output (design §G.8)."""
    target = output.parent if str(output.parent) else Path(".")
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return  # cannot determine -- let the write itself surface any real problem
    required = int(file_size * FREE_SPACE_FACTOR)
    if free < required:
        raise InsufficientDiskSpaceError(
            f"Not enough free space on {target}: {free:,} bytes available but "
            f"{required:,} bytes required ({file_size:,} byte input x {FREE_SPACE_FACTOR}). "
            f"Free up space or choose another drive."
        )


def _plan_size_delta(scan: ScanResult, plan: WritePlan) -> int:
    """Signed output-size delta vs the input, for the progress denominator (design §D).

    Inserted text minus the regions `_splice` drops: an already-DC input's VRM/Sink
    run (~213 MB on the real file) is skipped, so counting only insertions would
    leave a re-run's progress bar stuck near half. The input's own `.OtherCircuit`
    run cannot be measured without re-reading it, so a DC-in/DC-out re-run still
    over-estimates by that (~505 KB out of ~1.4 GB) -- progress is an estimate.
    """
    delta = sum(estimate_block_bytes(cfg, pos, neg) for cfg, pos, neg in plan.vrms)
    delta += sum(estimate_block_bytes(cfg, pos, neg) for cfg, pos, neg in plan.sinks)
    if plan.other_circuits:
        delta += other_circuit_bytes(plan.other_circuits)
    if scan.existing_blocks_span is not None:
        start, end = scan.existing_blocks_span
        delta -= end - start
    if plan.netlist_body is not None:
        body_start, body_end = scan.netlist_body_span
        delta += len(_encode(_normalize_text(plan.netlist_body))) - (body_end - body_start)
    if plan.patch_workflow_key and scan.workflow_key_span is not None:
        key_start, key_end = scan.workflow_key_span
        delta += len(_encode(WORKFLOW_KEY_DC)) - (key_end - key_start)
    return delta


class _Progress:
    """Byte-counting progress reporter, throttled to <= 20 Hz (design §G.5)."""

    def __init__(self, callback: Callable[[int, int], None] | None, total: int) -> None:
        self._callback = callback
        self._total = max(total, 1)
        self._done = 0
        self._last_emit = 0.0

    def add(self, count: int) -> None:
        self._done += count
        if self._callback is None:
            return
        now = time.monotonic()
        if now - self._last_emit < _PROGRESS_MIN_INTERVAL_S:
            return
        self._last_emit = now
        self._callback(min(self._done, self._total), self._total)

    def finish(self) -> None:
        """Resolve the estimated denominator to the byte count actually written."""
        if self._callback is None:
            return
        self._callback(self._done, self._done)
