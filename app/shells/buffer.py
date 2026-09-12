"""A bounded ring buffer over a monotonically increasing byte offset.

Readers hold a cursor, not a position in the buffer, so a reader that falls behind is told
exactly how many bytes it missed instead of silently receiving a gap.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutputChunk:
    """What a read returns."""

    data: bytes
    cursor: int
    next_cursor: int
    dropped_bytes: int
    end: int


class RingBuffer:
    """Thread-safe; holds the most recent ``capacity`` bytes."""

    def __init__(self, capacity: int) -> None:
        """Keep at most ``capacity`` bytes."""
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._data = bytearray()
        self._start = 0
        self._lock = threading.Lock()

    @property
    def end(self) -> int:
        """Offset one past the newest byte."""
        with self._lock:
            return self._start + len(self._data)

    @property
    def start(self) -> int:
        """Offset of the oldest byte still held."""
        with self._lock:
            return self._start

    def append(self, data: bytes) -> None:
        """Add ``data``, evicting the oldest bytes if needed."""
        if not data:
            return
        with self._lock:
            self._data += data
            excess = len(self._data) - self._capacity
            if excess > 0:
                del self._data[:excess]
                self._start += excess

    def read(self, cursor: int, max_bytes: int) -> OutputChunk:
        """Bytes from ``cursor``; ``dropped_bytes`` counts what had already been evicted."""
        if cursor < 0 or max_bytes < 0:
            raise ValueError("cursor and max_bytes must be non-negative")
        with self._lock:
            end = self._start + len(self._data)
            dropped = 0
            if cursor < self._start:
                dropped = self._start - cursor
                cursor = self._start
            if cursor > end:
                cursor = end
            offset = cursor - self._start
            data = bytes(self._data[offset : offset + max_bytes])
            return OutputChunk(data, cursor, cursor + len(data), dropped, end)
