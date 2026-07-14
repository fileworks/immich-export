"""Orchestrates a full export run: page assets → place files → sidecars → views."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .client import DEFAULT_VISIBILITIES, ImmichClient
from .config import ExportConfig, ExportMode
from .errors import ImmichExportError, OutputError, ServerUnreachableError
from .layout import compute_relative_path, disambiguate
from .manifest import ManifestEntry, ManifestWriter, load_index, write_csv
from .models import Asset
from .progress import Progress
from .report import ExportReport
from .sidecar import write_sidecar
from .views import build_view

logger = logging.getLogger(__name__)

LIBRARY_DIR = "library"
MANIFEST_JSONL = "manifest.jsonl"
MANIFEST_CSV = "manifest.csv"
REPORT_FILE = "export-report.txt"


def locate_original(library_root: Path, original_path: str) -> Path | None:
    """Find an asset's file under a Storage-Template tree from its server-side path.

    Immich reports paths like ``upload/library/admin/2024/03/IMG.jpg``; the local
    mount usually starts somewhere inside that. Try progressively shorter
    suffixes until one exists.
    """
    parts = PurePosixPath(original_path).parts
    if parts and parts[0] == "/":
        parts = parts[1:]
    for start in range(len(parts)):
        candidate = library_root.joinpath(*parts[start:])
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class _AssetMeta:
    """Flattened metadata snapshot used for manifest rows + change detection."""

    checksum: str
    file_name: str
    taken_at: datetime
    type: str
    favorite: bool
    description: str | None
    albums: list[str]
    people: list[str]
    tags: list[str]
    latitude: float | None
    longitude: float | None

    @classmethod
    def build(cls, asset: Asset, albums: list[str], extra_tags: set[str]) -> _AssetMeta:
        exif = asset.exif_info
        return cls(
            checksum=asset.checksum,
            file_name=asset.original_file_name,
            taken_at=asset.taken_at,
            type=str(asset.type),
            favorite=asset.is_favorite,
            description=asset.description,
            albums=sorted(albums),
            people=sorted(p.name for p in asset.people if p.name),
            tags=sorted({t.value for t in asset.tags} | extra_tags),
            latitude=exif.latitude if exif else None,
            longitude=exif.longitude if exif else None,
        )

    def matches(self, entry: ManifestEntry) -> bool:
        return (
            entry.checksum == self.checksum
            and entry.albums == self.albums
            and entry.people == self.people
            and entry.tags == self.tags
            and entry.favorite == self.favorite
            and entry.description == self.description
        )

    def to_entry(self, asset_id: str, path: str) -> ManifestEntry:
        return ManifestEntry(
            asset_id=asset_id,
            checksum=self.checksum,
            path=path,
            file_name=self.file_name,
            taken_at=self.taken_at,
            type=self.type,
            favorite=self.favorite,
            description=self.description,
            albums=self.albums,
            people=self.people,
            tags=self.tags,
            latitude=self.latitude,
            longitude=self.longitude,
            exported_at=datetime.now(UTC),
        )


class _Runner:
    def __init__(
        self,
        cfg: ExportConfig,
        client: ImmichClient,
        report: ExportReport,
        progress: Progress,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.report = report
        self.progress = progress
        self.asset_albums: dict[str, list[str]] = {}
        self.asset_tags: dict[str, set[str]] = {}
        self.media_base = (
            cfg.library_root if cfg.mode is ExportMode.SIDECAR and cfg.library_root else cfg.out
        )
        self.manifest_path = cfg.out / MANIFEST_JSONL
        self.index = load_index(self.manifest_path, warnings=report.warnings) if cfg.resume else {}
        # Every relative path handed out so far (this run + previous runs).
        self.assigned: dict[str, str] = {e.path: e.asset_id for e in self.index.values()}
        self.semaphore = asyncio.Semaphore(cfg.concurrency)

    async def load_memberships(self) -> None:
        """Index album and tag membership up front.

        v3 has no bulk membership endpoint, so this is one search per album and
        per tag. Run them concurrently: sequentially, a library with a few dozen
        albums spends its first minute doing nothing but waiting on round-trips.
        """
        albums, tags = await asyncio.gather(self.client.list_albums(), self.client.list_tags())

        async def index_album(album_id: str, album_name: str) -> None:
            async with self.semaphore:
                asset_ids = await self.client.search_asset_ids(album_id=album_id)
            for asset_id in asset_ids:
                self.asset_albums.setdefault(asset_id, []).append(album_name)

        async def index_tag(tag_id: str, tag_value: str) -> None:
            async with self.semaphore:
                asset_ids = await self.client.search_asset_ids(tag_id=tag_id)
            for asset_id in asset_ids:
                self.asset_tags.setdefault(asset_id, set()).add(tag_value)

        await asyncio.gather(
            *(index_album(a.id, a.album_name) for a in albums),
            *(index_tag(t.id, t.value) for t in tags),
        )
        self.progress.note(f"Indexed {len(albums)} album(s) and {len(tags)} tag(s). Exporting…")

    def meta_for(self, asset: Asset) -> _AssetMeta:
        return _AssetMeta.build(
            asset,
            self.asset_albums.get(asset.id, []),
            self.asset_tags.get(asset.id, set()),
        )

    def assign_path(self, asset_id: str, rel: PurePosixPath) -> str:
        """Hand out a unique relative path; collisions get an id-fragment suffix."""
        full = str(PurePosixPath(LIBRARY_DIR) / rel)
        if self.assigned.get(full, asset_id) != asset_id:
            full = str(disambiguate(PurePosixPath(full), asset_id))
        self.assigned[full] = asset_id
        return full

    def record(self, manifest: ManifestWriter, entry: ManifestEntry) -> None:
        manifest.append(entry)
        self.index[entry.asset_id] = entry
        self.report.exported += 1
        self.progress.exported()

    def skip(self) -> None:
        self.report.skipped += 1
        self.progress.skipped()


async def run_export(cfg: ExportConfig, *, progress: Progress | None = None) -> ExportReport:
    cfg.validate()
    report = ExportReport(server=cfg.server, mode=str(cfg.mode))
    _prepare_output(cfg)
    tracker = progress if progress is not None else Progress(enabled=False)

    with tracker:
        async with ImmichClient(cfg.server, cfg.api_key) as client:
            about = await client.check_connection()
            report.server_version = about.version
            tracker.note(f"Connected to Immich {about.version} at {cfg.server}.")
            runner = _Runner(cfg, client, report, tracker)
            await runner.load_memberships()

            visibilities = list(DEFAULT_VISIBILITIES) + (
                ["hidden", "locked"] if cfg.include_hidden else []
            )
            with ManifestWriter(runner.manifest_path) as manifest:
                async for page in client.iter_assets(
                    taken_after=cfg.since, visibilities=visibilities
                ):
                    await asyncio.gather(*(_export_one(runner, manifest, asset) for asset in page))

            tracker.close()
            _build_views(cfg, runner, report)
            report.finish()
            rows = write_csv(runner.manifest_path, cfg.out / MANIFEST_CSV)
            logger.debug("manifest.csv rewritten with %d rows", rows)
            report.write(cfg.out / REPORT_FILE)
    return report


def _prepare_output(cfg: ExportConfig) -> None:
    try:
        cfg.out.mkdir(parents=True, exist_ok=True)
        if cfg.mode is ExportMode.SELF_CONTAINED:
            (cfg.out / LIBRARY_DIR).mkdir(exist_ok=True)
        probe = cfg.out / ".write-probe"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise OutputError(f"Output directory {cfg.out} is not writable: {exc}") from exc


async def _export_one(runner: _Runner, manifest: ManifestWriter, asset: Asset) -> None:
    runner.report.total += 1
    try:
        async with runner.semaphore:
            await _place_asset(runner, manifest, asset)
    except (OutputError, ServerUnreachableError):
        raise  # disk full / server gone: abort the whole run
    except ImmichExportError as exc:
        runner.report.record_error(f"{asset.original_file_name} ({asset.id})", str(exc))
        runner.progress.failed()
    except Exception as exc:  # best-effort: one bad asset never kills the run
        logger.debug("asset %s failed", asset.id, exc_info=True)
        runner.report.record_error(f"{asset.original_file_name} ({asset.id})", repr(exc))
        runner.progress.failed()


async def _place_asset(runner: _Runner, manifest: ManifestWriter, asset: Asset) -> None:
    cfg, report = runner.cfg, runner.report
    meta = runner.meta_for(asset)
    entry = runner.index.get(asset.id)

    if cfg.mode is ExportMode.SIDECAR:
        assert cfg.library_root is not None
        located = locate_original(cfg.library_root, asset.original_path)
        if located is None:
            raise ImmichExportError(
                f"file not found under {cfg.library_root} (server path: {asset.original_path})"
            )
        rel_path = located.relative_to(cfg.library_root).as_posix()
        sidecar_exists = located.with_name(located.name + ".xmp").is_file()
        if entry is not None and meta.matches(entry) and sidecar_exists:
            runner.skip()
            return
        if cfg.write_sidecars:
            write_sidecar(asset, meta.albums, located)
        runner.record(manifest, meta.to_entry(asset.id, rel_path))
        return

    # self-contained mode
    if entry is not None and entry.checksum == asset.checksum and (cfg.out / entry.path).is_file():
        if meta.matches(entry):
            runner.skip()
            return
        # file unchanged, metadata changed → refresh sidecar + manifest only
        if cfg.write_sidecars:
            write_sidecar(asset, meta.albums, cfg.out / entry.path)
        runner.record(manifest, meta.to_entry(asset.id, entry.path))
        return

    rel = compute_relative_path(asset, cfg.layout, meta.albums[0] if meta.albums else None)
    full_rel = runner.assign_path(asset.id, rel)
    dest = cfg.out / full_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha1_hex = await _download_with_retry(runner, asset.id, dest)
    expected = base64.b64decode(asset.checksum).hex()
    if sha1_hex != expected:
        report.warnings.append(
            f"checksum mismatch for {full_rel} (expected {expected}, got {sha1_hex})"
        )
    if cfg.write_sidecars:
        write_sidecar(asset, meta.albums, dest)
    runner.record(manifest, meta.to_entry(asset.id, full_rel))


async def _download_with_retry(runner: _Runner, asset_id: str, dest: Path) -> str:
    try:
        return await runner.client.download_original(asset_id, dest)
    except ServerUnreachableError:
        await asyncio.sleep(1.0)
        return await runner.client.download_original(asset_id, dest)


def _build_views(cfg: ExportConfig, runner: _Runner, report: ExportReport) -> None:
    """Rebuild album/people views from the full manifest (complete across runs)."""
    if not (cfg.album_view or cfg.people_view):
        return
    album_groups: dict[str, list[Path]] = {}
    people_groups: dict[str, list[Path]] = {}
    for entry in runner.index.values():
        media = runner.media_base / entry.path
        for album in entry.albums:
            album_groups.setdefault(album, []).append(media)
        for person in entry.people:
            people_groups.setdefault(person, []).append(media)
    if cfg.album_view:
        report.album_links = build_view(cfg.out / "albums", album_groups, warnings=report.warnings)
    if cfg.people_view:
        report.people_links = build_view(
            cfg.out / "people", people_groups, warnings=report.warnings
        )
