"""Typer CLI — thin layer over `exporter.run_export`; owns exit codes and messages."""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import ExportConfig, ExportMode, SidecarFormat
from .errors import EXIT_UNEXPECTED, ImmichExportError

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
            help="Skip assets already exported with an unchanged checksum (via manifest.jsonl).",
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
    concurrency: Annotated[int, typer.Option("--concurrency", help="Parallel downloads.")] = 4,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging + full tracebacks.")
    ] = False,
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Export all Immich assets + metadata into a plain, human-readable folder tree."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s",
        stream=sys.stderr,
    )
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
    )
    try:
        from .exporter import run_export

        report = asyncio.run(run_export(cfg))
    except ImmichExportError as exc:
        if verbose:
            traceback.print_exc()
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        if verbose:
            traceback.print_exc()
        typer.secho(
            f"Unexpected error: {exc} (re-run with --verbose for the full traceback)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_UNEXPECTED) from exc

    if report.total == 0:
        typer.echo("Immich library is empty — nothing to export (manifest written).")
    else:
        typer.echo(
            f"Done: {report.exported} exported, {report.skipped} skipped, "
            f"{len(report.errors)} errors in {report.duration_seconds:.1f}s "
            f"→ {cfg.out} (see export-report.txt)"
        )


if __name__ == "__main__":
    app()
