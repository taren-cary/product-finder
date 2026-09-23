"""Logging setup: messages go to the screen and to logs/gapfinder.log.

Call setup_logging() once at the start of a script, then in any file:
    import logging
    log = logging.getLogger(__name__)
    log.info("Fetched %d products", count)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from core.config import PROJECT_ROOT, settings

LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    # Windows consoles default to an old encoding that can't show emoji or
    # accented letters in product names; switch the screen output to UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    level = settings.get("logging", {}).get("level", "INFO")
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s: %(message)s")

    # Log file: starts a new file at 5 MB and keeps the last 10 files.
    file_handler = RotatingFileHandler(
        LOG_DIR / "gapfinder.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    handlers = [file_handler]
    # When run by Task Scheduler (pythonw, no window) there is no screen to
    # print to, so only log to the file.
    if sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        handlers.append(console_handler)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = handlers

    # Quiet down chatty libraries.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
