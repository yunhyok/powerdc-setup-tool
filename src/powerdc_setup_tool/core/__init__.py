"""Core (headless) logic for SPD Manipulator for PowerDC.

Zero Qt imports anywhere under ``core/`` — this package must stay importable
and fully testable without PySide6 installed.
"""

from __future__ import annotations
