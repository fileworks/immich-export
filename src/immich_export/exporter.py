"""Verify Immich assets and atomically publish current export state."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .client import DEFAULT_VISIBILITIES, ImmichClient
from .config import ExportConfig, ExportMode, StaleAssetPolicy
from .errors import (
    AssetIntegrityError,
    AuthError,
    ChecksumError,
    OutputError,
    ServerUnreachableError,
)
from .layout import compute_relative_path, disambiguate
from .manifest import (
    AssetState,
    ManifestWriter,
    load_current,
    load_index,
    write_current,
    write_current_csv,
)
from .models import Asset
from .progress import Progress
from .report import ExportReport
from .sidecar import sidecar_matches, write_sidecar
from .views import build_view

LIBRARY_DIR = "library"
MANIFEST_JSONL = "manifest.jsonl"
CURRENT_MANIFEST_JSONL = "manifest-current.jsonl"
MANIFEST_CSV = "manifest.csv"
REPORT_FILE = "export-report.txt"
QUARANTINE_DIR = ".immich-export-quarantine"
REPLACED_DIR = ".immich-export-replaced"


def locate_original(library_root: Path, original_path: str) -> Path | None:
    """Locate a server Storage-Template path beneath a configured local root."""
    parts = PurePosixPath(original_path).parts
    if parts and parts[0] == "/":
        parts = parts[1:]
    for start in range(len(parts)):
        candidate = library_root.joinpath(*parts[start:])
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class _AssetMetadata:
    checksum: str
    file_name: str
    original_path: str
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
    def build(cls, asset: Asset, albums: list[str], indexed_tags: set[str]) -> _AssetMetadata:
        exif = asset.exif_info
        return cls(
            checksum=asset.checksum,
            file_name=asset.original_file_name,
            original_path=asset.original_path,
            taken_at=asset.taken_at,
            type=str(asset.type),
            favorite=asset.is_favorite,
            description=asset.description,
            albums=sorted(set(albums)),
            people=sorted({person.name for person in asset.people if person.name}),
            tags=sorted({tag.value for tag in asset.tags} | indexed_tags),
            latitude=exif.latitude if exif else None,
            longitude=exif.longitude if exif else None,
        )


class _Runner:
    def __init__(
        self,
        cfg: ExportConfig,
        client: ImmichClient,
        report: ExportReport,
        progress: Progress,
        visibilities: list[str],
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.report = report
        self.progress = progress
        self.visibilities = visibilities
        self.asset_albums: dict[str, list[str]] = {}
        self.asset_tags: dict[str, set[str]] = {}
        self.manifest_path = cfg.out / MANIFEST_JSONL
        self.current_path = cfg.out / CURRENT_MANIFEST_JSONL
        self.history = load_index(self.manifest_path, warnings=report.warnings)
        self.had_current = self.current_path.is_file()
        self.current = load_current(self.current_path)
        # History is only a migration hint. Every legacy candidate is verified
        # live before it enters the first authoritative current snapshot.
        self.lookup = dict(self.current or (self.history if cfg.resume else {}))
        self.authoritative = cfg.since is None and all(
            self._scope_matches(entry) for entry in self.current.values()
        )
        self.candidate = {} if self.authoritative else dict(self.current)
        if cfg.since is not None:
            report.warnings.append(
                "Incremental --since scan: merged verified assets without inferring absence."
            )
        elif self.current and not self.authoritative:
            report.warnings.append(
                "Current snapshot scope differs from this scan; absence reconciliation "
                "was suppressed."
            )
        self.assigned: dict[str, str] = {
            entry.path: entry.asset_id
            for entry in self.lookup.values()
            if entry.mode == ExportMode.SELF_CONTAINED
        }
        self.seen: set[str] = set()
        self.semaphore = asyncio.Semaphore(cfg.concurrency)

    def _scope_matches(self, entry: AssetState) -> bool:
        return (
            entry.mode == str(self.cfg.mode)
            and entry.layout == self.cfg.layout
            and entry.write_sidecars == self.cfg.write_sidecars
            and entry.include_hidden == self.cfg.include_hidden
            and entry.visibilities == self.visibilities
            and entry.library_root
            == (str(self.cfg.library_root) if self.cfg.library_root is not None else None)
        )

    async def load_memberships(self) -> None:
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
            *(index_album(album.id, album.album_name) for album in albums),
            *(index_tag(tag.id, tag.value) for tag in tags),
        )
        self.progress.note(f"Indexed {len(albums)} album(s) and {len(tags)} tag(s). Exporting…")

    def metadata_for(self, asset: Asset) -> _AssetMetadata:
        return _AssetMetadata.build(
            asset,
            self.asset_albums.get(asset.id, []),
            self.asset_tags.get(asset.id, set()),
        )

    def state_for(self, asset_id: str, metadata: _AssetMetadata, path: str) -> AssetState:
        return AssetState(
            asset_id=asset_id,
            checksum=metadata.checksum,
            path=path,
            file_name=metadata.file_name,
            original_path=metadata.original_path,
            taken_at=metadata.taken_at,
            type=metadata.type,
            favorite=metadata.favorite,
            description=metadata.description,
            albums=metadata.albums,
            people=metadata.people,
            tags=metadata.tags,
            latitude=metadata.latitude,
            longitude=metadata.longitude,
            mode=str(self.cfg.mode),
            layout=self.cfg.layout,
            write_sidecars=self.cfg.write_sidecars,
            include_hidden=self.cfg.include_hidden,
            visibilities=self.visibilities,
            library_root=(
                str(self.cfg.library_root) if self.cfg.library_root is not None else None
            ),
            verified_at=datetime.now(UTC),
        )

    def assign_path(self, asset_id: str, relative: PurePosixPath) -> str:
        base = PurePosixPath(LIBRARY_DIR) / relative
        candidate = base
        sequence = 1
        while True:
            rendered = str(candidate)
            owner = self.assigned.get(rendered)
            disk_path = self.cfg.out / rendered
            sidecar_path = disk_path.with_name(disk_path.name + ".xmp")
            unowned_disk_conflict = owner is None and (
                disk_path.exists()
                or disk_path.is_symlink()
                or sidecar_path.exists()
                or sidecar_path.is_symlink()
            )
            if unowned_disk_conflict:
                raise AssetIntegrityError(f"Refusing to overwrite unowned destination {disk_path}.")
            if owner in (None, asset_id) and not unowned_disk_conflict:
                self.assigned[rendered] = asset_id
                return rendered
            if sequence == 1:
                candidate = disambiguate(base, asset_id)
            else:
                candidate = base.with_name(f"{base.stem}-{asset_id[:8]}-{sequence}{base.suffix}")
            sequence += 1

    def mark_verified(
        self, manifest: ManifestWriter, state: AssetState, *, outputs_changed: bool
    ) -> None:
        previous = self.lookup.get(state.asset_id)
        changed = outputs_changed or previous is None or not state.equivalent(previous)
        if changed:
            manifest.append(state)
            self.report.exported += 1
            self.progress.exported()
        else:
            self.report.skipped += 1
            self.progress.skipped()
        self.lookup[state.asset_id] = state
        self.candidate[state.asset_id] = state

    def mark_failed(self, asset_id: str) -> None:
        self.candidate.pop(asset_id, None)

    def media_path(self, state: AssetState) -> Path:
        if state.mode == ExportMode.SIDECAR:
            if state.library_root is None:
                raise OutputError(
                    f"Current sidecar state for {state.asset_id} has no library root."
                )
            return Path(state.library_root) / state.path
        return self.cfg.out / state.path


async def run_export(cfg: ExportConfig, *, progress: Progress | None = None) -> ExportReport:
    cfg.validate()
    report = ExportReport(server=cfg.server, mode=str(cfg.mode))
    _prepare_output(cfg)
    tracker = progress if progress is not None else Progress(enabled=False)
    visibilities = list(DEFAULT_VISIBILITIES) + (["hidden", "locked"] if cfg.include_hidden else [])

    with tracker:
        async with ImmichClient(cfg.server, cfg.api_key) as client:
            about = await client.check_connection()
            report.server_version = about.version
            tracker.note(f"Connected to Immich {about.version} at {cfg.server}.")
            runner = _Runner(cfg, client, report, tracker, visibilities)
            await runner.load_memberships()

            with ManifestWriter(runner.manifest_path) as manifest:
                async for page in client.iter_assets(
                    taken_after=cfg.since, visibilities=visibilities
                ):
                    await asyncio.gather(*(_verify_one(runner, manifest, asset) for asset in page))

            if runner.authoritative:
                _reconcile_absent(runner)
            tracker.close()
            _build_views(cfg, runner, report)
            write_current_csv(runner.candidate, cfg.out / MANIFEST_CSV)
            report.finish()
            report.write(cfg.out / REPORT_FILE)
            # Publish authority last. Any earlier run-level/output failure leaves
            # the prior complete current snapshot untouched.
            write_current(runner.current_path, runner.candidate)
    return report


def _prepare_output(cfg: ExportConfig) -> None:
    probe: Path | None = None
    try:
        cfg.out.mkdir(parents=True, exist_ok=True)
        if cfg.mode is ExportMode.SELF_CONTAINED:
            (cfg.out / LIBRARY_DIR).mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".immich-export-probe-", dir=cfg.out)
        probe = Path(name)
        try:
            import os

            os.close(fd)
        finally:
            probe.unlink()
            probe = None
    except OSError as exc:
        raise OutputError(f"Output directory {cfg.out} is not writable: {exc}") from exc
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError as exc:
                raise OutputError(f"Cannot clean output probe {probe}: {exc}") from exc


async def _verify_one(runner: _Runner, manifest: ManifestWriter, asset: Asset) -> None:
    runner.report.total += 1
    runner.seen.add(asset.id)
    try:
        async with runner.semaphore:
            await _place_asset(runner, manifest, asset)
    except (AuthError, OutputError, ServerUnreachableError):
        raise
    except AssetIntegrityError as exc:
        runner.mark_failed(asset.id)
        runner.report.record_error(f"{asset.original_file_name} ({asset.id})", str(exc))
        runner.progress.failed()


async def _place_asset(runner: _Runner, manifest: ManifestWriter, asset: Asset) -> None:
    metadata = runner.metadata_for(asset)
    expected = _expected_sha1(metadata.checksum, asset.id)
    if runner.cfg.mode is ExportMode.SIDECAR:
        await _place_sidecar_asset(runner, manifest, asset, metadata, expected)
    else:
        await _place_self_contained_asset(runner, manifest, asset, metadata, expected)


async def _place_sidecar_asset(
    runner: _Runner,
    manifest: ManifestWriter,
    asset: Asset,
    metadata: _AssetMetadata,
    expected: str,
) -> None:
    cfg = runner.cfg
    assert cfg.library_root is not None
    located = locate_original(cfg.library_root, asset.original_path)
    if located is None:
        raise AssetIntegrityError(
            f"Original not found under {cfg.library_root} (server path: {asset.original_path})."
        )
    actual = await asyncio.to_thread(_sha1_file, located)
    if actual != expected:
        raise ChecksumError(
            f"Local original {located} failed SHA-1 verification "
            f"(expected {expected}, got {actual})."
        )
    try:
        relative = located.relative_to(cfg.library_root).as_posix()
    except ValueError as exc:
        raise AssetIntegrityError(
            f"Located original {located} escaped {cfg.library_root}."
        ) from exc
    state = runner.state_for(asset.id, metadata, relative)
    outputs_changed = False
    if cfg.write_sidecars and not sidecar_matches(state, located):
        write_sidecar(state, located)
        outputs_changed = True
    previous = runner.lookup.get(asset.id)
    if previous is not None and previous.mode == ExportMode.SIDECAR and previous.path != state.path:
        old_media = Path(previous.library_root or cfg.library_root) / previous.path
        old_sidecar = old_media.with_name(old_media.name + ".xmp")
        if old_sidecar.exists() or old_sidecar.is_symlink():
            if cfg.stale_assets is StaleAssetPolicy.QUARANTINE:
                if _quarantine_owned_outputs(runner, previous):
                    runner.report.quarantined += 1
            else:
                runner.report.warnings.append(
                    f"Preserved prior sidecar {old_sidecar} after Immich relocated "
                    f"asset {asset.id}."
                )
    runner.mark_verified(manifest, state, outputs_changed=outputs_changed)


async def _place_self_contained_asset(
    runner: _Runner,
    manifest: ManifestWriter,
    asset: Asset,
    metadata: _AssetMetadata,
    expected: str,
) -> None:
    cfg = runner.cfg
    previous = runner.lookup.get(asset.id)
    relative = compute_relative_path(
        asset, cfg.layout, metadata.albums[0] if metadata.albums else None
    )
    desired_relative = runner.assign_path(asset.id, relative)
    desired = cfg.out / desired_relative
    state = runner.state_for(asset.id, metadata, desired_relative)

    source: Path | None = None
    if previous is not None and previous.mode == ExportMode.SELF_CONTAINED:
        previous_path = cfg.out / previous.path
        if previous_path.is_file() and not previous_path.is_symlink():
            source = previous_path
    if source is None and desired.is_file() and not desired.is_symlink():
        source = desired
    if source is None and (desired.exists() or desired.is_symlink()):
        raise AssetIntegrityError(f"Refusing to replace non-regular destination {desired}.")

    outputs_changed = False
    if source is not None:
        actual = await asyncio.to_thread(_sha1_file, source)
        if actual == expected:
            if source != desired:
                _relocate_verified_media(
                    state,
                    source,
                    desired,
                    library_root=cfg.out / LIBRARY_DIR,
                )
                outputs_changed = True
            elif cfg.write_sidecars and not sidecar_matches(state, desired):
                write_sidecar(state, desired)
                outputs_changed = True
            runner.mark_verified(manifest, state, outputs_changed=outputs_changed)
            return
        runner.report.warnings.append(
            f"{source} no longer matches Immich; attempting a verified replacement."
        )

    await _download_verified(runner, state, desired, expected)
    runner.mark_verified(manifest, state, outputs_changed=True)


def _expected_sha1(encoded: str, asset_id: str) -> str:
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChecksumError(f"Immich returned an invalid checksum for asset {asset_id}.") from exc
    if len(digest) != hashlib.sha1().digest_size:
        raise ChecksumError(f"Immich returned a non-SHA-1 checksum for asset {asset_id}.")
    return digest.hex()


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AssetIntegrityError(f"Cannot verify local original {path}: {exc}") from exc
    return digest.hexdigest()


async def _download_verified(
    runner: _Runner, state: AssetState, destination: Path, expected: str
) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(
            f"Cannot create destination directory {destination.parent}: {exc}"
        ) from exc
    temporary = destination.with_name(f".{destination.name}.download-{uuid4().hex}.tmp")
    temporary_sidecar = temporary.with_name(temporary.name + ".xmp")
    try:
        actual = await _download_with_retry(runner, state.asset_id, temporary)
        if actual != expected:
            raise ChecksumError(
                f"Downloaded bytes failed SHA-1 verification (expected {expected}, got {actual})."
            )
        if runner.cfg.write_sidecars:
            write_sidecar(state, temporary)
        _promote_download(runner, state, temporary, destination)
    finally:
        _clean_temporary(temporary)
        _clean_temporary(temporary_sidecar)


async def _download_with_retry(runner: _Runner, asset_id: str, temporary: Path) -> str:
    try:
        return await runner.client.download_original(asset_id, temporary)
    except ServerUnreachableError:
        await asyncio.sleep(1.0)
        return await runner.client.download_original(asset_id, temporary)


def _promote_download(
    runner: _Runner, state: AssetState, temporary: Path, destination: Path
) -> None:
    final_sidecar = destination.with_name(destination.name + ".xmp")
    temporary_sidecar = temporary.with_name(temporary.name + ".xmp")
    backup_media: Path | None = None
    backup_sidecar: Path | None = None
    media_promoted = False
    sidecar_promoted = False
    try:
        if destination.exists() or destination.is_symlink():
            backup_media = _replacement_path(runner, state, destination.name)
            backup_media.parent.mkdir(parents=True, exist_ok=True)
            destination.replace(backup_media)
        if runner.cfg.write_sidecars and (final_sidecar.exists() or final_sidecar.is_symlink()):
            backup_sidecar = _replacement_path(runner, state, final_sidecar.name)
            backup_sidecar.parent.mkdir(parents=True, exist_ok=True)
            final_sidecar.replace(backup_sidecar)
        temporary.replace(destination)
        media_promoted = True
        if runner.cfg.write_sidecars:
            temporary_sidecar.replace(final_sidecar)
            sidecar_promoted = True
        if backup_media is not None or backup_sidecar is not None:
            runner.report.warnings.append(
                f"Preserved replaced output for {state.asset_id} under {REPLACED_DIR}/."
            )
    except OSError as exc:
        rollback_errors = [
            error
            for error in (
                _restore_replacement(destination, backup_media, promoted=media_promoted),
                _restore_replacement(final_sidecar, backup_sidecar, promoted=sidecar_promoted),
            )
            if error is not None
        ]
        rollback = (
            f" Rollback also failed: {'; '.join(rollback_errors)}." if rollback_errors else ""
        )
        raise OutputError(
            f"Cannot promote verified download to {destination}: {exc}.{rollback}"
        ) from exc


def _replacement_path(runner: _Runner, state: AssetState, name: str) -> Path:
    return runner.cfg.out / REPLACED_DIR / state.asset_id / uuid4().hex / name


def _restore_replacement(final: Path, backup: Path | None, *, promoted: bool) -> str | None:
    try:
        if (promoted or backup is not None) and (final.exists() or final.is_symlink()):
            final.unlink()
        if backup is not None and (backup.exists() or backup.is_symlink()):
            backup.replace(final)
    except OSError as exc:
        return f"cannot restore {final} from {backup}: {exc}"
    return None


def _relocate_verified_media(
    state: AssetState,
    source: Path,
    destination: Path,
    *,
    library_root: Path,
) -> None:
    old_sidecar = source.with_name(source.name + ".xmp")
    new_sidecar = destination.with_name(destination.name + ".xmp")
    sidecar_was_moved = False
    media_was_moved = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise OutputError(
                f"Refusing to relocate {source}: destination {destination} is occupied."
            )
        source.replace(destination)
        media_was_moved = True
        if state.write_sidecars:
            if new_sidecar.exists() or new_sidecar.is_symlink():
                raise OutputError(
                    f"Refusing to relocate sidecar: destination {new_sidecar} is occupied."
                )
            if old_sidecar.exists() or old_sidecar.is_symlink():
                old_sidecar.replace(new_sidecar)
                sidecar_was_moved = True
            write_sidecar(state, destination)
        _remove_empty_export_dirs(source.parent, boundary=library_root)
    except OutputError as exc:
        rollback_error = _rollback_relocation(
            source, destination, old_sidecar, new_sidecar, media_was_moved, sidecar_was_moved
        )
        if rollback_error is not None:
            raise OutputError(f"{exc} Rollback also failed: {rollback_error}.") from exc
        raise
    except OSError as exc:
        rollback_error = _rollback_relocation(
            source, destination, old_sidecar, new_sidecar, media_was_moved, sidecar_was_moved
        )
        rollback = f" Rollback also failed: {rollback_error}." if rollback_error else ""
        raise OutputError(
            f"Cannot relocate verified media {source} to {destination}: {exc}.{rollback}"
        ) from exc


def _rollback_relocation(
    source: Path,
    destination: Path,
    old_sidecar: Path,
    new_sidecar: Path,
    media_was_moved: bool,
    sidecar_was_moved: bool,
) -> str | None:
    try:
        if media_was_moved or sidecar_was_moved:
            source.parent.mkdir(parents=True, exist_ok=True)
        if sidecar_was_moved and new_sidecar.exists():
            new_sidecar.replace(old_sidecar)
        if media_was_moved and destination.exists():
            destination.replace(source)
    except OSError as exc:
        return f"cannot roll back relocation from {destination} to {source}: {exc}"
    return None


def _clean_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise OutputError(f"Cannot clean temporary output {path}: {exc}") from exc


def _remove_empty_export_dirs(start: Path, *, boundary: Path) -> None:
    """Remove empty exporter-created directories without crossing the library root."""
    try:
        start.relative_to(boundary)
    except ValueError:
        return
    current = start
    while current != boundary:
        try:
            current.rmdir()
        except OSError as exc:
            try:
                if any(current.iterdir()):
                    return
            except OSError as inspect_exc:
                raise OutputError(
                    f"Cannot inspect export directory {current}: {inspect_exc}"
                ) from inspect_exc
            raise OutputError(f"Cannot remove empty export directory {current}: {exc}") from exc
        current = current.parent


def _reconcile_absent(runner: _Runner) -> None:
    absent = sorted(set(runner.current) - runner.seen)
    runner.report.absent = len(absent)
    for asset_id in absent:
        entry = runner.current[asset_id]
        if runner.cfg.stale_assets is StaleAssetPolicy.QUARANTINE:
            moved = _quarantine_owned_outputs(runner, entry)
            if moved:
                runner.report.quarantined += 1
        else:
            runner.report.warnings.append(
                f"Asset {asset_id} is absent from the current scope; its managed "
                "outputs were preserved as orphans."
            )


def _quarantine_owned_outputs(runner: _Runner, entry: AssetState) -> bool:
    if entry.mode == ExportMode.SIDECAR:
        media = runner.media_path(entry)
        owned = [media.with_name(media.name + ".xmp")]
    else:
        media = runner.media_path(entry)
        owned = [media, media.with_name(media.name + ".xmp")]
    present = [source for source in owned if source.exists() or source.is_symlink()]
    for source in present:
        if source.is_symlink() or not source.is_file():
            raise OutputError(f"Refusing to quarantine non-regular managed path {source}.")
    moves = [
        (
            source,
            runner.cfg.out / QUARANTINE_DIR / entry.asset_id / uuid4().hex / source.name,
        )
        for source in present
    ]
    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            completed.append((source, target))
        if entry.mode == ExportMode.SELF_CONTAINED:
            for parent in {source.parent for source in present}:
                _remove_empty_export_dirs(parent, boundary=runner.cfg.out / LIBRARY_DIR)
    except (OSError, OutputError) as exc:
        rollback_errors: list[str] = []
        for source, target in reversed(completed):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                target.replace(source)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target} to {source}: {rollback_exc}")
        rollback = (
            f" Rollback also failed: {'; '.join(rollback_errors)}." if rollback_errors else ""
        )
        if isinstance(exc, OutputError):
            raise OutputError(f"{exc}{rollback}") from exc
        failed_source, failed_target = moves[len(completed)]
        raise OutputError(
            f"Cannot quarantine managed output {failed_source} to {failed_target}: {exc}.{rollback}"
        ) from exc
    return bool(completed)


def _build_views(cfg: ExportConfig, runner: _Runner, report: ExportReport) -> None:
    if not (cfg.album_view or cfg.people_view):
        return
    album_groups: dict[str, list[Path]] = {}
    people_groups: dict[str, list[Path]] = {}
    for entry in runner.candidate.values():
        media = runner.media_path(entry)
        for album in entry.albums:
            album_groups.setdefault(album, []).append(media)
        for person in entry.people:
            people_groups.setdefault(person, []).append(media)
    if cfg.album_view:
        report.album_links = build_view(cfg.out / "albums", album_groups)
    if cfg.people_view:
        report.people_links = build_view(cfg.out / "people", people_groups)
