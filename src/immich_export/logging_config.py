"""Bounded persistent logging with central credential and URL-query redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .errors import OutputError

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
_secrets: set[str] = set()
_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = _QUERY.sub(r"\1?[REDACTED]", record.getMessage())
        for secret in sorted(_secrets, key=len, reverse=True):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


class _ExcludeProgressConsole(logging.Filter):
    """Progress already owns the concise terminal renderer."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "immich_export.progress"


def configure_logging(log_file: Path, *, verbose: bool, secrets: tuple[str, ...]) -> None:
    _secrets.update(secret for secret in secrets if secret)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"Cannot create logfile directory {log_file.parent}: {exc}") from exc
    root = logging.getLogger()
    for handler in tuple(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    redactor = _RedactingFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s")
    )
    console.addFilter(redactor)
    console.addFilter(_ExcludeProgressConsole())

    try:
        logfile = RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        raise OutputError(f"Cannot open rotating logfile {log_file}: {exc}") from exc
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logfile.addFilter(redactor)
    root.addHandler(console)
    root.addHandler(logfile)
