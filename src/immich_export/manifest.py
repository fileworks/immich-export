"""Verified asset state, append-only history, and atomic current projections."""

from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from .errors import OutputError

logger = logging.getLogger(__name__)


class AssetState(BaseModel):
    """Canonical, exhaustive state for one verified export asset.

    ``verified_at`` records when verification happened and is intentionally the
    only field excluded from equivalence. Any future state field automatically
    participates in equality.
    """

    model_config = ConfigDict(frozen=True)

    asset_id: str
    checksum: str
    path: str
    """Resolved media path relative to the configured media root."""
    file_name: str
    original_path: str = ""
    taken_at: datetime
    type: str
    favorite: bool = False
    description: str | None = None
    albums: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    mode: str = "self-contained"
    layout: str = "{year}/{month}"
    write_sidecars: bool = True
    include_hidden: bool = False
    visibilities: list[str] = Field(default_factory=lambda: ["timeline", "archive"])
    library_root: str | None = None
    verified_at: datetime = Field(validation_alias=AliasChoices("verified_at", "exported_at"))

    @property
    def exported_at(self) -> datetime:
        """Compatibility name used by manifests written before current snapshots."""
        return self.verified_at

    def equivalent(self, other: AssetState) -> bool:
        return self.model_dump(exclude={"verified_at"}) == other.model_dump(exclude={"verified_at"})

    def same_scope(self, other: AssetState) -> bool:
        fields = (
            "mode",
            "layout",
            "write_sidecars",
            "include_hidden",
            "visibilities",
            "library_root",
        )
        return all(getattr(self, field) == getattr(other, field) for field in fields)

    def to_json_line(self) -> str:
        return self.model_dump_json() + "\n"


# Public compatibility alias: the history and current snapshot now use the same
# canonical type, but downstream imports of ManifestEntry keep working.
ManifestEntry = AssetState


CSV_COLUMNS = [
    "asset_id",
    "path",
    "file_name",
    "original_path",
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
    "mode",
    "layout",
]


def _validate_line(line: str) -> AssetState:
    """Read current and legacy history rows into the canonical state."""
    try:
        return AssetState.model_validate_json(line)
    except ValidationError as original:
        # v0.0.3 called the timestamp exported_at. Keep history readable without
        # weakening validation of the authoritative current snapshot.
        try:
            import json

            payload = json.loads(line)
            if "verified_at" not in payload and "exported_at" in payload:
                payload["verified_at"] = payload.pop("exported_at")
            return AssetState.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise original from exc


def load_index(manifest_path: Path, *, warnings: list[str] | None = None) -> dict[str, AssetState]:
    """Load append-only history; unreadable rows are reported and skipped."""
    index: dict[str, AssetState] = {}
    if not manifest_path.is_file():
        return index
    damaged = 0
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            for number, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = _validate_line(line)
                except ValidationError:
                    damaged += 1
                    logger.warning(
                        "%s line %d is unreadable — skipping it; its asset will be verified again.",
                        manifest_path,
                        number,
                    )
                    continue
                index[entry.asset_id] = entry
    except OSError as exc:
        raise OutputError(f"Cannot read export history {manifest_path}: {exc}") from exc
    if damaged and warnings is not None:
        warnings.append(
            f"{manifest_path.name}: skipped {damaged} unreadable line(s); "
            "the assets they covered will be verified again."
        )
    return index


def load_current(snapshot_path: Path) -> dict[str, AssetState]:
    """Load authoritative current state strictly; corruption aborts the run."""
    if not snapshot_path.is_file():
        return {}
    index: dict[str, AssetState] = {}
    try:
        with snapshot_path.open(encoding="utf-8") as fh:
            for number, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = _validate_line(line)
                except ValidationError as exc:
                    raise OutputError(
                        f"Current snapshot {snapshot_path} line {number} is invalid; "
                        "the previous state was not replaced."
                    ) from exc
                if entry.asset_id in index:
                    raise OutputError(
                        f"Current snapshot {snapshot_path} repeats asset {entry.asset_id}."
                    )
                index[entry.asset_id] = entry
    except OutputError:
        raise
    except OSError as exc:
        raise OutputError(f"Cannot read current snapshot {snapshot_path}: {exc}") from exc
    return index


def atomic_write_text(path: Path, content: str, *, operation: str) -> None:
    """Durably replace one text file using a unique same-directory temporary."""
    fd = -1
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise OutputError(f"Cannot {operation} at {path}: {exc}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                raise OutputError(f"Cannot close temporary output for {path}: {exc}") from exc
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise OutputError(f"Cannot clean temporary output {temporary}: {exc}") from exc


def write_current(snapshot_path: Path, entries: Mapping[str, AssetState]) -> int:
    """Atomically publish the complete current snapshot in stable asset-id order."""
    body = "".join(entries[asset_id].to_json_line() for asset_id in sorted(entries))
    atomic_write_text(snapshot_path, body, operation="publish current manifest")
    return len(entries)


class ManifestWriter:
    """Single-owner append-only history writer with one sync per committed group."""

    def __init__(self, manifest_path: Path) -> None:
        self._path = manifest_path
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = manifest_path.open("a", encoding="utf-8")
        except OSError as exc:
            raise OutputError(f"Cannot open export history {manifest_path}: {exc}") from exc
        self.synchronizations = 0

    def append(self, entry: AssetState) -> None:
        self.append_batch((entry,))

    def append_batch(self, entries: Iterable[AssetState]) -> int:
        """Append and synchronize one group, returning its durable record count."""
        rendered = "".join(entry.to_json_line() for entry in entries)
        if not rendered:
            return 0
        try:
            self._fh.write(rendered)
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            raise OutputError(f"Cannot append export history {self._path}: {exc}") from exc
        count = rendered.count("\n")
        self.synchronizations += 1
        return count

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError as exc:
            raise OutputError(f"Cannot close export history {self._path}: {exc}") from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _render_csv(entries: Iterable[AssetState]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(CSV_COLUMNS)
    for entry in sorted(entries, key=lambda item: (item.path, item.asset_id)):
        writer.writerow(
            [
                entry.asset_id,
                entry.path,
                entry.file_name,
                entry.original_path,
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
                entry.mode,
                entry.layout,
            ]
        )
    return stream.getvalue()


def write_current_csv(entries: Mapping[str, AssetState], csv_path: Path) -> int:
    """Atomically write the human-readable projection of current state."""
    atomic_write_text(csv_path, _render_csv(entries.values()), operation="write current CSV")
    return len(entries)


def write_csv(manifest_path: Path, csv_path: Path) -> int:
    """Compatibility helper: project the latest append-only history rows."""
    index = load_index(manifest_path)
    atomic_write_text(csv_path, _render_csv(index.values()), operation="write manifest CSV")
    return len(index)
