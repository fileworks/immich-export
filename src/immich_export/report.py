"""End-of-run report: counts, warnings, per-asset errors, timing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExportReport:
    server: str = ""
    server_version: str = ""
    mode: str = ""
    total: int = 0
    exported: int = 0
    skipped: int = 0
    album_links: int = 0
    people_links: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    duration_seconds: float = 0.0

    def record_error(self, subject: str, message: str) -> None:
        self.errors.append((subject, message))

    def finish(self) -> None:
        self.duration_seconds = time.monotonic() - self.started

    def render(self) -> str:
        lines = [
            "immich-export report",
            "====================",
            f"server:        {self.server} ({self.server_version})",
            f"mode:          {self.mode}",
            f"assets total:  {self.total}",
            f"exported:      {self.exported}",
            f"skipped:       {self.skipped} (already up to date)",
            f"errors:        {len(self.errors)}",
            f"album links:   {self.album_links}",
            f"people links:  {self.people_links}",
            f"duration:      {self.duration_seconds:.1f}s",
        ]
        if self.warnings:
            lines += ["", "warnings:"]
            lines += [f"  - {warning}" for warning in self.warnings]
        if self.errors:
            lines += ["", "errors (asset → reason):"]
            lines += [f"  - {subject}: {message}" for subject, message in self.errors]
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.write_text(self.render(), encoding="utf-8")
