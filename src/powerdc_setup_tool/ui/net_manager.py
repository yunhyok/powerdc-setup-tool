"""Net Manager tab -- owned by chunk 5 (design §C "Net Manager tab").

Filter `QLineEdit` (Ctrl+F, space-separated AND terms, `*` glob,
case-insensitive) + class combo (All/Power/Ground/Unclassified/Selected/
Errors) + shown-count label. Buttons: Mark Power, Mark Ground, Clear class,
Set ground for selected..., Set voltage for selected.... Table columns 0-8:
Use | Net | Class | Paired GND | Voltage (V) | Die | Pins @VRM comp |
Pins @Sink comp | Source.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from powerdc_setup_tool.core.session import Session


class NetManagerTab(QWidget):
    """Filter box + class/pairing toolbar + net table."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        raise NotImplementedError

    def _apply_filter(self, text: str) -> None:
        raise NotImplementedError

    def _mark_power(self) -> None:
        raise NotImplementedError

    def _mark_ground(self) -> None:
        raise NotImplementedError

    def _clear_class(self) -> None:
        raise NotImplementedError

    def _set_ground_for_selected(self) -> None:
        raise NotImplementedError

    def _set_voltage_for_selected(self) -> None:
        raise NotImplementedError
