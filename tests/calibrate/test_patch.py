# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The calibration patch and the text edit that applies it (ADR 0028 section 8).

A patch is RFC 6902 ``replace`` operations on declared numbers, and its digest is the digest of its text.
The edit writes the new numbers into a document's text and changes no other byte, and refuses a document it
cannot edit in one place (an alias, a merge key, a block scalar), a target that is not a number, and a
pointer that reaches nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from flux_engine.calibrate.patch import (
    METHOD_ID,
    Operation,
    locate,
    patch_digest,
    render_patch,
    splice,
)
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

DOCUMENT = """# the first comment
a:
  b: '0.5'   # kept
  c: 0.25
  d:
  - 1
  - "2"
  e: text
  'f.g/h': 3
"""


def edit(text: str, *pairs: Any) -> str:
    found = locate(text, "x.flux.yml", [(pointer, tokens) for pointer, tokens, _ in pairs])
    return splice(text, [(found[pointer], value) for pointer, _, value in pairs])


def test_an_edit_changes_the_target_and_no_other_byte() -> None:
    out = edit(DOCUMENT, ("/a/b", ("a", "b"), "0.9"), ("/a/c", ("a", "c"), "0.125"))
    assert out == DOCUMENT.replace("'0.5'", "'0.9'").replace("0.25", "0.125")
    assert "# the first comment" in out and "# kept" in out


def test_a_quoted_target_keeps_its_quote_and_a_plain_one_stays_plain() -> None:
    out = edit(DOCUMENT, ("/a/d/1", ("a", "d", "1"), "7"), ("/a/d/0", ("a", "d", "0"), "2.5"))
    assert '- "7"' in out and "- 2.5" in out


def test_a_key_with_a_dot_and_a_slash_is_reached_by_its_unescaped_token() -> None:
    out = edit("a:\n  'f.g/h': 3\n", ("/a/f.g~1h", ("a", "f.g/h"), "4"))
    assert out == "a:\n  'f.g/h': 4\n"


@pytest.mark.parametrize(
    ("pointer", "tokens", "why"),
    [
        ("/a/missing", ("a", "missing"), "no member"),
        ("/a/d/9", ("a", "d", "9"), "no member"),
        ("/a/d/01", ("a", "d", "01"), "no member"),
        ("/a", ("a",), "mapping or a list"),
        ("/a/d", ("a", "d"), "mapping or a list"),
        ("/a/e", ("a", "e"), "not a number"),
    ],
)
def test_a_target_that_is_not_one_number_or_that_does_not_exist_is_refused(
    pointer: str, tokens: Any, why: str
) -> None:
    with pytest.raises(FluxError) as error:
        locate(DOCUMENT, "x.flux.yml", [(pointer, tokens)])
    assert error.value.code.code == "FLX-3711"
    assert why in str(error.value) and "x.flux.yml" in str(error.value)


@pytest.mark.parametrize(
    "text",
    [
        "a: &n 1\nb: *n\n",
        "a: &n {x: 1}\nb: *n\n",
        "base: &base\n  x: 1\nuses:\n  <<: *base\n  y: 2\n",
        "a: 1\n---\nb: 2\n",
        "",
        "a: [unclosed\n",
    ],
)
def test_a_document_that_cannot_be_edited_in_one_place_is_refused(text: str) -> None:
    with pytest.raises(FluxError) as error:
        locate(text, "x.flux.yml", [("/a", ("a",))])
    assert error.value.code.code == "FLX-3711"


def test_a_merge_key_on_the_way_is_refused() -> None:
    text = "a:\n  <<: {x: 1}\n  y: 2\n"
    with pytest.raises(FluxError) as error:
        locate(text, "x.flux.yml", [("/a/x", ("a", "x"))])
    assert error.value.code.code == "FLX-3711"


def test_a_block_scalar_and_a_scalar_over_several_lines_are_refused() -> None:
    for text in ("a: |\n  1\n", "a: >\n  1\n", 'a: "1\n  2"\n'):
        with pytest.raises(FluxError):
            locate(text, "x.flux.yml", [("/a", ("a",))])


def test_the_edit_keeps_windows_line_ends() -> None:
    text = "a:\r\n  b: 1\r\n  c: 2\r\n"
    assert edit(text, ("/a/b", ("a", "b"), "5")) == "a:\r\n  b: 5\r\n  c: 2\r\n"


def test_several_edits_in_any_order_land_where_they_were_found() -> None:
    text = "a: 1\nb: 2\nc: 3\n"
    out = edit(text, ("/c", ("c",), "30"), ("/a", ("a",), "10"), ("/b", ("b",), "20"))
    assert out == "a: 10\nb: 20\nc: 30\n"


def test_an_operation_is_a_replace_with_four_members() -> None:
    assert Operation("s.flux.yml", "/a/b", "0.5").as_document() == {
        "document": "s.flux.yml",
        "op": "replace",
        "path": "/a/b",
        "value": "0.5",
    }


def test_a_patch_renders_with_sorted_keys_and_digests_its_own_text() -> None:
    document = {"method": METHOD_ID, "operations": [], "a": 1}
    text = render_patch(document)
    assert text.endswith("\n") and json.loads(text) == document
    assert text.index('"a"') < text.index('"method"')
    digest = patch_digest(text)
    assert digest.startswith("sha256:") and len(digest) == 71
    assert patch_digest(text) == digest != patch_digest(text + " ")
