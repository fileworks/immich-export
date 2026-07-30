from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path

import pytest

from immich_export.durable_manifest import DurableManifestQueue
from immich_export.errors import OutputError
from immich_export.manifest import AssetState, ManifestWriter, load_index

from .test_manifest import _entry


@pytest.mark.asyncio
async def test_termination_before_group_commit_leaves_work_unacknowledged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    with ManifestWriter(path) as writer:
        queue = DurableManifestQueue(writer, batch_size=10, flush_interval_seconds=60)
        await queue.__aenter__()
        pending = asyncio.create_task(queue.publish(_entry("not-durable")))
        await asyncio.sleep(0)
        assert queue._task is not None
        queue._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queue._task
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert load_index(path) == {}


@pytest.mark.asyncio
async def test_group_commit_acknowledges_only_after_one_batch_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.jsonl"
    syncs: list[int] = []
    real_fsync = os.fsync

    def observe_fsync(descriptor: int) -> None:
        syncs.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    with ManifestWriter(path) as writer:
        async with DurableManifestQueue(writer, batch_size=3, flush_interval_seconds=10) as queue:
            receipts = await asyncio.gather(
                queue.publish(_entry("a1")),
                queue.publish(_entry("a2")),
                queue.publish(_entry("a3")),
            )
            assert set(load_index(path)) == {"a1", "a2", "a3"}

    assert len(syncs) == 1
    assert {receipt.durable_sequence for receipt in receipts} == {3}
    assert {receipt.batch_size for receipt in receipts} == {3}


@pytest.mark.asyncio
async def test_failure_during_group_commit_acknowledges_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.jsonl"
    with ManifestWriter(path) as writer:
        queue = DurableManifestQueue(writer, batch_size=2, flush_interval_seconds=10)
        await queue.__aenter__()

        def fail(_entries: Iterable[AssetState]) -> int:
            writer._fh.write('{"asset_id":"truncated"')
            writer._fh.flush()
            raise OutputError("injected synchronization failure")

        monkeypatch.setattr(writer, "append_batch", fail)
        results = await asyncio.gather(
            queue.publish(_entry("a1")),
            queue.publish(_entry("a2")),
            return_exceptions=True,
        )
        with pytest.raises(OutputError):
            await queue.close()

    assert all(isinstance(result, OutputError) for result in results)
    assert load_index(path) == {}


@pytest.mark.asyncio
async def test_committed_receipt_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    with ManifestWriter(path) as writer:
        async with DurableManifestQueue(
            writer, batch_size=50, flush_interval_seconds=0.01
        ) as queue:
            receipt = await queue.publish(_entry("durable"))

    assert receipt.durable_sequence == 1
    assert set(load_index(path)) == {"durable"}
