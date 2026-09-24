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


def test_a_span_that_fits_comes_back_whole_from_either_end() -> None:
    buf = RingBuffer(64)
    buf.append(b"before|build ok\n")
    for tail in (False, True):
        chunk = buf.read_span(7, 16, 100, tail=tail)
        assert chunk.data == b"build ok\n" and chunk.cursor == 7 and chunk.next_cursor == 16
        assert chunk.truncated_bytes == 0 and chunk.dropped_bytes == 0


def test_a_tail_read_keeps_the_verdict_and_counts_the_head_it_cut() -> None:
    """The bug, named: a long command's output came back as its first bytes only.

    ``POST /v1/exec`` read ``max_output_bytes`` from the start of the command, and the hub
    asks for 64 KiB, so the end of a test run or a build log -- where the verdict is -- never
    reached it. What was cut was counted nowhere; only the ring buffer's evictions were, so
    the model was told a small number of bytes were omitted when most of the output was.
    """
    buf = RingBuffer(64)
    buf.append(b"collected 3 items\n...\n1 failed, 2 passed\n")
    tail = buf.read_span(0, 41, 20, tail=True)
    assert tail.data == b"\n1 failed, 2 passed\n" and tail.next_cursor == 41
    assert tail.truncated_bytes == 21 and tail.dropped_bytes == 0
    head = buf.read_span(0, 41, 20)
    assert head.data == b"collected 3 items\n.." and head.cursor == 0
    assert head.truncated_bytes == 21 and head.dropped_bytes == 0


def test_eviction_and_the_cap_are_counted_apart_and_add_up_to_the_span() -> None:
    buf = RingBuffer(8)
    buf.append(b"0123456789abcdef")  # 16 bytes written, "89abcdef" still held
    tail = buf.read_span(0, 16, 10, tail=True)
    assert tail.data == b"89abcdef" and tail.dropped_bytes == 2 and tail.truncated_bytes == 6
    head = buf.read_span(0, 16, 10)
    assert head.data == b"89abcdef" and head.dropped_bytes == 8 and head.truncated_bytes == 0
    for chunk in (tail, head):
        assert chunk.truncated_bytes + chunk.dropped_bytes + len(chunk.data) == 16


def test_a_head_read_that_eviction_pushed_past_its_span_cuts_nothing() -> None:
    """A running command's span ends where the output was when it was asked for.

    If more arrives and evicts the head before the read, the read starts later and runs past
    that end. What it returned beyond the span was not cut, so it must not count as negative.
    """
    buf = RingBuffer(8)
    buf.append(b"0123456789")
    chunk = buf.read_span(0, 6, 6)
    assert chunk.data == b"234567" and chunk.dropped_bytes == 2 and chunk.truncated_bytes == 0


def test_a_cursor_read_has_no_span_to_cut() -> None:
    buf = RingBuffer(8)
    buf.append(b"abcdef")
    assert buf.read(0, 2).truncated_bytes == 0


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
