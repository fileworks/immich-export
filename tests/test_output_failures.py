"""Filesystem fault injection: owned mutations classify as output failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from immich_export import manifest as manifest_module
from immich_export import report as report_module
from immich_export.cli import app
from immich_export.client import ImmichClient
from immich_export.config import ExportConfig, StaleAssetPolicy
from immich_export.errors import OutputError
from immich_export.exporter import run_export
from immich_export.manifest import ManifestWriter, atomic_write_text
from immich_export.progress import Progress
from immich_export.report import ExportReport
from immich_export.views import build_view

from .fake_immich import BASE, FakeImmich


@pytest.mark.parametrize(
    "operation",
    [
        "write XMP sidecar",
        "write current CSV",
        "write export report",
        "publish current manifest",
    ],
)
def test_atomic_outputs_preserve_prior_file_and_clean_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    final = tmp_path / "output"
    final.write_text("prior")
    original_replace = Path.replace

    def fail_final_replace(self: Path, target: str | Path) -> Path:
        if Path(target) == final:
            raise OSError("injected replace failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_final_replace)
    with pytest.raises(OutputError, match=operation):
        atomic_write_text(final, "candidate", operation=operation)

    assert final.read_text() == "prior"
    assert not list(tmp_path.glob(".output.*.tmp"))


class _FailingHistoryFile:
    def write(self, content: str) -> int:
        del content
        raise OSError("disk full")

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        pass


def test_history_append_oserror_is_contextual_output_error(tmp_path: Path) -> None:
    from tests.test_manifest import _entry

    writer = ManifestWriter(tmp_path / "manifest.jsonl")
    cast(Any, writer)._fh = _FailingHistoryFile()
    with pytest.raises(OutputError, match="append export history"):
        writer.append(_entry("a1"))


async def test_download_temporary_open_failure_is_output_error(
    fake_immich: FakeImmich,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / ".download.tmp"
    original_open = Path.open

    def fail_download_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == temporary:
            raise OSError("no space")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_download_open)
    async with ImmichClient(BASE, "k") as client:
        with pytest.raises(OutputError, match="download temporary"):
            await client.download_original("a1", temporary)


async def test_destination_directory_failure_aborts_current_publication(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = out_dir / "library/2019/04"
    original_mkdir = Path.mkdir

    def fail_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == failing:
            raise OSError("read-only")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(OutputError, match="destination directory"):
        await run_export(base_config)
    assert not (out_dir / "manifest-current.jsonl").exists()


async def test_media_promotion_failure_cleans_temp_and_publishes_no_current(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = Path.replace

    def fail_download_replace(self: Path, target: str | Path) -> Path:
        target_path = Path(target)
        if (
            ".download-" in self.name
            and ".xmp." not in self.name
            and not target_path.name.endswith(".xmp")
        ):
            raise OSError("rename denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_download_replace)
    with pytest.raises(OutputError, match="promote verified download"):
        await run_export(base_config)
    assert not (out_dir / "manifest-current.jsonl").exists()
    assert not list(out_dir.rglob("*.download-*.tmp*"))


@pytest.mark.parametrize(
    ("failed_operation", "expected"),
    [
        ("write current CSV", "manifest.csv"),
        ("write export report", "export-report.txt"),
        ("publish current manifest", "manifest-current.jsonl"),
    ],
)
async def test_derived_output_failure_keeps_prior_current_and_atomic_file(
    fake_immich: FakeImmich,
    base_config: ExportConfig,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_operation: str,
    expected: str,
) -> None:
    await run_export(base_config)
    current = out_dir / "manifest-current.jsonl"
    current_before = current.read_bytes()
    output = out_dir / expected
    output_before = output.read_bytes()
    original_atomic = manifest_module.atomic_write_text

    def fail_selected(path: Path, content: str, *, operation: str) -> None:
        if operation == failed_operation:
            raise OutputError(f"injected {operation} failure at {path}")
        original_atomic(path, content, operation=operation)

    if failed_operation == "write export report":
        monkeypatch.setattr(report_module, "atomic_write_text", fail_selected)
    else:
        monkeypatch.setattr(manifest_module, "atomic_write_text", fail_selected)
    with pytest.raises(OutputError, match=failed_operation):
        await run_export(base_config)

    assert current.read_bytes() == current_before
    assert output.read_bytes() == output_before


def test_symlink_failure_preserves_prior_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "library/photo.jpg"
    target.parent.mkdir()
    target.write_bytes(b"photo")
    view = tmp_path / "albums"
    prior_group = view / "Prior"
    prior_group.mkdir(parents=True)
    prior_link = prior_group / "photo.jpg"
    prior_link.symlink_to("../../library/photo.jpg")

    def fail_symlink(self: Path, target: str | Path, target_is_directory: bool = False) -> None:
        del self, target, target_is_directory
        raise OSError("links disabled")

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    with pytest.raises(OutputError, match="publish managed view"):
        build_view(view, {"New": [target]})
    assert prior_link.is_symlink()


async def test_quarantine_move_failure_is_output_error_and_keeps_current(
    fake_immich: FakeImmich,
    out_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = ExportConfig(server=BASE, api_key="k", out=out_dir)
    await run_export(initial)
    current = out_dir / "manifest-current.jsonl"
    current_before = current.read_bytes()
    media = out_dir / "library/2019/04/IMG_0001.jpg"
    fake_immich.remove_asset("a1")
    original_replace = Path.replace

    def fail_quarantine(self: Path, target: str | Path) -> Path:
        if self == media:
            raise OSError("move denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_quarantine)
    cfg = ExportConfig(
        server=BASE,
        api_key="k",
        out=out_dir,
        stale_assets=StaleAssetPolicy.QUARANTINE,
    )
    with pytest.raises(OutputError, match="quarantine managed output"):
        await run_export(cfg)
    assert media.is_file()
    assert current.read_bytes() == current_before


def test_cli_classifies_output_failure_as_exit_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from immich_export import exporter

    async def fail_export(cfg: ExportConfig, *, progress: Progress | None = None) -> ExportReport:
        del cfg, progress
        raise OutputError("injected output failure")

    monkeypatch.setattr(exporter, "run_export", fail_export)
    result = CliRunner().invoke(
        app, ["--server", BASE, "--api-key", "k", "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 4
    assert "injected output failure" in result.output
    assert "Unexpected error" not in result.output
