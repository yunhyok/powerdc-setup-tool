"""Single streaming scan pass over a `.spd` file -- owned by chunk 1 (design §A;
spec §9 "Pass 1 - index").

Records byte offsets of every splice point, harvests `.Connect` pin data (spec
ADDENDUM), the netlist body text, and any existing VRM/Sink block headers
(already-DC input, spec §8). Pure stdlib, no Qt.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from powerdc_setup_tool.core.model import ScanResult

# Anchor regexes used while walking the file (spec §9 pass 1). Matching rules
# are the algorithmic heart of this module and are left for chunk 1 to design
# and test (design §E test_spd_scan): anchors must fire only inside
# `[.PowerDC, .EndPowerDC)`, must not match decoys like `.PowerDCFoo` or a
# `* PdcElem...` comment outside `.PowerDC`, and `.EndC` (spec ADDENDUM
# `.Connect` terminator) must not be confused with the unrelated `.EndCTE`
# thermal-block directive.
_ANCHORS: dict[str, re.Pattern[str]] = {}


def scan_spd(path: Path, progress: Callable[[int, int], None] | None = None) -> ScanResult:
    """Sequential single pass over *path*; never loads the whole file into memory.

    *progress*, if given, is called with ``(bytes_read, file_size)`` throttled
    to <=20 Hz (design §G.5).
    """
    raise NotImplementedError


def read_region(path: Path, start: int, end: int) -> str:
    """Seek to *start* and decode exactly ``[start, end)`` as UTF-8 (errors="replace").

    Lazy, on-demand re-read of a previously indexed span -- e.g. for the
    two-phase-scan fallback in design §G.5 -- without holding the whole file
    in RAM.
    """
    raise NotImplementedError
