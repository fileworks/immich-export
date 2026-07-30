"""Bounded asynchronous coordination around the single manifest writer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from .manifest import AssetState, ManifestWriter


@dataclass(frozen=True)
class CommitReceipt:
    durable_sequence: int
    batch_size: int


@dataclass
class _Pending:
    entry: AssetState
    acknowledged: asyncio.Future[CommitReceipt]


_STOP = object()


class DurableManifestQueue:
    """One writer task; producers are acknowledged only after ``fsync``."""

    def __init__(
        self,
        writer: ManifestWriter,
        *,
        batch_size: int = 128,
        flush_interval_seconds: float = 0.1,
        queue_size: int | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("manifest batch size must be at least 1")
        if flush_interval_seconds <= 0:
            raise ValueError("manifest flush interval must be positive")
        self.writer = writer
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[_Pending | object] = asyncio.Queue(
            maxsize=queue_size or batch_size * 2
        )
        self.durable_count = 0
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run(), name="manifest-single-writer")
        return self

    async def publish(self, entry: AssetState) -> CommitReceipt:
        if self._failure is not None:
            raise self._failure
        loop = asyncio.get_running_loop()
        acknowledged: asyncio.Future[CommitReceipt] = loop.create_future()
        await self.queue.put(_Pending(entry, acknowledged))
        return await acknowledged

    async def close(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        if not task.done():
            await self.queue.put(_STOP)
        await asyncio.shield(task)
        if self._failure is not None:
            raise self._failure

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _run(self) -> None:
        stopped = False
        while not stopped:
            item = await self.queue.get()
            if item is _STOP:
                return
            assert isinstance(item, _Pending)
            batch = [item]
            deadline = asyncio.get_running_loop().time() + self.flush_interval_seconds
            while len(batch) < self.batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if item is _STOP:
                    stopped = True
                    break
                assert isinstance(item, _Pending)
                batch.append(item)
            try:
                committed = self.writer.append_batch(pending.entry for pending in batch)
                self.durable_count += committed
                receipt = CommitReceipt(self.durable_count, committed)
            except BaseException as exc:
                self._failure = exc
                for pending in batch:
                    if not pending.acknowledged.done():
                        pending.acknowledged.set_exception(exc)
                while not self.queue.empty():
                    queued = self.queue.get_nowait()
                    if isinstance(queued, _Pending) and not queued.acknowledged.done():
                        queued.acknowledged.set_exception(exc)
                while True:
                    queued = await self.queue.get()
                    if queued is _STOP:
                        return
                    assert isinstance(queued, _Pending)
                    if not queued.acknowledged.done():
                        queued.acknowledged.set_exception(exc)
            for pending in batch:
                if not pending.acknowledged.done():
                    pending.acknowledged.set_result(receipt)
