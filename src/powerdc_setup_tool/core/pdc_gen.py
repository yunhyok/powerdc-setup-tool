"""`.VRM`/`.Sink`/`.OtherCircuit` block text generation -- owned by chunk 2 (design §A/§D;
spec §9 templates, verbatim).

Pure text generation: no file I/O and no byte-offset logic (that is
`writer.py`'s job).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from powerdc_setup_tool.core.model import SinkConfig, VrmConfig

# (positive_pins, negative_pins), each `((pin, node), ...)` -- design §D WritePlan.
PinPairs = tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]

# spec §8 row 2 / design §G.2 default filter for `.OtherCircuit` emission.
OTHER_CIRCUIT_RE: re.Pattern[str] = re.compile(r"^C\d+_[01]$")


def render_vrm(cfg: VrmConfig, pins: PinPairs) -> str:
    """Render one complete `.VRM ... .EndVRM` block (spec §9 template, verbatim)."""
    raise NotImplementedError


def render_sink(cfg: SinkConfig, pins: PinPairs) -> str:
    """Render one complete `.Sink ... .EndSink` block (spec §9 template, verbatim)."""
    raise NotImplementedError


def render_other_circuits(names: Iterable[str]) -> str:
    """Render `.OtherCircuit Device = {n} Name = "{n}"` lines, sorted by (numeric id, die)."""
    raise NotImplementedError


class NegPinCache:
    """LRU cache (capacity 8) of rendered negative-pin blobs, keyed by ``(comp, gnet, style)``.

    ``style`` is ``"vrm"`` or ``"sink"`` (Sink negative pins additionally carry
    ``Voltage = inf``, spec §5). Blobs are byte-identical across all blocks
    sharing a key (spec §4/§D) -- this cache is what keeps `writer.py`'s peak
    memory bounded per the design §D size budget.
    """

    def __init__(self, maxsize: int = 8) -> None:
        raise NotImplementedError

    def get(
        self,
        comp: str,
        gnet: str,
        style: str,
        pins: tuple[tuple[str, str], ...],
    ) -> str:
        raise NotImplementedError


def estimate_insert_bytes(
    cfgs: Sequence[VrmConfig | SinkConfig],
    pins: Mapping[str, PinPairs],
    opts: Mapping[str, bool],
) -> int:
    """Estimate total inserted-text size, for export progress (design §D size budget)."""
    raise NotImplementedError
