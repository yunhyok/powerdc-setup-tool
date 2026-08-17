"""`.NetList` grammar parse + minimal-edit re-render -- owned by chunk 1 (design §A;
spec §2).

Round-trips byte-identical when no `PowerNetConfig` changes are applied. When
a net's classification changes, only that net's own line (and, for a newly
classified member, its insertion point at the end of its group's existing
member run) is rewritten -- never re-sort, never regenerate untouched lines
(design §G.7).
"""

from __future__ import annotations

from powerdc_setup_tool.core.model import NetEntry, PowerNetConfig

# spec §2: "Colors cycle over 11 names."
NETLIST_COLORS: tuple[str, ...] = (
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


def parse_netlist(body: str) -> list[NetEntry]:
    """Parse the `.NetList` body (entries only, no `.NetList`/`.EndNetList` lines)."""
    raise NotImplementedError


def render_netlist(
    entries: list[NetEntry],
    configs: dict[str, PowerNetConfig],
    *,
    emit_groups: bool = True,
) -> str:
    """Re-render the netlist body applying *configs*, with minimal edits (design §G.7)."""
    raise NotImplementedError


def _render_entry(e: NetEntry) -> str:
    """Render a single `NetEntry` back to its verbatim (or edited) line text."""
    raise NotImplementedError
