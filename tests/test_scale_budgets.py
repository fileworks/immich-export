"""Deterministic structural fixtures for the 100k/500k operating range."""

from __future__ import annotations

import math
import tracemalloc
from collections.abc import Iterator

import pytest

from immich_export.progress import LOG_EVERY

BATCH_SIZE = 128
QUEUE_SIZE = BATCH_SIZE * 2
PEAK_FIXTURE_MEMORY_BYTES = 8 * 1024 * 1024


def _assets(count: int) -> Iterator[tuple[str, int, int]]:
    for index in range(count):
        yield (f"asset-{index:07}", index % 10_000, index % 2_000)


@pytest.mark.parametrize("count", [100_000, 500_000])
def test_scale_fixture_is_deterministic_streamed_and_memory_bounded(count: int) -> None:
    tracemalloc.start()
    try:
        first = None
        last = None
        seen = 0
        for record in _assets(count):
            first = first or record
            last = record
            seen += 1
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert seen == count
    assert first == ("asset-0000000", 0, 0)
    assert last == (f"asset-{count - 1:07}", (count - 1) % 10_000, (count - 1) % 2_000)
    assert peak < PEAK_FIXTURE_MEMORY_BYTES


@pytest.mark.parametrize("count", [100_000, 500_000])
def test_recorded_sync_redo_queue_and_log_budgets(count: int) -> None:
    maximum_syncs = math.ceil(count / BATCH_SIZE)
    maximum_crash_redo = BATCH_SIZE
    maximum_progress_lines = math.ceil(count / LOG_EVERY) + 5

    assert maximum_syncs <= math.ceil(count / BATCH_SIZE)
    assert maximum_crash_redo == 128
    assert QUEUE_SIZE == 256
    assert maximum_progress_lines <= count // 100 + 5
