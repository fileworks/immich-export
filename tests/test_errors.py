"""Error paths: bad key, unreachable server, empty library, bad config."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from immich_export.client import ImmichClient
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


class TestClientTransportEdges:
    """Failures that are neither a transport drop nor a clean HTTP status."""

    async def test_unreadable_response_body_leaves_no_temporary_behind(
        self,
        respx_mock: respx.MockRouter,
        tmp_path: Path,
    ) -> None:
        """`DecodingError` is an `HTTPError` but not a `TransportError`.

        It therefore escaped the cleanup handlers, and a corrupt compressed
        stream left the part-written download temporary on disk.
        """
        respx_mock.get(f"{BASE}/api/assets/asset-1/original").mock(
            # A byte-stream body defers decoding to the stream, which is where
            # a real truncated or corrupt response fails.
            return_value=httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"this is not gzip"),
            )
        )
        temporary = tmp_path / ".photo.jpg.download"

        async with ImmichClient(BASE, "test-key") as client:
            with pytest.raises(ServerUnreachableError):
                await client.download_original("asset-1", temporary)

        assert not temporary.exists()
        assert list(tmp_path.iterdir()) == []

    async def test_a_non_numeric_page_token_is_reported_not_raised_raw(
        self,
        respx_mock: respx.MockRouter,
    ) -> None:
        """`nextPage` is an ordinal; an opaque cursor is a contract change.

        `int(page_token)` used to end a half-finished export with a bare
        `ValueError` and no indication of what the server had done.
        """
        respx_mock.post(f"{BASE}/api/search/metadata").mock(
            return_value=httpx.Response(
                200,
                json={"assets": {"items": [], "nextPage": "cursor:abc"}},
            )
        )

        async with ImmichClient(BASE, "test-key") as client:
            with pytest.raises(ServerUnreachableError, match="non-numeric page token"):
                async for _page in client.iter_assets(visibilities=("timeline",)):
                    pass
