"""Logging configuration and the timezone-aware formatter.

Isolated so that the log format and timezone policy are defined in one place and
the rest of the code depends only on a plain ``logging.Logger``.
"""

import logging
from datetime import datetime


class TZFormatter(logging.Formatter):
    """Logging formatter that renders the timestamp in a configured timezone."""

    def __init__(self, fmt=None, tzinfo=None):
        super().__init__(fmt)
        self._tzinfo = tzinfo

    def formatTime(self, record, datefmt=None):
        """Format the record's creation time in the configured tz, honoring ``datefmt``."""
        dt = datetime.fromtimestamp(record.created, self._tzinfo)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")


def configure_logging(settings):
    """Configure and return the ``llmproxy`` logger according to ``settings``."""
    handler = logging.StreamHandler()
    handler.setFormatter(TZFormatter("%(asctime)s [%(levelname)s] %(message)s", settings.log_tzinfo))

    logger = logging.getLogger("llmproxy")
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.handlers = [handler]
    logger.propagate = False
    return logger
