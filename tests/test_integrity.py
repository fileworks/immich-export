"""Integrity, canonical-state, reconciliation, and publication contracts."""

from __future__ import annotations

import csv
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immich_export import sidecar as sidecar_module
from immich_export.cli import app
from immich_export.client import ImmichClient
from immich_export.config import (
    ExportConfig,
    ExportMode,
    StaleAssetPolicy,
)
from immich_export.errors import OutputError, ServerUnreachableError
from immich_export.exporter import run_export
from immich_export.manifest import load_current

from .fake_immich import BASE, FakeImmich, checksum_of


def _library_root(tmp_path: Path, fake: FakeImmich) -> Path:
    root = tmp_path / "immich-library"
    for asset in fake.assets:
        relative = Path(*Path(asset["originalPath"]).parts[3:])
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fake.contents[asset["id"]])
    return root


def _sidecar_config(tmp_path: Path, fake: FakeImmich) -> ExportConfig:
    return ExportConfig(
        server=BASE,
        api_key="k",
        out=tmp_path / "export",
        mode=ExportMode.SIDECAR,
        library_root=_library_root(tmp_path, fake),
    )


async def test_self_contained_canonical_metadata_change_moves_without_download(
    fake_immich: FakeImmich, out_dir: Path
) -> None:
    cfg = ExportConfig(
        server=BASE,
        api_key="k",
        out=out_dir,
        layout="{year}/{album}/{type}",
    )
    await run_export(cfg)
    downloads = fake_immich.download_calls
    old_path = out_dir / load_current(out_dir / "manifest-current.jsonl")["a1"].path

    fake_immich.change_asset(
        "a1",
        originalFileName="RENAMED.mov",
        type="VIDEO",
        isFavorite=True,
        people=[{"id": "p-bob", "name": "Bob"}],
        localDateTime="2025-08-09T12:00:00.000Z",
        fileCreatedAt="2025-08-09T12:00:00.000Z",
        tags=[{"id": "t-new", "name": "new", "value": "new/tag"}],
    )
    fake_immich.change_exif(
        "a1",
        dateTimeOriginal="2025-08-09T12:00:00.000Z",
        description="changed",
        latitude=1.25,
        longitude=-2.5,
    )
    fake_immich.add_album("al-new", "New Album", ["a1"])
    fake_immich.set_album_members("al-japan", [])
    fake_immich.add_tag("t-new", "new", "indexed/only", ["a1"])
    fake_immich.set_tag_members("t-japan", [])

    report = await run_export(cfg)
    current = load_current(out_dir / "manifest-current.jsonl")["a1"]
    new_path = out_dir / current.path

    assert fake_immich.download_calls == downloads
    assert report.errors == []
    assert not old_path.exists()
    assert new_path.read_bytes() == b"jpeg-bytes-one"
    assert current.path == "library/2025/New Album/videos/RENAMED.mov"
    assert current.file_name == "RENAMED.mov"
    assert current.type == "VIDEO"
    assert current.favorite is True
    assert current.description == "changed"
    assert current.people == ["Bob"]
    assert current.tags == ["indexed/only", "new/tag"]
    assert current.latitude == pytest.approx(1.25)
    assert current.longitude == pytest.approx(-2.5)
    xmp = new_path.with_name(new_path.name + ".xmp").read_text()
    assert "indexed/only" in xmp
    assert "travel/japan" not in xmp
    assert "Anna" not in xmp


async def test_metadata_removal_clears_state_sidecar_and_album_path(
    fake_immich: FakeImmich, out_dir: Path
) -> None:
    cfg = ExportConfig(server=BASE, api_key="k", out=out_dir, layout="{year}/{album}")
    await run_export(cfg)
    downloads = fake_immich.download_calls
    fake_immich.change_asset("a1", isFavorite=False, people=[], tags=[])
    fake_immich.change_exif("a1", description=None, latitude=None, longitude=None)
    fake_immich.set_album_members("al-japan", [])
    fake_immich.set_tag_members("t-japan", [])

    await run_export(cfg)
    current = load_current(out_dir / "manifest-current.jsonl")["a1"]
    media = out_dir / current.path
    xmp = media.with_name(media.name + ".xmp").read_text()

    assert fake_immich.download_calls == downloads
    assert current.path == "library/2019/Unsorted/IMG_0001.jpg"
    assert current.albums == []
    assert current.people == []
    assert current.tags == []
    assert current.description is None
    assert current.latitude is None
    assert current.longitude is None
    assert "Japan 2019" not in xmp
    assert "travel/japan" not in xmp
    assert "Anna" not in xmp
    assert "Tokyo tower" not in xmp
    assert "GPSLatitude" not in xmp


async def test_sidecar_mode_tracks_relocated_original_and_metadata_removal(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    cfg = _sidecar_config(tmp_path, fake_immich)
    assert cfg.library_root is not None
    await run_export(cfg)
    old_original = cfg.library_root / "2019/04/IMG_0001.jpg"
    old_sidecar = old_original.with_name(old_original.name + ".xmp")
    new_original = cfg.library_root / "relocated/RENAMED.jpg"
    new_original.parent.mkdir(parents=True)
    old_original.replace(new_original)
    fake_immich.relocate_asset("a1", "upload/library/admin/relocated/RENAMED.jpg")
    fake_immich.change_asset(
        "a1",
        originalFileName="RENAMED.jpg",
        isFavorite=False,
        people=[],
        tags=[],
    )
    fake_immich.change_exif("a1", description=None, latitude=None, longitude=None)
    fake_immich.set_album_members("al-japan", [])
    fake_immich.set_tag_members("t-japan", [])

    report = await run_export(cfg)
    state = load_current(cfg.out / "manifest-current.jsonl")["a1"]
    new_sidecar = new_original.with_name(new_original.name + ".xmp")

    assert report.errors == []
    assert fake_immich.download_calls == 0
    assert new_original.read_bytes() == b"jpeg-bytes-one"
    assert state.path == "relocated/RENAMED.jpg"
    assert state.original_path.endswith("relocated/RENAMED.jpg")
    assert state.albums == state.people == state.tags == []
    assert old_sidecar.is_file()  # keep-by-default never mutates stale sidecars
    assert "Tokyo tower" not in new_sidecar.read_text()


async def test_indexed_and_embedded_tags_share_one_canonical_projection(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    fake_immich.change_asset(
        "a2",
        tags=[{"id": "embedded", "name": "embedded", "value": "tag/embedded"}],
    )
    fake_immich.add_tag("indexed", "indexed", "tag/indexed", ["a2"])
    await run_export(base_config)

    state = load_current(out_dir / "manifest-current.jsonl")["a2"]
    xmp = (out_dir / state.path).with_name(Path(state.path).name + ".xmp").read_text()
    assert state.tags == ["tag/embedded", "tag/indexed"]
    assert xmp.count("tag/embedded") == 1
    assert xmp.count("tag/indexed") == 1

    fake_immich.change_asset("a2", tags=[])
    fake_immich.set_tag_members("indexed", [])
    await run_export(base_config)
    state = load_current(out_dir / "manifest-current.jsonl")["a2"]
    xmp = (out_dir / state.path).with_name(Path(state.path).name + ".xmp").read_text()
    assert state.tags == []
    assert "tag/embedded" not in xmp
    assert "tag/indexed" not in xmp


async def test_download_checksum_mismatch_preserves_good_final_and_excludes_current(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    before = load_current(out_dir / "manifest-current.jsonl")["a1"]
    final = out_dir / before.path
    history_before = sum(
        json.loads(line)["asset_id"] == "a1"
        for line in (out_dir / "manifest.jsonl").read_text().splitlines()
    )
    fake_immich.change_asset("a1", checksum=checksum_of(b"expected-new-content"))
    fake_immich.set_content("a1", b"wrong-download", update_checksum=False)

    report = await run_export(base_config)

    assert len(report.errors) == 1
    assert "SHA-1" in report.errors[0][1]
    assert final.read_bytes() == b"jpeg-bytes-one"
    assert "a1" not in load_current(out_dir / "manifest-current.jsonl")
    assert (
        sum(
            json.loads(line)["asset_id"] == "a1"
            for line in (out_dir / "manifest.jsonl").read_text().splitlines()
        )
        == history_before
    )
    assert not list(final.parent.glob(f".{final.name}.download-*.tmp*"))


async def test_resume_rehashes_and_repairs_corrupted_self_contained_media(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    state = load_current(out_dir / "manifest-current.jsonl")["a1"]
    media = out_dir / state.path
    downloads = fake_immich.download_calls
    media.write_bytes(b"locally-corrupted")

    report = await run_export(base_config)

    assert report.errors == []
    assert fake_immich.download_calls == downloads + 1
    assert media.read_bytes() == b"jpeg-bytes-one"
    assert load_current(out_dir / "manifest-current.jsonl")["a1"].checksum == state.checksum


async def test_sidecar_mode_rehashes_but_never_repairs_original(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    cfg = _sidecar_config(tmp_path, fake_immich)
    assert cfg.library_root is not None
    await run_export(cfg)
    original = cfg.library_root / "2019/04/IMG_0001.jpg"
    original.write_bytes(b"locally-corrupted")
    sidecar_before = original.with_name(original.name + ".xmp").read_bytes()

    report = await run_export(cfg)

    assert any("failed SHA-1" in message for _, message in report.errors)
    assert original.read_bytes() == b"locally-corrupted"
    assert original.with_name(original.name + ".xmp").read_bytes() == sidecar_before
    assert "a1" not in load_current(cfg.out / "manifest-current.jsonl")
    assert fake_immich.download_calls == 0


@pytest.mark.parametrize(
    "damage",
    [
        lambda path: path.unlink(),
        lambda path: path.write_text("<truncated", encoding="utf-8"),
        lambda path: path.write_text("<xmp>stale</xmp>", encoding="utf-8"),
    ],
    ids=["missing", "malformed", "stale"],
)
async def test_required_sidecar_damage_is_atomically_repaired(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    damage: Callable[[Path], None],
) -> None:
    await run_export(base_config)
    state = load_current(out_dir / "manifest-current.jsonl")["a1"]
    sidecar = (out_dir / state.path).with_name(Path(state.path).name + ".xmp")
    damage(sidecar)

    report = await run_export(base_config)

    assert report.errors == []
    assert report.exported == 1
    repaired = sidecar.read_text()
    assert "Tokyo tower" in repaired
    assert repaired.endswith('<?xpacket end="w"?>\n')
    assert not list(sidecar.parent.glob(f".{sidecar.name}.*.tmp"))


async def test_interrupted_sidecar_replacement_preserves_sidecar_and_current(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run_export(base_config)
    current_path = out_dir / "manifest-current.jsonl"
    current_before = current_path.read_bytes()
    state = load_current(current_path)["a1"]
    sidecar = (out_dir / state.path).with_name(Path(state.path).name + ".xmp")
    sidecar_before = sidecar.read_bytes()
    fake_immich.change_exif("a1", description="new description")

    def fail_atomic(path: Path, content: str, *, operation: str) -> None:
        del path, content, operation
        raise OutputError("injected sidecar interruption")

    monkeypatch.setattr(sidecar_module, "atomic_write_text", fail_atomic)
    with pytest.raises(OutputError, match="injected"):
        await run_export(base_config)

    assert sidecar.read_bytes() == sidecar_before
    assert current_path.read_bytes() == current_before


async def test_history_is_append_only_while_current_and_csv_are_deduplicated(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    fake_immich.change_exif("a1", description="revision two")
    await run_export(base_config)

    history = [json.loads(line) for line in (out_dir / "manifest.jsonl").read_text().splitlines()]
    current = load_current(out_dir / "manifest-current.jsonl")
    with (out_dir / "manifest.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert len([row for row in history if row["asset_id"] == "a1"]) == 2
    assert len(current) == 5
    assert len(rows) == 5
    assert next(row for row in rows if row["asset_id"] == "a1")["description"] == "revision two"


async def test_legacy_history_is_preserved_but_corrupt_media_is_not_trusted(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    media = out_dir / "library/2019/04/IMG_0001.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"bad-legacy-copy")
    legacy = {
        "asset_id": "a1",
        "checksum": checksum_of(b"jpeg-bytes-one"),
        "path": "library/2019/04/IMG_0001.jpg",
        "file_name": "IMG_0001.jpg",
        "taken_at": "2019-04-12T10:00:00Z",
        "type": "IMAGE",
        "albums": ["Japan 2019"],
        "people": ["Anna"],
        "tags": ["travel/japan"],
        "exported_at": "2026-07-13T00:00:00Z",
    }
    history_path = out_dir / "manifest.jsonl"
    original_history = json.dumps(legacy) + "\n" + '{"asset_id":"truncated"'
    history_path.write_text(original_history)

    report = await run_export(base_config)

    assert media.read_bytes() == b"jpeg-bytes-one"
    assert "a1" in load_current(out_dir / "manifest-current.jsonl")
    assert (out_dir / "manifest.jsonl").read_text().startswith(original_history)
    assert any("unreadable" in warning for warning in report.warnings)


async def test_authoritative_absence_updates_current_csv_and_views_but_keeps_media(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    state = load_current(out_dir / "manifest-current.jsonl")["a1"]
    media = out_dir / state.path
    fake_immich.remove_asset("a1")

    report = await run_export(base_config)

    assert report.absent == 1
    assert "a1" not in load_current(out_dir / "manifest-current.jsonl")
    assert media.is_file()
    assert not (out_dir / "albums/Japan 2019/IMG_0001.jpg").exists()
    assert not (out_dir / "people/Anna/IMG_0001.jpg").exists()
    assert "a1" not in (out_dir / "manifest.csv").read_text()
    assert any("orphans" in warning for warning in report.warnings)


async def test_incremental_and_incompatible_scope_do_not_infer_absence(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    await run_export(base_config)
    fake_immich.remove_asset("a1")
    incremental = ExportConfig(
        server=BASE,
        api_key="k",
        out=out_dir,
        since=datetime(2024, 1, 1),
    )
    report = await run_export(incremental)
    assert "a1" in load_current(out_dir / "manifest-current.jsonl")
    assert report.absent == 0

    incompatible = ExportConfig(
        server=BASE,
        api_key="k",
        out=out_dir,
        include_hidden=True,
    )
    report = await run_export(incompatible)
    assert "a1" in load_current(out_dir / "manifest-current.jsonl")
    assert report.absent == 0
    assert any("scope differs" in warning for warning in report.warnings)


async def test_enumeration_failure_does_not_replace_prior_current(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run_export(base_config)
    current_path = out_dir / "manifest-current.jsonl"
    before = current_path.read_bytes()
    original = ImmichClient.iter_assets

    async def interrupted(self: ImmichClient, **kwargs: object) -> AsyncIterator[list[object]]:
        async for page in original(self, **kwargs):  # type: ignore[arg-type]
            yield page  # type: ignore[misc]
            raise ServerUnreachableError("enumeration interrupted")

    monkeypatch.setattr(ImmichClient, "iter_assets", interrupted)
    with pytest.raises(ServerUnreachableError, match="interrupted"):
        await run_export(base_config)
    assert current_path.read_bytes() == before


async def test_explicit_quarantine_moves_only_manifest_owned_outputs(
    fake_immich: FakeImmich, out_dir: Path
) -> None:
    initial = ExportConfig(server=BASE, api_key="k", out=out_dir)
    await run_export(initial)
    state = load_current(out_dir / "manifest-current.jsonl")["a1"]
    media = out_dir / state.path
    sidecar = media.with_name(media.name + ".xmp")
    fake_immich.remove_asset("a1")
    quarantine = ExportConfig(
        server=BASE,
        api_key="k",
        out=out_dir,
        stale_assets=StaleAssetPolicy.QUARANTINE,
    )

    report = await run_export(quarantine)

    assert report.quarantined == 1
    assert not media.exists()
    assert not sidecar.exists()
    quarantined = list((out_dir / ".immich-export-quarantine/a1").rglob("*"))
    assert any(path.name == media.name for path in quarantined)
    assert any(path.name == sidecar.name for path in quarantined)


async def test_sidecar_quarantine_never_moves_immich_original(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    initial = _sidecar_config(tmp_path, fake_immich)
    await run_export(initial)
    assert initial.library_root is not None
    original = initial.library_root / "2019/04/IMG_0001.jpg"
    sidecar = original.with_name(original.name + ".xmp")
    fake_immich.remove_asset("a1")
    quarantine = ExportConfig(
        server=BASE,
        api_key="k",
        out=initial.out,
        mode=ExportMode.SIDECAR,
        library_root=initial.library_root,
        stale_assets=StaleAssetPolicy.QUARANTINE,
    )

    report = await run_export(quarantine)

    assert report.quarantined == 1
    assert original.read_bytes() == b"jpeg-bytes-one"
    assert not sidecar.exists()


async def test_unowned_destination_conflict_is_preserved_and_asset_fails(
    fake_immich: FakeImmich, base_config: ExportConfig, out_dir: Path
) -> None:
    conflict = out_dir / "library/2019/04/IMG_0001.jpg"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"user-owned")

    report = await run_export(base_config)

    assert conflict.read_bytes() == b"user-owned"
    assert any("unowned destination" in message for _, message in report.errors)
    assert "a1" not in load_current(out_dir / "manifest-current.jsonl")


async def test_unexpected_regular_file_blocks_atomic_view_replacement(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
) -> None:
    await run_export(base_config)
    current_path = out_dir / "manifest-current.jsonl"
    before = current_path.read_bytes()
    blocker = out_dir / "albums/user-note.txt"
    blocker.write_text("keep me")

    with pytest.raises(OutputError, match="unexpected regular file"):
        await run_export(base_config)

    assert blocker.read_text() == "keep me"
    assert current_path.read_bytes() == before


def test_cli_returns_partial_exit_five_and_publishes_successes(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    del fake_immich.contents["a2"]
    result = CliRunner().invoke(
        app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path / "out")]
    )

    assert result.exit_code == 5
    assert "1 errors" in result.output
    assert len(load_current(tmp_path / "out/manifest-current.jsonl")) == 4
    assert "outcome:       partial" in (tmp_path / "out/export-report.txt").read_text()


def test_cli_returns_partial_when_every_asset_fails(
    fake_immich: FakeImmich, tmp_path: Path
) -> None:
    fake_immich.contents.clear()
    result = CliRunner().invoke(
        app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path / "out")]
    )

    assert result.exit_code == 5
    assert load_current(tmp_path / "out/manifest-current.jsonl") == {}
