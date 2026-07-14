from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from immich_export.manifest import ManifestEntry, ManifestWriter, load_index, write_csv


def _entry(
    asset_id: str, checksum: str = "abc=", path: str = "library/2024/03/a.jpg"
) -> ManifestEntry:
    return ManifestEntry(
        asset_id=asset_id,
        checksum=checksum,
        path=path,
        file_name="a.jpg",
        taken_at=datetime(2024, 3, 15, tzinfo=UTC),
        type="IMAGE",
        albums=["Japan 2019"],
        people=["Anna"],
        tags=["travel/japan"],
        exported_at=datetime(2026, 7, 6, tzinfo=UTC),
    )


class TestManifestRoundTrip:
    def test_append_then_load(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.jsonl"
        with ManifestWriter(manifest) as writer:
            writer.append(_entry("a1"))
            writer.append(_entry("a2"))
        index = load_index(manifest)
        assert set(index) == {"a1", "a2"}
        assert index["a1"].albums == ["Japan 2019"]

    def test_last_line_wins_per_asset(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.jsonl"
        with ManifestWriter(manifest) as writer:
            writer.append(_entry("a1", checksum="old="))
            writer.append(_entry("a1", checksum="new="))
        assert load_index(manifest)["a1"].checksum == "new="

    def test_missing_manifest_is_empty_index(self, tmp_path: Path) -> None:
        assert load_index(tmp_path / "nope.jsonl") == {}


def test_write_csv_dedupes_and_orders(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    with ManifestWriter(manifest) as writer:
        writer.append(_entry("a2", path="library/2024/03/b.jpg"))
        writer.append(_entry("a1", path="library/2019/04/a.jpg"))
        writer.append(_entry("a1", path="library/2019/04/a.jpg", checksum="new="))
    csv_path = tmp_path / "manifest.csv"
    rows = write_csv(manifest, csv_path)
    assert rows == 2
    with csv_path.open() as fh:
        parsed = list(csv.DictReader(fh))
    assert [row["path"] for row in parsed] == ["library/2019/04/a.jpg", "library/2024/03/b.jpg"]
    assert parsed[0]["checksum"] == "new="
    assert parsed[0]["people"] == "Anna"


class TestDamagedManifest:
    """A run killed mid-write leaves a truncated line; resume must survive it."""

    def test_truncated_final_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.jsonl"
        with ManifestWriter(manifest) as writer:
            writer.append(_entry("a1"))
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write('{"asset_id":"a2","checksum":"y","pa')  # killed mid-write

        warnings: list[str] = []
        index = load_index(manifest, warnings=warnings)

        assert list(index) == ["a1"]  # the good entry still resumes
        assert len(warnings) == 1
        assert "unreadable" in warnings[0]

    def test_intact_manifest_reports_no_warnings(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.jsonl"
        with ManifestWriter(manifest) as writer:
            writer.append(_entry("a1"))

        warnings: list[str] = []
        assert list(load_index(manifest, warnings=warnings)) == ["a1"]
        assert warnings == []

    def test_write_csv_tolerates_a_damaged_line(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.jsonl"
        with ManifestWriter(manifest) as writer:
            writer.append(_entry("a1"))
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")

        assert write_csv(manifest, tmp_path / "manifest.csv") == 1
