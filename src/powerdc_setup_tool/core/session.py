"""`Session` -- single source of truth for config + derived-value propagation
-- owned by chunk 3 (design §B propagation rule, §D `build_plan`).

Plain Python, no Qt; headless-testable per design §E `test_session`. Mutators
return the list of changed `(net, field)` keys so `ui/models.py`'s
`BaseConfigModel` can translate them to `(row, col)` and emit one batched
`dataChanged` per bulk operation (design §B).
"""

from __future__ import annotations

from collections.abc import Mapping

from powerdc_setup_tool.core.model import (
    PowerNetConfig,
    ScanResult,
    SinkConfig,
    ValidationIssue,
    VrmConfig,
)
from powerdc_setup_tool.core.writer import WritePlan

# (net, field) -- ui/models.py maps these to concrete (row, col) table indices.
ChangedKey = tuple[str, str]


class Session:
    """Owns all `*Config` state derived from a `ScanResult` plus user edits."""

    def __init__(self) -> None:
        raise NotImplementedError

    def load(self, scan: ScanResult) -> None:
        """Reset state from a fresh scan; preload already-classified nets (design §G.1)."""
        raise NotImplementedError

    def set_net_voltage(self, net: str, voltage: float) -> list[ChangedKey]:
        """Set `PowerNetConfig.voltage`; propagate into non-overridden derived fields."""
        raise NotImplementedError

    def set_class(self, net: str, net_class: str) -> list[ChangedKey]:
        """Set `PowerNetConfig.net_class` (``"power"``/``"ground"``/``"none"``)."""
        raise NotImplementedError

    def set_paired_ground(self, net: str, gnet: str) -> list[ChangedKey]:
        """Set the paired ground net for *net* (`PowerNetConfig`/`VrmConfig`/`SinkConfig`)."""
        raise NotImplementedError

    def autopair(self) -> None:
        """Default `paired_gnd` per power net: most pins on the VRM component,
        tie -> ASCII-first (design §C P/G pairing UX)."""
        raise NotImplementedError

    def derive_all(self) -> None:
        """Recompute every non-overridden derived field from its upstream value."""
        raise NotImplementedError

    def reset_auto(self, field_keys: list[ChangedKey]) -> list[ChangedKey]:
        """Clear `*_override` flags for *field_keys* and re-derive (design §B)."""
        raise NotImplementedError

    @property
    def vrm_rows(self) -> list[VrmConfig]:
        raise NotImplementedError

    @property
    def sink_rows(self) -> list[SinkConfig]:
        raise NotImplementedError

    def validate(self) -> list[ValidationIssue]:
        """Blocking/warning checks: 0 positive pins, no paired ground, voltage <= 0,
        duplicate block name (design §C export flow, §G.3/§G.4)."""
        raise NotImplementedError

    def to_json(self) -> str:
        raise NotImplementedError

    @classmethod
    def from_json(cls, data: str) -> Session:
        raise NotImplementedError

    def build_plan(self, opts: Mapping[str, bool]) -> WritePlan:
        """Freeze current config into a `WritePlan` for `writer.write_spd` (design §D)."""
        raise NotImplementedError
