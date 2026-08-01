"""Verified asset state, append-only history, and atomic current projections."""

from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import sqlite3
import stat
import tempfile
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    ValuesView,
)
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

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
    return {entry.asset_id: entry for entry in iter_current(snapshot_path)}


def iter_current(snapshot_path: Path) -> Iterator[AssetState]:
    """Strictly stream authoritative current state without whole-file retention."""
    if not snapshot_path.is_file():
        return
    seen: set[str] = set()
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
                if entry.asset_id in seen:
                    raise OutputError(
                        f"Current snapshot {snapshot_path} repeats asset {entry.asset_id}."
                    )
                seen.add(entry.asset_id)
                yield entry
    except OutputError:
        raise
    except OSError as exc:
        raise OutputError(f"Cannot read current snapshot {snapshot_path}: {exc}") from exc


class _StateValues(ValuesView[AssetState]):
    def __init__(self, mapping: DiskStateMap) -> None:
        super().__init__(mapping)
        self._state_mapping = mapping

    def __iter__(self) -> Iterator[AssetState]:
        rows = self._state_mapping._connection.execute(
            "SELECT payload FROM state ORDER BY asset_id"
        )
        for row in rows:
            yield _validate_line(str(row[0]))


class DiskStateMap(MutableMapping[str, AssetState]):
    """Temporary SQLite-backed asset mapping used during one export run."""

    def __init__(self, entries: Iterable[AssetState] = ()) -> None:
        with tempfile.NamedTemporaryFile(
            prefix="immich-state-",
            suffix=".sqlite",
            delete=False,
        ) as handle:
            self._path = Path(handle.name)
        self._connection = sqlite3.connect(self._path)
        self._closed = False
        try:
            self._connection.execute("PRAGMA journal_mode = MEMORY")
            self._connection.execute("PRAGMA synchronous = OFF")
            self._connection.execute(
                "CREATE TABLE state ("
                "asset_id TEXT PRIMARY KEY, path TEXT NOT NULL, payload TEXT NOT NULL"
                ")"
            )
            self._connection.executemany(
                "INSERT INTO state(asset_id, path, payload) VALUES (?, ?, ?)",
                ((entry.asset_id, entry.path, entry.model_dump_json()) for entry in entries),
            )
            self._connection.commit()
        except BaseException:
            self.close()
            raise

    def __getitem__(self, key: str) -> AssetState:
        row = self._connection.execute(
            "SELECT payload FROM state WHERE asset_id = ?",
            (key,),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return _validate_line(str(row[0]))

    def __setitem__(self, key: str, value: AssetState) -> None:
        if key != value.asset_id:
            raise ValueError("state mapping key must equal AssetState.asset_id")
        self._connection.execute(
            "INSERT INTO state(asset_id, path, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "path = excluded.path, payload = excluded.payload",
            (key, value.path, value.model_dump_json()),
        )

    def __delitem__(self, key: str) -> None:
        cursor = self._connection.execute("DELETE FROM state WHERE asset_id = ?", (key,))
        if cursor.rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        rows = self._connection.execute("SELECT asset_id FROM state ORDER BY asset_id")
        yield from (str(row[0]) for row in rows)

    def __len__(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM state").fetchone()[0])

    def values(self) -> ValuesView[AssetState]:
        return _StateValues(self)

    def copy(self) -> DiskStateMap:
        """Return an independent disk-backed snapshot."""
        return DiskStateMap(self.values())

    def ordered_by_path(self) -> Iterator[AssetState]:
        """Stream values in stable human-projection order."""
        rows = self._connection.execute("SELECT payload FROM state ORDER BY path, asset_id")
        for row in rows:
            yield _validate_line(str(row[0]))

    def close(self) -> None:
        """Close and remove this run-local database."""
        if self._closed:
            return
        self._closed = True
        self._connection.close()
        self._path.unlink(missing_ok=True)

    def __del__(self) -> None:
        if hasattr(self, "_closed"):
            self.close()


def atomic_write_text(
    path: Path,
    content: str,
    *,
    operation: str,
    boundary: Path | None = None,
) -> None:
    """Durably replace one text file using a unique same-directory temporary."""
    if boundary is None:
        atomic_write_stream(
            path,
            lambda stream: stream.write(content),
            operation=operation,
        )
        return
    atomic_write_stream(
        path,
        lambda stream: stream.write(content),
        operation=operation,
        boundary=boundary,
    )


def atomic_write_stream(
    path: Path,
    write: Callable[[TextIO], object],
    *,
    operation: str,
    boundary: Path | None = None,
) -> None:
    """Durably replace a text file without retaining its complete body.

    When ``boundary`` is supplied on a platform with directory-descriptor
    support, every path component and the final rename remain anchored below
    that already-open directory. This closes the symlink-swap window between
    validation and publication.
    """
    if boundary is not None and _supports_anchored_replace():
        _atomic_write_stream_anchored(path, write, operation=operation, boundary=boundary)
        return

    fd = -1
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fd = -1
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        if boundary is not None:
            _validate_fallback_boundary(path, boundary)
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


def _supports_anchored_replace() -> bool:
    """Return whether this runtime can safely traverse and replace by dir fd."""
    return (
        os.open in os.supports_dir_fd
        and os.replace in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )


def _validate_fallback_boundary(path: Path, boundary: Path) -> None:
    """Best-effort confinement for platforms without directory descriptors."""
    try:
        root = boundary.resolve(strict=True)
        path.parent.resolve(strict=True).relative_to(root)
        relative = path.relative_to(boundary)
    except (OSError, ValueError) as exc:
        raise OutputError(f"Refusing output path outside the configured root: {path}.") from exc
    current = boundary
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise OutputError(f"Refusing symlinked output path: {path}.")


def _atomic_write_stream_anchored(
    path: Path,
    write: Callable[[TextIO], object],
    *,
    operation: str,
    boundary: Path,
) -> None:
    """Publish through open directory descriptors so path swaps cannot escape."""
    root_fd = -1
    parent_fd = -1
    output_fd = -1
    temporary_name: str | None = None
    try:
        relative = path.relative_to(boundary)
        if not relative.parts or relative.name in {"", ".", ".."}:
            raise ValueError("invalid output path")
        root = boundary.resolve(strict=True)
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        parent_fd = os.dup(root_fd)
        for component in relative.parts[:-1]:
            if component in {"", ".", ".."}:
                raise ValueError("invalid output path component")
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd

        try:
            leaf = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            leaf = None
        if leaf is not None and stat.S_ISLNK(leaf.st_mode):
            raise ValueError("symlinked output leaf")

        for _ in range(128):
            candidate = f".{relative.name}.{secrets.token_hex(8)}.tmp"
            try:
                output_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise OSError("cannot allocate a unique temporary output")

        with os.fdopen(output_fd, "w", encoding="utf-8", newline="") as fh:
            output_fd = -1
            write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(
            temporary_name,
            relative.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    except (OSError, ValueError) as exc:
        raise OutputError(f"Cannot {operation} at {path}: {exc}") from exc
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if temporary_name is not None and parent_fd >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if root_fd >= 0:
            os.close(root_fd)


def write_current(snapshot_path: Path, entries: Mapping[str, AssetState]) -> int:
    """Atomically publish the complete current snapshot in stable asset-id order."""

    def render(stream: TextIO) -> None:
        asset_ids = entries if isinstance(entries, DiskStateMap) else sorted(entries)
        for asset_id in asset_ids:
            stream.write(entries[asset_id].to_json_line())

    atomic_write_stream(snapshot_path, render, operation="publish current manifest")
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

    def render(stream: TextIO) -> None:
        writer = csv.writer(stream)
        writer.writerow(CSV_COLUMNS)
        ordered = (
            entries.ordered_by_path()
            if isinstance(entries, DiskStateMap)
            else sorted(entries.values(), key=lambda item: (item.path, item.asset_id))
        )
        for entry in ordered:
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

    atomic_write_stream(csv_path, render, operation="write current CSV")
    return len(entries)


def write_csv(manifest_path: Path, csv_path: Path) -> int:
    """Compatibility helper: project the latest append-only history rows."""
    index = load_index(manifest_path)
    atomic_write_text(csv_path, _render_csv(index.values()), operation="write manifest CSV")
    return len(index)
