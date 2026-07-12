"""Append-only `manifest.jsonl` (machine truth) + derived `manifest.csv` (human audit).

Resume works off the manifest: an asset whose id appears with an unchanged
checksum — and whose file still exists — is skipped on re-run. The jsonl is
append-only (last line per asset id wins), so an interrupted run loses at most
the in-flight asset.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict


class ManifestEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    checksum: str
    path: str
    """Path of the exported/located media file, relative to the export root."""
    file_name: str
    taken_at: datetime
    type: str
    favorite: bool = False
    description: str | None = None
    albums: list[str] = []
    people: list[str] = []
    tags: list[str] = []
    latitude: float | None = None
    longitude: float | None = None
    exported_at: datetime

    def to_json_line(self) -> str:
        return self.model_dump_json() + "\n"


CSV_COLUMNS = [
    "asset_id",
    "path",
    "file_name",
    "taken_at",
    "type",
    "favorite",
    "albums",
    "people",
    "tags",
    "description",
    "latitude",
    "longitude",
    "checksum",
]


def load_index(manifest_path: Path) -> dict[str, ManifestEntry]:
    """Latest manifest entry per asset id; empty dict if no manifest yet."""
    index: dict[str, ManifestEntry] = {}
    if not manifest_path.is_file():
        return index
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entry = ManifestEntry.model_validate_json(line)
                index[entry.asset_id] = entry
    return index


class ManifestWriter:
    def __init__(self, manifest_path: Path) -> None:
        self._path = manifest_path
        self._fh = manifest_path.open("a", encoding="utf-8")

    def append(self, entry: ManifestEntry) -> None:
        self._fh.write(entry.to_json_line())
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def write_csv(manifest_path: Path, csv_path: Path) -> int:
    """Regenerate `manifest.csv` from the jsonl; returns the number of rows."""
    index = load_index(manifest_path)
    entries = sorted(index.values(), key=lambda e: e.path)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for entry in entries:
            writer.writerow(
                [
                    entry.asset_id,
                    entry.path,
                    entry.file_name,
                    entry.taken_at.isoformat(),
                    entry.type,
                    entry.favorite,
                    "; ".join(entry.albums),
                    "; ".join(entry.people),
                    "; ".join(entry.tags),
                    entry.description or "",
                    entry.latitude if entry.latitude is not None else "",
                    entry.longitude if entry.longitude is not None else "",
                    entry.checksum,
                ]
            )
    return len(entries)
