"""Application entry point for SPD Manipulator for PowerDC.

Owns process bootstrap only: CLI parsing, logging configuration, then hands
off to the Qt UI. ``MainWindow`` (and PySide6 itself, beyond the small bit
needed to build the QApplication) is imported lazily inside :func:`main` so
that ``powerdc_setup_tool.core`` stays importable in headless/test
environments without PySide6 installed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DISPLAY_NAME = "SPD Manipulator for PowerDC"
LOG_FILE_NAME = "app.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
LOG_BACKUP_COUNT = 3

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _log_dir() -> Path:
    """Best-effort per-platform app-data directory for the rotating log file.

    Preferred: Windows ``%LOCALAPPDATA%``. Platform-appropriate fallback via
    ``Path.home()`` when unset (design §A) -- kept dependency-free so this
    function never needs Qt, even though ``--debug`` logging is configured
    before the lazy Qt import in :func:`main`.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DISPLAY_NAME

    return Path.home() / ".powerdc_setup_tool" / APP_DISPLAY_NAME


def _configure_logging(debug: bool) -> None:
    """Configure the root logger.

    ``debug=True``  -> rotating file handler (2 MB x 3 backups) under the
                        platform app-data directory, level DEBUG.
    ``debug=False`` -> WARNING and above to stderr only.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)

    if debug:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / LOG_FILE_NAME,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        logging.getLogger(__name__).debug("logging to %s", log_dir / LOG_FILE_NAME)
    else:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(logging.WARNING)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="powerdc-setup-tool", description=APP_DISPLAY_NAME)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable verbose rotating file logging instead of WARNING-to-stderr",
    )
    known, _unknown = parser.parse_known_args(argv)
    return known


def main(argv: list[str] | None = None) -> int:
    """Process entry point: bootstrap logging, then the Qt application."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.debug)

    # Imported lazily so `powerdc_setup_tool.core` stays importable without
    # Qt installed (e.g. headless testing of core/ alone).
    from PySide6.QtWidgets import QApplication

    from powerdc_setup_tool.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setOrganizationName("Yunhyok")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
