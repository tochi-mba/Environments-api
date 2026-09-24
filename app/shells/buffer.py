"""A bounded ring buffer over a monotonically increasing byte offset.

Readers hold a cursor, not a position in the buffer, so a reader that falls behind is told
exactly how many bytes it missed instead of silently receiving a gap.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class OutputChunk:
    """What a read returns.

    ``dropped_bytes`` counts what eviction took before the read got there;
    ``truncated_bytes`` counts what a span read left out to stay within its cap. A cursor
    read has no span to leave anything out of, so there it is always zero.
    """

    data: bytes
    cursor: int
    next_cursor: int
    dropped_bytes: int
    end: int
    truncated_bytes: int = 0


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

    def read_span(self, start: int, end: int, max_bytes: int, *, tail: bool = False) -> OutputChunk:
        """At most ``max_bytes`` of ``[start, end)``: its head, or with ``tail`` its tail.

        A test run or a build prints its verdict last, so a head-only read of a long one
        returns everything except the part that says what happened. What the cap left out
        is counted in ``truncated_bytes`` rather than left to be worked out from cursors: the
        hub never did, and told the model almost nothing was omitted when most of a long
        log was.
        """
        length = max(0, min(end - start, max_bytes))
        cursor = end - length if tail else start
        chunk = self.read(cursor, length)
        # A live span's end is a snapshot: eviction can move the read past it, and bytes
        # returned beyond the end were not cut.
        return replace(chunk, truncated_bytes=cursor - start + max(0, end - chunk.next_cursor))
