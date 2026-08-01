from __future__ import annotations

import asyncio

import pytest

from immich_export.config import ExportConfig
from immich_export.exporter import run_export

from .fake_immich import BASE, FakeImmich


async def test_membership_worker_failure_cancels_full_producer_queue(
    fake_immich: FakeImmich, base_config: ExportConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(8):
        fake_immich.add_album(f"failure-{index}", f"Failure {index}", [])

    async def fail_membership(
        _client: object,
        *,
        album_id: str | None = None,
        tag_id: str | None = None,
    ) -> set[str]:
        del album_id, tag_id
        raise RuntimeError("membership lookup failed")

    monkeypatch.setattr(
        "immich_export.client.ImmichClient.search_asset_ids",
        fail_membership,
    )
    config = ExportConfig(
        server=BASE,
        api_key=base_config.api_key,
        out=base_config.out,
        concurrency=1,
    )

    with pytest.raises(RuntimeError, match="membership lookup failed"):
        await asyncio.wait_for(run_export(config), timeout=1.0)
