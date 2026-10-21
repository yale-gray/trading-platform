"""Logging setup: console plus a rotating file at `<log_dir>/runner.log`."""

import logging
import logging.config
from pathlib import Path

from tradebot.config import Settings

LOG_FILE_NAME = "runner.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
CONTEXT_FIELDS = ("portfolio", "sleeve", "bar_ts")


class ContextDefaultsFilter(logging.Filter):
    """Fill `portfolio`, `sleeve` and `bar_ts` with "-" when a record lacks them."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name in CONTEXT_FIELDS:
            if not hasattr(record, name):
                setattr(record, name, "-")
        return True


def configure_logging(settings: Settings) -> None:
    """Apply logging config from settings. Creates the log directory if needed."""
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    level = settings.log_level.upper()
    fmt = (
        "ts=%(asctime)s level=%(levelname)s logger=%(name)s "
        "portfolio=%(portfolio)s sleeve=%(sleeve)s bar_ts=%(bar_ts)s msg=%(message)s"
    )
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": ContextDefaultsFilter}},
            "formatters": {"kv": {"format": fmt}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "kv",
                    "filters": ["context"],
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "kv",
                    "filters": ["context"],
                    "filename": str(log_dir / LOG_FILE_NAME),
                    "maxBytes": MAX_BYTES,
                    "backupCount": BACKUP_COUNT,
                    "encoding": "utf-8",
                },
            },
            "root": {"level": level, "handlers": ["console", "file"]},
        }
    )
