"""Streaming splice writer -- owned by chunk 2 (design §D).

Ports the CRLF/CR -> LF, chunk-boundary-safe normalization verbatim from the
sibling `spd-model-injector` repo's `core/spd.py` (see `old_repo_brief.md`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from powerdc_setup_tool.core.model import ScanResult, SinkConfig, VrmConfig

# `((pin, node), ...)` -- one circuit's pins on one net.
PinPairs = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class WritePlan:
    """Everything `write_spd` needs; built by `Session.build_plan()` (design §D).

    The writer performs no config logic: `vrms`/`sinks` already carry their
    resolved positive/negative pin tuples.
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
    (design §G.8). Raises `ValueError` if `output` resolves to `scan.path`.
    """
    raise NotImplementedError


def _copy_range(src: BinaryIO, dst: BinaryIO, start: int, end: int, pending_cr: bool) -> bool:
    """Copy raw bytes `[start, end)` from *src* to *dst*, LF-normalizing (design §D).

    Returns the updated `pending_cr` flag for the next call.
    """
    raise NotImplementedError


def _normalize_chunk(chunk: bytes, pending_cr: bool) -> tuple[bytes, bool]:
    """CRLF/CR -> LF, carrying a pending trailing ``\\r`` across chunk boundaries."""
    raise NotImplementedError


def _ensure_output_is_not_source(source: Path, output: Path) -> None:
    """Raise `ValueError` if *output* resolves to the same file as *source*."""
    raise NotImplementedError
