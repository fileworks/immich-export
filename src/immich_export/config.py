"""Validated run configuration, independent of the CLI layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError
from .layout import validate_layout


class ExportMode(StrEnum):
    SELF_CONTAINED = "self-contained"
    SIDECAR = "sidecar"


class SidecarFormat(StrEnum):
    XMP = "xmp"
    NONE = "none"


@dataclass(frozen=True)
class ExportConfig:
    server: str
    api_key: str
    out: Path
    mode: ExportMode = ExportMode.SELF_CONTAINED
    layout: str = "{year}/{month}"
    album_view: bool = True
    people_view: bool = True
    write_sidecars: bool = True
    since: datetime | None = None
    resume: bool = True
    include_hidden: bool = False
    library_root: Path | None = None
    """Where the existing Storage-Template tree lives (sidecar mode only)."""
    concurrency: int = 4

    def validate(self) -> None:
        parsed = urlparse(self.server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"--server must be an http(s) URL, got {self.server!r} "
                "(e.g. https://immich.local:2283)."
            )
        if not self.api_key:
            raise ConfigError(
                "No API key — pass --api-key or set $IMMICH_API_KEY "
                "(Immich → Account Settings → API Keys)."
            )
        validate_layout(self.layout)
        if self.mode is ExportMode.SIDECAR:
            if self.library_root is None:
                raise ConfigError("--mode sidecar requires --library-root <storage-template dir>.")
            if not self.library_root.is_dir():
                raise ConfigError(f"--library-root {self.library_root} is not a directory.")
        if self.concurrency < 1:
            raise ConfigError("--concurrency must be at least 1.")
