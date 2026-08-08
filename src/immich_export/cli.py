"""Typer CLI — thin layer over `exporter.run_export`; owns exit codes and messages."""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import ExportConfig, ExportMode, SidecarFormat, StaleAssetPolicy
from .errors import ImmichExportError
from .exit_codes import ExitCode
from .logging_config import configure_logging

app = typer.Typer(add_completion=False, context_settings={"help_option_names": ["-h", "--help"]})


def _version() -> str:
    try:
        return importlib.metadata.version("immich-export")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"immich-export {_version()}")
        raise typer.Exit()


@app.command()
def export(
    server: Annotated[
        str,
        typer.Option("--server", envvar="IMMICH_SERVER", help="Immich base URL."),
    ],
    api_key: Annotated[
        str,
        typer.Option("--api-key", envvar="IMMICH_API_KEY", help="Immich API key."),
    ] = "",
    out: Annotated[Path, typer.Option("--out", help="Export destination directory.")] = Path(
        "./immich-export"
    ),
    mode: Annotated[
        ExportMode,
        typer.Option("--mode", help="self-contained copies originals; sidecar only writes XMP."),
    ] = ExportMode.SELF_CONTAINED,
    layout: Annotated[
        str,
        typer.Option(
            "--layout",
            help="Primary tree layout; tokens: {year} {month} {day} {album} {type}.",
        ),
    ] = "{year}/{month}",
    album_view: Annotated[
        bool, typer.Option("--album-view/--no-album-view", help="Build albums/ symlink view.")
    ] = True,
    people_view: Annotated[
        bool, typer.Option("--people-view/--no-people-view", help="Build people/ symlink view.")
    ] = True,
    sidecars: Annotated[
        SidecarFormat,
        typer.Option("--sidecars", help="Sidecar format."),
    ] = SidecarFormat.XMP,
    since: Annotated[
        datetime | None,
        typer.Option("--since", help="Only assets taken on/after this date (incremental)."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--no-resume",
            help="Use prior state as a migration/resume hint; local bytes are always verified.",
        ),
    ] = True,
    include_hidden: Annotated[
        bool,
        typer.Option("--include-hidden", help="Also export hidden and locked-folder assets."),
    ] = False,
    library_root: Annotated[
        Path | None,
        typer.Option("--library-root", help="Storage-Template tree (required for --mode sidecar)."),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            envvar="IMMICH_EXPORT_CONCURRENCY",
            help="Parallel downloads and membership requests.",
        ),
    ] = 4,
    stale_assets: Annotated[
        StaleAssetPolicy,
        typer.Option(
            "--stale-assets",
            help="Preserve absent outputs (keep) or move manifest-owned outputs (quarantine).",
        ),
    ] = StaleAssetPolicy.KEEP,
    manifest_batch_size: Annotated[
        int,
        typer.Option(
            "--manifest-batch-size",
            envvar="IMMICH_EXPORT_MANIFEST_BATCH_SIZE",
            help="Verified history records synchronized per durable group.",
        ),
    ] = 128,
    manifest_flush_interval: Annotated[
        float,
        typer.Option(
            "--manifest-flush-interval",
            envvar="IMMICH_EXPORT_MANIFEST_FLUSH_INTERVAL",
            help="Maximum seconds before a partial history group is synchronized.",
        ),
    ] = 0.1,
    history_max_records: Annotated[
        int,
        typer.Option(
            "--history-max-records",
            envvar="IMMICH_EXPORT_HISTORY_MAX_RECORDS",
            help="Rotate active history at this many records.",
        ),
    ] = 100_000,
    history_max_bytes: Annotated[
        int,
        typer.Option(
            "--history-max-bytes",
            envvar="IMMICH_EXPORT_HISTORY_MAX_BYTES",
            help="Rotate active history at this many bytes.",
        ),
    ] = 128 * 1024 * 1024,
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            envvar="IMMICH_EXPORT_LOG_FILE",
            help="Rotating logfile (default: <out>/immich-export.log).",
        ),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging + full tracebacks.")
    ] = False,
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Export all Immich assets + metadata into a plain, human-readable folder tree."""
    logfile = log_file or out / "immich-export.log"
    logger = logging.getLogger(__name__)
    cfg = ExportConfig(
        server=server,
        api_key=api_key,
        out=out,
        mode=mode,
        layout=layout,
        album_view=album_view,
        people_view=people_view,
        write_sidecars=sidecars is SidecarFormat.XMP,
        since=since,
        resume=resume,
        include_hidden=include_hidden,
        library_root=library_root,
        concurrency=concurrency,
        stale_assets=stale_assets,
        manifest_batch_size=manifest_batch_size,
        manifest_flush_interval_seconds=manifest_flush_interval,
        history_max_records=history_max_records,
        history_max_bytes=history_max_bytes,
    )
    try:
        configure_logging(
            logfile,
            verbose=verbose,
            secrets=(api_key,),
        )
        logger.info("immich-export started; logfile=%s", logfile)
        from .exporter import run_export
        from .progress import Progress

        report = asyncio.run(run_export(cfg, progress=Progress(enabled=True)))
    except KeyboardInterrupt as exc:
        typer.secho("Interrupted; nothing was left half-written.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=ExitCode.INTERRUPTED) from exc
    except ImmichExportError as exc:
        logger.error("immich-export failed exit_code=%s: %s", exc.exit_code, exc)
        if verbose:
            traceback.print_exc()
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        logger.exception("immich-export failed unexpectedly")
        if verbose:
            traceback.print_exc()
        typer.secho(
            f"Unexpected error: {exc} (re-run with --verbose for the full traceback)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=ExitCode.FATAL) from exc

    if report.total == 0:
        typer.echo("Immich library is empty — nothing to export (manifest written).")
    else:
        typer.echo(
            f"Done: {report.exported} exported, {report.skipped} skipped, "
            f"{len(report.errors)} errors in {report.duration_seconds:.1f}s "
            f"→ {cfg.out} (see export-report.txt)"
        )
    if report.errors:
        logger.error(
            "immich-export completed outcome=partial total=%s durable=%s failures=%s",
            report.total,
            report.exported,
            len(report.errors),
        )
        raise typer.Exit(code=ExitCode.PARTIAL)
    logger.info(
        "immich-export completed outcome=complete total=%s durable=%s failures=0",
        report.total,
        report.exported,
    )


if __name__ == "__main__":
    app()
