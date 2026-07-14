"""Append-only `manifest.jsonl` (machine truth) + derived `manifest.csv` (human audit).

Resume works off the manifest: an asset whose id appears with an unchanged
checksum — and whose file still exists — is skipped on re-run. The jsonl is
append-only (last line per asset id wins), so an interrupted run loses at most
the in-flight asset.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


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


def load_index(
    manifest_path: Path, *, warnings: list[str] | None = None
) -> dict[str, ManifestEntry]:
    """Latest manifest entry per asset id; empty dict if no manifest yet.

    A run killed mid-write (or a full disk) leaves a truncated final line. That
    must not poison every future run, so an unparseable line is skipped and
    reported — the assets it described are simply re-exported.
    """
    index: dict[str, ManifestEntry] = {}
    if not manifest_path.is_file():
        return index
    damaged = 0
    with manifest_path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = ManifestEntry.model_validate_json(line)
            except ValidationError:
                damaged += 1
                logger.warning(
                    "%s line %d is unreadable (truncated by an interrupted run?) — "
                    "skipping it; any assets it covered will be exported again.",
                    manifest_path,
                    number,
                )
                continue
            index[entry.asset_id] = entry
    if damaged and warnings is not None:
        warnings.append(
            f"{manifest_path.name}: skipped {damaged} unreadable line(s); "
            "the assets they covered were re-exported."
        )
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
