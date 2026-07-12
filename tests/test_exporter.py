"""Integration: full export runs against the fake Immich API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from immich_export.config import ExportConfig, ExportMode
from immich_export.exporter import locate_original, run_export
from immich_export.manifest import load_index

from .fake_immich import BASE, FakeImmich


async def test_self_contained_export_builds_full_tree(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    report = await run_export(base_config)

    assert report.total == 5
    assert report.exported == 5
    assert report.skipped == 0
    assert report.errors == []

    library = out_dir / "library"
    assert (library / "2019/04/IMG_0001.jpg").read_bytes() == b"jpeg-bytes-one"
    assert (library / "2019/04/IMG_0002.jpg").is_file()
    assert (library / "2023/01/IMG_0003.heic").is_file()
    assert (library / "2024/03/VID_0004.mp4").is_file()
    assert (library / "2024/03/IMG_0005.jpg").is_file()

    sidecar = (library / "2019/04/IMG_0001.jpg.xmp").read_text(encoding="utf-8")
    assert "travel/japan" in sidecar
    assert "Anna" in sidecar
    assert "Japan 2019" in sidecar
    assert "Tokyo tower" in sidecar

    album_link = out_dir / "albums/Japan 2019/IMG_0001.jpg"
    assert album_link.is_symlink()
    assert album_link.resolve() == (library / "2019/04/IMG_0001.jpg").resolve()
    assert (out_dir / "albums/Family/IMG_0003.heic").is_symlink()

    people_link = out_dir / "people/Anna/IMG_0001.jpg"
    assert people_link.is_symlink()
    assert (out_dir / "people/Anna/IMG_0003.heic").is_symlink()

    index = load_index(out_dir / "manifest.jsonl")
    assert len(index) == 5
    entry = index["a1"]
    assert entry.albums == ["Japan 2019"]
    assert entry.people == ["Anna"]
    assert entry.tags == ["travel/japan"]
    assert entry.latitude == pytest.approx(35.6586)

    assert (out_dir / "manifest.csv").is_file()
    report_text = (out_dir / "export-report.txt").read_text(encoding="utf-8")
    assert "exported:      5" in report_text


async def test_rerun_is_incremental(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    downloads_after_first = fake_immich.download_calls

    report = await run_export(base_config)
    assert fake_immich.download_calls == downloads_after_first  # nothing re-downloaded
    assert report.exported == 0
    assert report.skipped == 5
    lines = (out_dir / "manifest.jsonl").read_text().strip().splitlines()
    assert len(lines) == 5  # no duplicate manifest rows


async def test_metadata_change_refreshes_sidecar_without_download(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    downloads = fake_immich.download_calls

    fake_immich.add_album("al-best", "Best Of", ["a5"])
    report = await run_export(base_config)

    assert fake_immich.download_calls == downloads
    assert report.exported == 1  # only a5's metadata row refreshed
    assert load_index(out_dir / "manifest.jsonl")["a5"].albums == ["Best Of"]
    assert "Best Of" in (out_dir / "library/2024/03/IMG_0005.jpg.xmp").read_text()
    assert (out_dir / "albums/Best Of/IMG_0005.jpg").is_symlink()


async def test_pagination_is_followed(fake_immich: FakeImmich, base_config: ExportConfig) -> None:
    fake_immich.page_size_override = 2  # force 3 pages for 5 assets
    report = await run_export(base_config)
    assert report.total == 5
    assert report.exported == 5


async def test_since_filters_assets(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    from datetime import datetime

    cfg = ExportConfig(
        server=base_config.server,
        api_key=base_config.api_key,
        out=out_dir,
        since=datetime(2024, 1, 1),
    )
    report = await run_export(cfg)
    assert report.total == 2  # only the two 2024 assets
    assert not (out_dir / "library/2019").exists()


async def test_single_asset_failure_continues(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    del fake_immich.contents["a2"]  # download will 404
    report = await run_export(base_config)
    assert report.exported == 4
    assert len(report.errors) == 1
    assert "IMG_0002.jpg" in report.errors[0][0]
    assert not (out_dir / "library/2019/04/IMG_0002.jpg").exists()
    assert not (out_dir / "library/2019/04/IMG_0002.jpg.part").exists()


async def test_layout_album_token(fake_immich: FakeImmich, out_dir: Path) -> None:
    cfg = ExportConfig(server=BASE, api_key="k", out=out_dir, layout="{year}/{album}")
    await run_export(cfg)
    assert (out_dir / "library/2019/Japan 2019/IMG_0001.jpg").is_file()
    assert (out_dir / "library/2024/Unsorted/IMG_0005.jpg").is_file()


async def test_filename_collision_is_disambiguated(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    from .fake_immich import make_asset

    clash = make_asset("a9-clash", "IMG_0005.jpg", b"different-bytes", "2024-03-16T09:00:00.000Z")
    fake_immich.add_asset(clash, b"different-bytes")

    report = await run_export(base_config)
    assert report.exported == 6
    assert (out_dir / "library/2024/03/IMG_0005.jpg").is_file()
    assert (out_dir / "library/2024/03/IMG_0005-a9-clash.jpg").is_file()


async def test_sidecar_mode_writes_next_to_existing_tree(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    library_root = tmp_path / "nas-library"
    for asset in fake_immich.assets:
        rel = Path(*Path(asset["originalPath"]).parts[3:])  # strip upload/library/admin
        target = library_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fake_immich.contents[asset["id"]])

    out = tmp_path / "views"
    cfg = ExportConfig(
        server=BASE,
        api_key="k",
        out=out,
        mode=ExportMode.SIDECAR,
        library_root=library_root,
    )
    report = await run_export(cfg)

    assert report.exported == 5
    assert fake_immich.download_calls == 0  # sidecar mode never downloads
    assert (library_root / "2019/04/IMG_0001.jpg.xmp").is_file()
    link = out / "albums/Japan 2019/IMG_0001.jpg"
    assert link.is_symlink()
    assert link.resolve() == (library_root / "2019/04/IMG_0001.jpg").resolve()

    # second run: everything up to date
    report2 = await run_export(cfg)
    assert report2.skipped == 5


async def test_manifest_records_relative_paths_only(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    for line in (out_dir / "manifest.jsonl").read_text().strip().splitlines():
        path = json.loads(line)["path"]
        assert not Path(path).is_absolute()
        assert path.startswith("library/")


def test_locate_original_strips_server_prefixes(tmp_path: Path) -> None:
    target = tmp_path / "2024/03/IMG.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert locate_original(tmp_path, "upload/library/admin/2024/03/IMG.jpg") == target
    assert locate_original(tmp_path, "/upload/library/admin/2024/03/IMG.jpg") == target
    assert locate_original(tmp_path, "upload/library/admin/2024/03/MISSING.jpg") is None
