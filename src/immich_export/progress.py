"""Live progress for long runs — a 50k-asset export must never look frozen.

Two audiences, one class: on a terminal we repaint a single line; under cron or
a redirected log we emit a line every `LOG_EVERY` assets instead, so a nightly
job's log stays readable rather than filling with carriage returns.
"""

from __future__ import annotations

import sys
import time
from types import TracebackType
from typing import Self, TextIO

LOG_EVERY = 500
"""Assets between progress lines when stderr is not a terminal."""

_REPAINT_INTERVAL = 0.2
"""Seconds between terminal repaints — enough to look live, cheap enough to ignore."""


def _plural(count: int) -> str:
    return f"{count:,}"


class Progress:
    """Counts assets as they land and reports at a human pace."""

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._tty = enabled and self._stream.isatty()
        self._enabled = enabled
        self._started = time.monotonic()
        self._last_paint = 0.0
        self._exported = 0
        self._skipped = 0
        self._errors = 0
        self._painted = False
        self._closed = False

    def _line(self) -> str:
        done = self._exported + self._skipped
        elapsed = max(time.monotonic() - self._started, 1e-6)
        rate = done / elapsed
        parts = [f"{_plural(self._exported)} exported"]
        if self._skipped:
            parts.append(f"{_plural(self._skipped)} up to date")
        if self._errors:
            parts.append(f"{_plural(self._errors)} failed")
        return f"{', '.join(parts)} — {rate:.1f}/s"

    def _paint(self, *, force: bool) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if self._tty:
            if not force and now - self._last_paint < _REPAINT_INTERVAL:
                return
            self._stream.write(f"\r\033[2K{self._line()}")
            self._stream.flush()
            self._painted = True
        elif force or (self._exported + self._skipped) % LOG_EVERY == 0:
            self._stream.write(f"{self._line()}\n")
            self._stream.flush()
        self._last_paint = now

    def exported(self) -> None:
        self._exported += 1
        self._paint(force=False)

    def skipped(self) -> None:
        self._skipped += 1
        self._paint(force=False)

    def failed(self) -> None:
        self._errors += 1
        self._paint(force=False)

    def note(self, message: str) -> None:
        """Print a standalone status line without clobbering the live counter."""
        if not self._enabled:
            return
        if self._painted:
            self._stream.write("\r\033[2K")
            self._painted = False
        self._stream.write(f"{message}\n")
        self._stream.flush()

    def close(self) -> None:
        """Finish the live line. Idempotent — the exporter closes it before the
        view-building phase, and the context manager closes it again on exit."""
        if self._closed or not self._enabled:
            return
        self._closed = True
        if self._exported + self._skipped + self._errors == 0:
            return
        self._paint(force=True)
        if self._tty and self._painted:
            self._stream.write("\n")
            self._stream.flush()
            self._painted = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
