from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import BinaryIO

import pytest

from immich_export.errors import OutputError
from immich_export.history import (
    HISTORY_DIRECTORY,
    HISTORY_POINTER,
    recover_history_rotation,
    rotate_history,
)
from immich_export.manifest import ManifestWriter

from .test_manifest import _entry


def _history(path: Path, records: int) -> None:
    with ManifestWriter(path) as writer:
        writer.append_batch(_entry(f"a{index}") for index in range(records))


def test_rotation_verifies_archive_and_bounds_active_history(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    _history(path, 5)

    result = rotate_history(path, max_records=5, max_bytes=10**9)

    assert result.rotated is True
    assert result.records == 5
    assert path.read_text() == ""
    pointer = json.loads((tmp_path / HISTORY_POINTER).read_text())
    assert pointer["phase"] == "complete"
    assert len(list((tmp_path / HISTORY_DIRECTORY).glob("*.jsonl"))) == 1
    assert len(list((tmp_path / HISTORY_DIRECTORY).glob("*.json"))) == 1


def test_recovery_finishes_truncation_after_pointer_publication(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    _history(path, 2)
    rotate_history(path, max_records=2, max_bytes=10**9)
    archive = next((tmp_path / HISTORY_DIRECTORY).glob("*.jsonl"))
    path.write_bytes(archive.read_bytes())
    pointer_path = tmp_path / HISTORY_POINTER
    pointer = json.loads(pointer_path.read_text())
    pointer["phase"] = "archive_published"
    pointer_path.write_text(json.dumps(pointer))

    recover_history_rotation(path)

    assert path.read_text() == ""
    assert json.loads(pointer_path.read_text())["phase"] == "complete"


def test_tampered_archive_is_rejected_without_truncating_active_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    _history(path, 2)
    rotate_history(path, max_records=2, max_bytes=10**9)
    path.write_text(_entry("new").to_json_line())
    archive = next((tmp_path / HISTORY_DIRECTORY).glob("*.jsonl"))
    archive.write_text("tampered")

    with pytest.raises(OutputError, match="digest"):
        recover_history_rotation(path)

    assert "new" in path.read_text()


def test_generations_form_a_digest_linked_chain(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    _history(path, 2)
    first = rotate_history(path, max_records=2, max_bytes=10**9)
    _history(path, 2)
    second = rotate_history(path, max_records=2, max_bytes=10**9)

    assert first.generation != second.generation
    metadata = sorted((tmp_path / HISTORY_DIRECTORY).glob("*.json"))
    payloads = [json.loads(item.read_text()) for item in metadata]
    assert sum(item["predecessor_metadata_sha256"] is None for item in payloads) == 1
    assert sum(item["predecessor_metadata_sha256"] is not None for item in payloads) == 1
    recover_history_rotation(path)


def test_failed_archive_copy_retains_the_original_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.jsonl"
    _history(path, 2)
    before = path.read_bytes()

    def interrupt(_source: BinaryIO, _destination: BinaryIO, *, length: int) -> None:
        del length
        raise OSError("injected archive failure")

    monkeypatch.setattr(shutil, "copyfileobj", interrupt)
    with pytest.raises(OutputError, match="Cannot archive"):
        rotate_history(path, max_records=2, max_bytes=10**9)

    assert path.read_bytes() == before
    assert not (tmp_path / HISTORY_POINTER).exists()
