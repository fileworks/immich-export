"""Digest-linked, recoverable rotation of append-only manifest history."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import OutputError
from .manifest import atomic_write_text

HISTORY_DIRECTORY = "manifest-history"
HISTORY_POINTER = "manifest-history-current.json"


@dataclass(frozen=True)
class RotationResult:
    rotated: bool
    records: int = 0
    bytes_archived: int = 0
    generation: str | None = None


def recover_history_rotation(manifest_path: Path) -> None:
    """Validate the authoritative chain and finish an interrupted truncation."""
    pointer_path = manifest_path.parent / HISTORY_POINTER
    if not pointer_path.exists() and not pointer_path.is_symlink():
        return
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise OutputError(f"Manifest history pointer {pointer_path} is unsafe.")
    pointer = _read_json(pointer_path)
    latest_digest = _require_string(pointer, "metadata_sha256")
    phase = _require_string(pointer, "phase")
    if phase not in {"archive_published", "complete"}:
        raise OutputError(f"History pointer {pointer_path} has an invalid phase.")
    directory = _safe_history_directory(manifest_path.parent, create=False)
    _validate_chain(directory, latest_digest)
    if phase == "archive_published":
        expected_active = _require_string(pointer, "archived_active_sha256")
        if not manifest_path.is_file() or _sha256(manifest_path) != expected_active:
            raise OutputError(
                "Active manifest changed after archive publication; refusing recovery truncation."
            )
        atomic_write_text(manifest_path, "", operation="finish history rotation")
        pointer["phase"] = "complete"
        _write_json(pointer_path, pointer, operation="complete history rotation")


def rotate_history(
    manifest_path: Path,
    *,
    max_records: int,
    max_bytes: int,
) -> RotationResult:
    """Archive a verified active generation and atomically bound the active file."""
    recover_history_rotation(manifest_path)
    if not manifest_path.is_file():
        return RotationResult(False)
    size = manifest_path.stat().st_size
    records = _count_records(manifest_path)
    if records < max_records and size < max_bytes:
        return RotationResult(False)

    root = manifest_path.parent
    directory = _safe_history_directory(root, create=True)
    generation = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}"
    archive = directory / f"{generation}.jsonl"
    metadata_path = directory / f"{generation}.json"
    pointer_path = root / HISTORY_POINTER
    predecessor = None
    if pointer_path.exists():
        predecessor = _require_string(_read_json(pointer_path), "metadata_sha256")

    try:
        with manifest_path.open("rb") as source, archive.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        archive.unlink(missing_ok=True)
        raise OutputError(f"Cannot archive export history {manifest_path}: {exc}") from exc

    archive_digest = _sha256(archive)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "generation": generation,
        "archive": archive.name,
        "archive_sha256": archive_digest,
        "record_count": records,
        "byte_count": size,
        "predecessor_metadata_sha256": predecessor,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_json(metadata_path, metadata, operation="publish history generation metadata")
    metadata_digest = _sha256(metadata_path)
    _validate_generation(directory, metadata_digest)
    pointer = {
        "schema_version": 1,
        "metadata": metadata_path.name,
        "metadata_sha256": metadata_digest,
        "archived_active_sha256": archive_digest,
        "phase": "archive_published",
    }
    _write_json(pointer_path, pointer, operation="publish history generation pointer")
    atomic_write_text(manifest_path, "", operation="start bounded active history")
    pointer["phase"] = "complete"
    _write_json(pointer_path, pointer, operation="complete history rotation")
    return RotationResult(True, records, size, generation)


def _validate_chain(directory: Path, latest_digest: str) -> None:
    metadata_by_digest: dict[str, Path] = {}
    for path in directory.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            raise OutputError(f"Manifest history metadata path {path} is unsafe.")
        metadata_by_digest[_sha256(path)] = path
    seen: set[str] = set()
    current: str | None = latest_digest
    while current is not None:
        if current in seen:
            raise OutputError("Manifest history generation chain contains a cycle.")
        seen.add(current)
        metadata_path = metadata_by_digest.get(current)
        if metadata_path is None:
            raise OutputError("Manifest history generation metadata is missing.")
        metadata = _validate_metadata_file(directory, metadata_path)
        predecessor = metadata.get("predecessor_metadata_sha256")
        if predecessor is not None and not isinstance(predecessor, str):
            raise OutputError("Manifest history predecessor digest is invalid.")
        current = predecessor


def _validate_generation(directory: Path, metadata_digest: str) -> dict[str, Any]:
    candidates = list(directory.glob("*.json"))
    if any(path.is_symlink() or not path.is_file() for path in candidates):
        raise OutputError("Manifest history generation metadata path is unsafe.")
    matches = [path for path in candidates if _sha256(path) == metadata_digest]
    if len(matches) != 1:
        raise OutputError("Manifest history generation metadata is missing or ambiguous.")
    return _validate_metadata_file(directory, matches[0])


def _validate_metadata_file(directory: Path, metadata_path: Path) -> dict[str, Any]:
    metadata = _read_json(metadata_path)
    archive_name = _require_string(metadata, "archive")
    archive = directory / archive_name
    if (
        archive.parent != directory
        or archive.name != archive_name
        or archive.is_symlink()
        or not archive.is_file()
    ):
        raise OutputError("Manifest history archive path is unsafe.")
    if _sha256(archive) != _require_string(metadata, "archive_sha256"):
        raise OutputError(f"Manifest history archive {archive} failed digest verification.")
    return metadata


def _write_json(path: Path, payload: dict[str, Any], *, operation: str) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        operation=operation,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise OutputError(f"Cannot read manifest history metadata {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise OutputError(f"Manifest history metadata {path} has an invalid schema.")
    return payload


def _require_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise OutputError(f"Manifest history metadata field {field!r} is invalid.")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OutputError(f"Cannot verify manifest history file {path}: {exc}") from exc
    return digest.hexdigest()


def _count_records(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: stream.read(1024 * 1024), b""))
    except OSError as exc:
        raise OutputError(f"Cannot inspect export history {path}: {exc}") from exc


def _safe_history_directory(root: Path, *, create: bool) -> Path:
    directory = root / HISTORY_DIRECTORY
    try:
        if create:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"Cannot create manifest history directory {directory}: {exc}") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise OutputError(f"Manifest history directory {directory} is unsafe.")
    return directory
