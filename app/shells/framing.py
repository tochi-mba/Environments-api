"""Command framing: how a persistent shell tells us a command finished.

Each command is followed by a ``printf`` of a frame carrying a random per-command nonce and
``$?``. The nonce is why the exit code can be trusted: a fixed sentinel could be forged by
the command simply printing it, a random one cannot be guessed.
"""

from __future__ import annotations

import secrets
import shlex
from dataclasses import dataclass

from app.constants import FRAME_BYTE, FRAME_PATTERN, PARTIAL_FRAME_PATTERN


def new_nonce() -> str:
    """A fresh 128-bit hex nonce."""
    return secrets.token_hex(16)


def exec_script(command: str, nonce: str, env: dict[str, str] | None = None) -> bytes:
    """The bytes written to the shell's stdin to run ``command`` and report its exit code.

    ``eval`` of a single-quoted literal keeps the command's own quoting intact, lets ``cd``
    and variable assignments persist, and turns an unterminated quote into a syntax error
    with a frame rather than a shell that hangs waiting for the closing quote.
    Temporary assignments before ``eval`` scope injected credentials to that one command.
    """
    prefix = ""
    if env:
        prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items()) + " "
    frame = "printf '\\036%s:%d\\036' " + nonce + " $?"
    return f"{prefix}eval {shlex.quote(command)}; {frame}\n".encode()


@dataclass(frozen=True, slots=True)
class Frame:
    """A completed exit-code marker."""

    nonce: str
    exit_code: int

    def raw(self) -> bytes:
        """The marker bytes, for re-emitting a marker that was not ours."""
        return FRAME_BYTE + f"{self.nonce}:{self.exit_code}".encode() + FRAME_BYTE


class FrameParser:
    """Splits a byte stream into output and frames, across arbitrary chunk boundaries."""

    def __init__(self) -> None:
        """Start with nothing pending."""
        self._pending = b""

    def feed(self, data: bytes) -> list[bytes | Frame]:
        """Consume ``data`` and return output segments and frames in stream order."""
        buf = self._pending + data
        self._pending = b""
        events: list[bytes | Frame] = []
        pos = 0
        while True:
            start = buf.find(FRAME_BYTE, pos)
            if start < 0:
                _emit(events, buf[pos:])
                return events
            _emit(events, buf[pos:start])
            match = FRAME_PATTERN.match(buf, start)
            if match:
                events.append(Frame(match.group(1).decode(), int(match.group(2))))
                pos = match.end()
                continue
            if PARTIAL_FRAME_PATTERN.match(buf, start):
                # Might be a frame still arriving; hold it until more bytes settle it.
                self._pending = buf[start:]
                return events
            _emit(events, buf[start : start + 1])
            pos = start + 1

    def flush(self) -> bytes:
        """Release anything held back; call at end of stream."""
        pending, self._pending = self._pending, b""
        return pending


def _emit(events: list[bytes | Frame], data: bytes) -> None:
    if not data:
        return
    if events and isinstance(events[-1], bytes):
        events[-1] += data
    else:
        events.append(data)
