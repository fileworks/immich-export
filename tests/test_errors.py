"""Error paths: bad key, unreachable server, empty library, bad config."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from immich_export.config import ExportConfig, ExportMode
from immich_export.errors import AuthError, ConfigError, ServerUnreachableError
from immich_export.exporter import run_export
from immich_export.manifest import load_index

from .fake_immich import BASE, FakeImmich


async def test_bad_api_key_raises_auth_error(
    respx_mock: respx.MockRouter, base_config: ExportConfig
) -> None:
    respx_mock.get(f"{BASE}/api/server/ping").respond(json={"res": "pong"})
    respx_mock.get(f"{BASE}/api/server/about").respond(401)
    with pytest.raises(AuthError, match="check your Immich API key"):
        await run_export(base_config)


async def test_unreachable_server_raises_clear_error(
    respx_mock: respx.MockRouter, base_config: ExportConfig
) -> None:
    respx_mock.get(f"{BASE}/api/server/ping").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(ServerUnreachableError, match="Cannot reach Immich"):
        await run_export(base_config)


async def test_empty_library_writes_empty_manifest(
    respx_mock: respx.MockRouter, base_config: ExportConfig, out_dir: Path
) -> None:
    fake = FakeImmich()
    fake.install(respx_mock)
    report = await run_export(base_config)
    assert report.total == 0
    assert report.errors == []
    assert (out_dir / "manifest.jsonl").is_file()
    assert load_index(out_dir / "manifest.jsonl") == {}
    assert (out_dir / "export-report.txt").is_file()


class TestConfigValidation:
    def test_malformed_server_url(self, out_dir: Path) -> None:
        cfg = ExportConfig(server="immich.local", api_key="k", out=out_dir)
        with pytest.raises(ConfigError, match="http"):
            cfg.validate()

    def test_missing_api_key(self, out_dir: Path) -> None:
        cfg = ExportConfig(server=BASE, api_key="", out=out_dir)
        with pytest.raises(ConfigError, match="IMMICH_API_KEY"):
            cfg.validate()

    def test_sidecar_mode_requires_library_root(self, out_dir: Path) -> None:
        cfg = ExportConfig(server=BASE, api_key="k", out=out_dir, mode=ExportMode.SIDECAR)
        with pytest.raises(ConfigError, match="library-root"):
            cfg.validate()

    def test_bad_layout_token(self, out_dir: Path) -> None:
        cfg = ExportConfig(server=BASE, api_key="k", out=out_dir, layout="{nope}")
        with pytest.raises(ConfigError, match="Unknown layout token"):
            cfg.validate()
