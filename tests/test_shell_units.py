from __future__ import annotations

import pytest

from app.shells.buffer import RingBuffer
from app.shells.framing import Frame, FrameParser, exec_script, new_nonce
from app.shells.redact import Redactor


def test_ring_buffer_cursor_semantics() -> None:
    buf = RingBuffer(8)
    buf.append(b"")
    assert buf.end == 0 and buf.start == 0
    buf.append(b"abcdef")
    chunk = buf.read(0, 100)
    assert chunk.data == b"abcdef" and chunk.next_cursor == 6 and chunk.dropped_bytes == 0
    chunk = buf.read(2, 2)
    assert chunk.data == b"cd" and chunk.next_cursor == 4 and chunk.end == 6
    buf.append(b"ghij")  # 10 bytes total, 8 held: "cdefghij"
    assert buf.start == 2 and buf.end == 10
    chunk = buf.read(0, 100)
    assert chunk.dropped_bytes == 2 and chunk.cursor == 2 and chunk.data == b"cdefghij"
    chunk = buf.read(50, 10)
    assert chunk.data == b"" and chunk.cursor == 10 and chunk.next_cursor == 10
    with pytest.raises(ValueError, match="non-negative"):
        buf.read(-1, 1)
    with pytest.raises(ValueError, match="positive"):
        RingBuffer(0)


def test_ring_buffer_large_append_evicts_everything_old() -> None:
    buf = RingBuffer(4)
    buf.append(b"0123456789")
    assert buf.read(0, 10).data == b"6789"
    assert buf.read(0, 10).dropped_bytes == 6


def test_exec_script_shapes() -> None:
    nonce = new_nonce()
    assert len(nonce) == 32
    script = exec_script("echo 'hi'", nonce)
    assert script.startswith(b"eval 'echo '\"'\"'hi'\"'\"''; printf")
    assert nonce.encode() in script and script.endswith(b" $?\n")
    with_env = exec_script("x", nonce, {"GITHUB_TOKEN": "a b"})
    assert with_env.startswith(b"GITHUB_TOKEN='a b' eval")


def test_frame_parser_across_boundaries() -> None:
    nonce = "a" * 32
    parser = FrameParser()
    frame = b"\x1e" + nonce.encode() + b":17\x1e"
    events = parser.feed(b"hello" + frame[:10])
    assert events == [b"hello"]
    events = parser.feed(frame[10:] + b"\nworld")
    assert events == [Frame(nonce, 17), b"\nworld"]
    assert Frame(nonce, 17).raw() == frame
    assert parser.flush() == b""


def test_frame_parser_stray_bytes() -> None:
    parser = FrameParser()
    assert parser.feed(b"a\x1eb") == [b"a"]  # the rest is held: might be a frame starting
    assert parser.flush() == b"\x1eb"
    parser = FrameParser()
    events = parser.feed(b"\x1e" + b"x" * 100)
    assert events == [b"\x1e" + b"x" * 100]
    events = parser.feed(b"\x1e\x1e" + b"y" * 100)
    assert events == [b"\x1e\x1e" + b"y" * 100]
    # A stray frame byte directly before a real frame does not swallow the frame.
    nonce = "c" * 32
    events = parser.feed(b"a\x1eb\x1e" + nonce.encode() + b":0\x1e")
    assert events == [b"a\x1eb", Frame(nonce, 0)]
    # Partial frames are held at every stage of arrival.
    parser = FrameParser()
    assert parser.feed(b"\x1e" + b"c" * 32) == []
    assert parser.feed(b":") == []
    assert parser.feed(b"-1") == []
    assert parser.feed(b"2\x1e") == [Frame(nonce, -12)]
    assert parser.feed(b"\x1e" + b"c" * 32 + b":x") == [b"\x1e" + b"c" * 32 + b":x"]
    parser = FrameParser()
    assert parser.feed(b"") == []
    assert parser.feed(b"plain") == [b"plain"]


def test_frame_parser_merges_adjacent_output() -> None:
    parser = FrameParser()
    nonce = "b" * 32
    data = b"x\x1e" + b"z" * 70 + b"\x1e" + nonce.encode() + b":0\x1e" + b"\x1e"
    events = parser.feed(data)
    assert events == [b"x\x1e" + b"z" * 70, Frame(nonce, 0)]
    assert parser.flush() == b"\x1e"


def test_redactor_basic_and_split() -> None:
    red = Redactor({"secret123": "github", "tok": "npm", "": "empty"})
    assert red.active
    out = red.feed(b"echo secret123 and tok and sec")
    assert out == b"echo \xc2\xabredacted:github\xc2\xbb and \xc2\xabredacted:npm\xc2\xbb and "
    out += red.feed(b"ret123!")
    assert out.endswith("«redacted:github»!".encode())
    assert red.flush() == b""
    out = red.feed(b"tail secr")
    assert out == b"tail "
    assert red.flush() == b"secr"
    assert not Redactor({}).active
    assert Redactor({}).feed(b"x") == b"x"


def test_redactor_holds_back_only_prefixes() -> None:
    red = Redactor({"abcdef": "svc"})
    assert red.feed(b"xyzab") == b"xyz"
    assert red.feed(b"cdefgh") == "«redacted:svc»gh".encode()
    assert red.feed(b"zzzzzzzz") == b"zzzzzzzz"
