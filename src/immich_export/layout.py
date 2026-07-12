"""Compute where an asset lands in the export tree from a `--layout` template."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .errors import ConfigError
from .models import Asset, AssetType

KNOWN_TOKENS = frozenset({"year", "month", "day", "album", "type"})
UNSORTED_ALBUM = "Unsorted"

_TOKEN_RE = re.compile(r"\{([^{}]*)\}")
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def validate_layout(layout: str) -> None:
    """Reject unknown tokens up front so a typo fails before any network call."""
    unknown = {token for token in _TOKEN_RE.findall(layout) if token not in KNOWN_TOKENS}
    if unknown:
        raise ConfigError(
            f"Unknown layout token(s) {sorted(unknown)} in {layout!r} — "
            f"available: {sorted(KNOWN_TOKENS)}."
        )


def sanitize_component(name: str) -> str:
    """Make a single path component safe on every mainstream filesystem."""
    cleaned = _UNSAFE_CHARS_RE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "_"


def compute_relative_path(asset: Asset, layout: str, album: str | None) -> PurePosixPath:
    """Relative path (under `library/`) for an asset, e.g. `2024/03/IMG_1234.jpg`."""
    taken = asset.taken_at
    values = {
        "year": f"{taken.year:04d}",
        "month": f"{taken.month:02d}",
        "day": f"{taken.day:02d}",
        "album": sanitize_component(album) if album else UNSORTED_ALBUM,
        "type": "videos" if asset.type is AssetType.VIDEO else "images",
    }
    rendered = _TOKEN_RE.sub(lambda m: values[m.group(1)], layout)
    parts = [sanitize_component(part) for part in rendered.split("/") if part.strip()]
    return PurePosixPath(*parts, sanitize_component(asset.original_file_name))


def disambiguate(path: PurePosixPath, asset_id: str) -> PurePosixPath:
    """Resolve a filename collision by suffixing a short asset-id fragment."""
    return path.with_name(f"{path.stem}-{asset_id[:8]}{path.suffix}")
