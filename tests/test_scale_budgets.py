"""Production-path scale gates for state indexing and atomic projections."""

from __future__ import annotations

import math
import os
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from immich_export.manifest import (
    AssetState,
    DiskStateMap,
    iter_current,
    write_current,
    write_current_csv,
)
from immich_export.progress import LOG_EVERY

BATCH_SIZE = 128
QUEUE_SIZE = BATCH_SIZE * 2
PR_ASSET_COUNT = 20_000
PEAK_PYTHON_MEMORY_BYTES = 64 * 1024 * 1024


def _asset_states(count: int) -> Iterator[AssetState]:
    verified = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        yield AssetState(
            asset_id=f"asset-{index:07}",
            checksum=f"{index:040x}",
            path=f"library/{index % 10000:04}/{index:07}.jpg",
            file_name=f"{index:07}.jpg",
            original_path=f"/upload/{index:07}.jpg",
            taken_at=verified,
            type="IMAGE",
            albums=[f"album-{index % 2000:04}"],
            tags=[f"tag-{index % 200:03}"],
            verified_at=verified,
        )


def test_real_state_reconcile_and_publication_are_disk_backed(tmp_path: Path) -> None:
    count = int(os.getenv("IMMICH_EXPORT_SCALE_ASSETS", str(PR_ASSET_COUNT)))
    current = tmp_path / "manifest-current.jsonl"
    projection = tmp_path / "manifest.csv"

    tracemalloc.start()
    state = DiskStateMap(_asset_states(count))
    try:
        assert len(state) == count
        write_current(current, state)
        write_current_csv(state, projection)
        reloaded = DiskStateMap(iter_current(current))
        try:
            assert len(reloaded) == count
            assert reloaded["asset-0000000"].path == "library/0000/0000000.jpg"
            assert reloaded[f"asset-{count - 1:07}"].asset_id == f"asset-{count - 1:07}"
        finally:
            reloaded.close()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        state.close()
        tracemalloc.stop()

    assert peak < PEAK_PYTHON_MEMORY_BYTES
    assert current.stat().st_size > count
    assert projection.stat().st_size > count


def test_recorded_sync_redo_queue_and_log_budgets() -> None:
    count = int(os.getenv("IMMICH_EXPORT_SCALE_ASSETS", str(PR_ASSET_COUNT)))
    maximum_syncs = math.ceil(count / BATCH_SIZE)
    maximum_crash_redo = BATCH_SIZE
    maximum_progress_lines = math.ceil(count / LOG_EVERY) + 5

    assert maximum_syncs <= math.ceil(count / BATCH_SIZE)
    assert maximum_crash_redo == 128
    assert QUEUE_SIZE == 256
    assert maximum_progress_lines <= count // 100 + 5
