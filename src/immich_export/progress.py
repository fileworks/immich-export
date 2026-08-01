"""Typed, bounded progress rendering for terminals, cron, and logfiles."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Self, TextIO

LOG_EVERY = 500
LOG_INTERVAL_SECONDS = 5.0
_REPAINT_INTERVAL = 0.2
MAX_RETAINED_EVENTS = 1_024

ProgressPhase = Literal[
    "connection",
    "membership",
    "export",
    "verification",
    "publication",
    "completion",
]


@dataclass(frozen=True)
class ProgressEvent:
    phase: ProgressPhase
    current: int
    total: int | None
    durable: int
    failures: int
    rate: float
    elapsed: float
    eta_seconds: float | None


def _plural(count: int) -> str:
    return f"{count:,}"


class Progress:
    """One event model with compact interactive and redirected renderers."""

    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._tty = enabled and self._stream.isatty()
        self._enabled = enabled
        self._started = time.monotonic()
        self._phase_started = self._started
        self._last_paint = self._started
        self._last_log = self._started
        self._exported = 0
        self._skipped = 0
        self._errors = 0
        self._durable = 0
        self._phase: ProgressPhase = "connection"
        self._phase_current = 0
        self._phase_total: int | None = None
        self._painted = False
        self._closed = False
        self._lock = threading.RLock()
        self._stop_heartbeat = threading.Event()
        self._heartbeat: threading.Thread | None = None
        # Retain enough structured evidence for diagnostics without making
        # progress tracking itself scale with the number of exported assets.
        self.events: deque[ProgressEvent] = deque(maxlen=MAX_RETAINED_EVENTS)

    def phase(self, phase: ProgressPhase, *, total: int | None = None) -> None:
        with self._lock:
            self._phase = phase
            self._phase_current = 0
            self._phase_total = total
            self._phase_started = time.monotonic()
            self._paint(force=True)

    def advanced(self, count: int = 1) -> None:
        with self._lock:
            self._phase_current += count
            self._paint(force=self._phase_current == self._phase_total)

    def exported(self, *, durable: bool = False) -> None:
        with self._lock:
            self._exported += 1
            self._phase_current += 1
            if durable:
                self._durable += 1
            self._paint(force=False)

    def skipped(self) -> None:
        with self._lock:
            self._skipped += 1
            self._phase_current += 1
            self._paint(force=False)

    def failed(self) -> None:
        with self._lock:
            self._errors += 1
            self._phase_current += 1
            self._paint(force=False)

    def event(self) -> ProgressEvent:
        elapsed = max(time.monotonic() - self._phase_started, 1e-6)
        rate = self._phase_current / elapsed
        eta = None
        if (
            self._phase_total is not None
            and self._phase_current >= 5
            and rate > 0
            and self._phase_current <= self._phase_total
        ):
            eta = (self._phase_total - self._phase_current) / rate
        return ProgressEvent(
            phase=self._phase,
            current=self._phase_current,
            total=self._phase_total,
            durable=self._durable,
            failures=self._errors,
            rate=rate,
            elapsed=max(time.monotonic() - self._started, 0.0),
            eta_seconds=eta,
        )

    def _line(self) -> str:
        event = self.event()
        total = f"/{_plural(event.total)}" if event.total is not None else ""
        eta = f", ETA {event.eta_seconds:.0f}s" if event.eta_seconds is not None else ""
        outcomes = [f"{_plural(self._exported)} exported"]
        if self._skipped:
            outcomes.append(f"{_plural(self._skipped)} up to date")
        if self._errors:
            outcomes.append(f"{_plural(self._errors)} failed")
        return (
            f"{event.phase}: {_plural(event.current)}{total}, "
            f"{_plural(event.durable)} durable, {_plural(event.failures)} failed — "
            f"{event.rate:.1f}/s, {event.elapsed:.1f}s{eta} ({', '.join(outcomes)})"
        )

    def _paint(self, *, force: bool) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        event = self.event()
        emitted = False
        if self._tty:
            if force or now - self._last_paint >= _REPAINT_INTERVAL:
                self._stream.write(f"\r\033[2K{self._line()}")
                self._stream.flush()
                self._painted = True
                emitted = True
        elif force or (
            self._phase_current > 0
            and (
                self._phase_current % LOG_EVERY == 0
                or now - self._last_paint >= LOG_INTERVAL_SECONDS
            )
        ):
            self._stream.write(f"{self._line()}\n")
            self._stream.flush()
            emitted = True
        if force or now - self._last_log >= LOG_INTERVAL_SECONDS:
            logging.getLogger("immich_export.progress").info(self._line())
            self._last_log = now
            emitted = True
        if not emitted:
            return
        self.events.append(event)
        self._last_paint = now

    def note(self, message: str) -> None:
        if not self._enabled:
            return
        if self._painted:
            self._stream.write("\r\033[2K")
            self._painted = False
        self._stream.write(f"{message}\n")
        self._stream.flush()
        logging.getLogger("immich_export.progress").info("%s", message)

    def close(self) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1.0)
        with self._lock:
            if self._closed or not self._enabled:
                return
            self._closed = True
            self._phase = "completion"
            self._phase_current = self._exported + self._skipped + self._errors
            self._phase_total = self._phase_current
            self._paint(force=True)
            if self._tty and self._painted:
                self._stream.write("\n")
                self._stream.flush()
                self._painted = False

    def __enter__(self) -> Self:
        if self._enabled and self._heartbeat is None:
            self._heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                name="immich-export-progress",
                daemon=True,
            )
            self._heartbeat.start()
        return self

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(LOG_INTERVAL_SECONDS):
            with self._lock:
                if self._closed:
                    return
                self._paint(force=True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
