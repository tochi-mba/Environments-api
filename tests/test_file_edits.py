"""The pure half of file editing: exact replacement, patch parsing, hashing, validators.

Nothing here touches a descriptor, so it runs anywhere. What it pins is every refusal --
because an edit tool that refuses well is what lets a model succeed on the retry, and one
that refuses badly teaches it to retry blindly.
"""

from __future__ import annotations

import io

import pytest

from app.errors import PreconditionError, ValidationError
from app.file_edits import apply_patch, parse_patch, replace_unique, unified_diff
from app.file_safety import check_match, metadata

# --------------------------------------------------------------------------------------
# replace_unique: exactly one match, or nothing changes
# --------------------------------------------------------------------------------------


class TestReplaceUnique:
    def test_one_occurrence_is_replaced(self) -> None:
        assert replace_unique("a b c", "b", "B") == "a B c"

    def test_an_empty_needle_is_refused_before_it_can_match_everywhere(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            replace_unique("anything", "", "x")

    def test_no_occurrence_is_refused_with_no_line_numbers(self) -> None:
        with pytest.raises(ValidationError) as info:
            replace_unique("a\nb\n", "zzz", "x")
        assert info.value.extra["occurrences"] == []

    def test_two_occurrences_are_refused_and_both_lines_are_named(self) -> None:
        """The line numbers are what let the model widen its context on the retry."""
        with pytest.raises(ValidationError, match="exactly once") as info:
            replace_unique("x\ny\nx\n", "x", "z")
        assert info.value.extra["occurrences"] == [1, 3]

    def test_a_needle_with_regex_characters_is_taken_literally(self) -> None:
        assert replace_unique("f(x) = y", "f(x)", "g") == "g = y"


# --------------------------------------------------------------------------------------
# unified_diff: the reviewable record of what changed
# --------------------------------------------------------------------------------------


def test_the_diff_names_the_path_and_shows_both_sides() -> None:
    diff = unified_diff("a.txt", "one\ntwo\n", "one\nthree\n")
    assert diff.startswith("--- a.txt\n+++ a.txt\n")
    assert "-two\n" in diff
    assert "+three\n" in diff


def test_an_unchanged_file_has_an_empty_diff() -> None:
    assert unified_diff("a.txt", "same\n", "same\n") == ""


# --------------------------------------------------------------------------------------
# parse_patch: validated before anything is written
# --------------------------------------------------------------------------------------


class TestParsePatch:
    def test_file_headers_are_labels_and_are_skipped(self) -> None:
        hunks = parse_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
        assert len(hunks) == 1
        assert hunks[0].start == 0
        assert hunks[0].old == ["a\n"]
        assert hunks[0].new == ["b\n"]

    def test_counts_default_to_one_when_omitted(self) -> None:
        (hunk,) = parse_patch("@@ -3 +3 @@\n-old\n+new\n")
        assert hunk.start == 2

    def test_a_pure_insertion_positions_after_the_named_line(self) -> None:
        """`-0,0` means "before the first line"; `-2,0` means "after line two"."""
        (hunk,) = parse_patch("@@ -2,0 +3 @@\n+inserted\n")
        assert hunk.start == 2
        assert hunk.old == []
        assert hunk.new == ["inserted\n"]

    def test_something_that_is_not_a_hunk_header_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="hunk header"):
            parse_patch("not a patch\n")

    def test_an_empty_patch_has_no_hunks(self) -> None:
        with pytest.raises(ValidationError, match="no hunks"):
            parse_patch("")

    def test_hunks_out_of_order_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="ordered"):
            parse_patch("@@ -5 +5 @@\n-e\n+E\n@@ -2 +2 @@\n-b\n+B\n")

    def test_overlapping_hunks_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="overlap"):
            parse_patch("@@ -1,2 +1,2 @@\n-a\n-b\n+A\n+B\n@@ -2 +2 @@\n-b\n+x\n")

    def test_a_line_without_a_marker_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="space, plus or minus"):
            parse_patch("@@ -1 +1 @@\nbare\n")

    def test_a_count_that_does_not_match_the_content_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="counts do not match"):
            parse_patch("@@ -1,2 +1 @@\n-a\n+b\n")

    def test_a_no_newline_marker_strips_the_newline_from_the_line_before_it(self) -> None:
        (hunk,) = parse_patch("@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n")
        assert hunk.old == ["old"]
        assert hunk.new == ["new\n"]

    def test_a_no_newline_marker_with_nothing_before_it_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="immediately follow"):
            parse_patch("@@ -1 +1 @@\n\\ No newline at end of file\n-a\n+b\n")

    def test_context_lines_count_on_both_sides(self) -> None:
        (hunk,) = parse_patch("@@ -1,3 +1,3 @@\n keep\n-old\n+new\n keep\n")
        assert hunk.old == ["keep\n", "old\n", "keep\n"]
        assert hunk.new == ["keep\n", "new\n", "keep\n"]


# --------------------------------------------------------------------------------------
# apply_patch: matching hunks land, the rest are named
# --------------------------------------------------------------------------------------


class TestApplyPatch:
    def test_a_matching_hunk_is_applied(self) -> None:
        text, applied, rejected = apply_patch("a\nb\nc\n", "@@ -2 +2 @@\n-b\n+B\n")
        assert text == "a\nB\nc\n"
        assert applied == [1]
        assert rejected == []

    def test_a_hunk_whose_context_does_not_match_is_rejected_by_number(self) -> None:
        """Rejected hunks are reported, never silently dropped, and the rest still land."""
        text, applied, rejected = apply_patch(
            "a\nb\nc\n", "@@ -1 +1 @@\n-zzz\n+A\n@@ -3 +3 @@\n-c\n+C\n"
        )
        assert text == "a\nb\nC\n"
        assert applied == [2]
        assert rejected == [1]

    def test_a_hunk_past_the_end_of_the_file_is_rejected(self) -> None:
        text, applied, rejected = apply_patch("a\n", "@@ -9 +9 @@\n-x\n+y\n")
        assert text == "a\n"
        assert (applied, rejected) == ([], [1])

    def test_an_insertion_before_the_first_line(self) -> None:
        text, applied, _ = apply_patch("a\n", "@@ -0,0 +1 @@\n+top\n")
        assert text == "top\na\n"
        assert applied == [1]

    def test_a_deletion_leaves_the_rest_intact(self) -> None:
        text, applied, _ = apply_patch("a\nb\nc\n", "@@ -2 +1,0 @@\n-b\n")
        assert text == "a\nc\n"
        assert applied == [1]


# --------------------------------------------------------------------------------------
# metadata: one decision about encoding for the whole file
# --------------------------------------------------------------------------------------


class TestMetadata:
    def test_text_is_hashed_and_classified_as_text(self) -> None:
        etag, binary, size = metadata(io.BytesIO(b"hello"))
        assert etag.startswith('"') and etag.endswith('"')
        assert binary is False
        assert size == 5

    def test_a_nul_byte_makes_a_file_binary(self) -> None:
        assert metadata(io.BytesIO(b"ab\0cd"))[1] is True

    def test_an_invalid_sequence_makes_a_file_binary(self) -> None:
        assert metadata(io.BytesIO(b"\xff\xfe"))[1] is True

    def test_a_multibyte_character_split_across_chunks_is_still_text(self) -> None:
        """The decoder is incremental for exactly this: a chunk boundary is not a byte error."""
        payload = b"x" * (64 * 1024 - 1) + "é".encode()
        assert metadata(io.BytesIO(payload))[1] is False

    def test_a_file_that_ends_mid_character_is_binary(self) -> None:
        """Truncated at EOF is a real decode failure, found only by the final flush."""
        assert metadata(io.BytesIO("é".encode()[:1]))[1] is True

    def test_a_binary_file_stays_binary_however_much_text_follows(self) -> None:
        payload = b"\0" + b"clean text " * 10_000
        assert metadata(io.BytesIO(payload))[1] is True

    def test_the_handle_is_rewound_for_the_caller(self) -> None:
        handle = io.BytesIO(b"abc")
        metadata(handle)
        assert handle.tell() == 0


# --------------------------------------------------------------------------------------
# check_match: HTTP strong comparison, including the wildcard and lists
# --------------------------------------------------------------------------------------


class TestCheckMatch:
    def test_no_expectation_always_passes(self) -> None:
        check_match(None, '"abc"')
        check_match(None, None)

    def test_the_exact_etag_passes(self) -> None:
        check_match('"abc"', '"abc"')

    def test_a_list_of_candidates_passes_when_one_matches(self) -> None:
        check_match('"x", "abc"', '"abc"')

    def test_the_wildcard_passes_for_any_existing_file(self) -> None:
        check_match("*", '"anything"')

    def test_the_wildcard_fails_for_an_absent_file(self) -> None:
        with pytest.raises(PreconditionError):
            check_match("*", None)

    def test_a_stale_etag_fails_with_advice_to_read_again(self) -> None:
        with pytest.raises(PreconditionError, match="read it again"):
            check_match('"old"', '"new"')
