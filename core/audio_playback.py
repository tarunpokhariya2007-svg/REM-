"""Bounded, ordered buffering for streamed assistant PCM playback."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PlaybackChunk:
    data: bytes
    generation: int
    queued_at: float


class PlaybackBuffer:
    """A bounded queue that applies producer backpressure instead of dropping PCM."""

    def __init__(self, maxsize: int = 600):
        if int(maxsize) < 1:
            raise ValueError("playback queue capacity must be positive")
        self.maxsize = int(maxsize)
        self._queue: asyncio.Queue[PlaybackChunk] = asyncio.Queue(maxsize=self.maxsize)
        self.generated = 0
        self.enqueued = 0
        self.played = 0
        self.discarded: dict[str, int] = {}
        self.high_water = 0
        self.producer_wait_seconds = 0.0
        self.playback_latency_seconds = 0.0
        self._closed = False
        self._created_at = time.monotonic()

    async def put(self, data: bytes, generation: int) -> bool:
        if self._closed:
            self._discard("shutdown")
            return False
        chunk = PlaybackChunk(bytes(data), int(generation), time.monotonic())
        self.generated += 1
        started = time.monotonic()
        await self._queue.put(chunk)
        self.enqueued += 1
        waited = time.monotonic() - started
        self.producer_wait_seconds += waited
        self.high_water = max(self.high_water, self.qsize())
        if self._closed:
            self.discard_generation(chunk.generation, "shutdown")
            return False
        return True

    async def get(self) -> PlaybackChunk:
        return await self._queue.get()

    def get_nowait(self) -> PlaybackChunk:
        return self._queue.get_nowait()

    def mark_played(self, chunks: int = 1, queued_at: float | None = None) -> None:
        self.played += int(chunks)
        if queued_at is not None:
            self.playback_latency_seconds += max(0.0, time.monotonic() - queued_at)

    def discard_generation(self, generation: int, reason: str) -> int:
        kept: list[PlaybackChunk] = []
        removed = 0
        while True:
            try:
                chunk = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if chunk.generation == generation:
                removed += 1
            else:
                kept.append(chunk)
        for chunk in kept:
            self._queue.put_nowait(chunk)
        if removed:
            self._discard(reason, removed)
        return removed

    def discard_all(self, reason: str) -> int:
        count = 0
        while True:
            try:
                self._queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        if count:
            self._discard(reason, count)
        return count

    def close(self, reason: str = "shutdown") -> int:
        self._closed = True
        return self.discard_all(reason)

    def reopen(self) -> None:
        self._closed = False

    def _discard(self, reason: str, count: int = 1) -> None:
        self.discarded[reason] = self.discarded.get(reason, 0) + int(count)

    def record_discard(self, reason: str, count: int = 1) -> None:
        self._discard(reason, count)

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def stats(self) -> dict:
        elapsed = max(0.001, time.monotonic() - self._created_at)
        return {
            "size": self.qsize(), "capacity": self.maxsize,
            "high_water": self.high_water, "generated": self.generated,
            "enqueued": self.enqueued,
            "played": self.played, "discarded": dict(self.discarded),
            "generated_chunks_per_s": round(self.generated / elapsed, 2),
            "played_chunks_per_s": round(self.played / elapsed, 2),
            "producer_wait_ms": round(self.producer_wait_seconds * 1000, 2),
            "mean_queue_latency_ms": round(
                self.playback_latency_seconds * 1000 / max(1, self.played), 2),
        }
