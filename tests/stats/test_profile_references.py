# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-3-0: the ``references`` section, read beside the earlier formats.

PROFILE-SPEC.md section 5b: a table the owner releases whole, as ``{disclosure, columns, rows}``,
every cell a string or null. The reader refuses with ``FLX-5036`` what the section does not allow,
and a document of an earlier format reads and digests exactly as it did.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_engine.core.digest import digest_of_document
from flux_stats.errors import FluxError
from flux_stats.profile import (
    SCHEMA_URL,
    SCHEMA_URL_1_1_0,
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    ReferenceTable,
    parse_profile,
)

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
BASE: Dict[str, Any] = json.loads((SPEC / "profile-1-2-example.json").read_text("utf-8"))

TABLE: Dict[str, Any] = {
    "disclosure": "none",
    "columns": ["sku", "name", "price", "retired"],
    "rows": [
        ["A-1", "Anvil", "12.5", "false"],
        ["B-2", "Bolt", "0.25", None],
        ["C-3", "Crate", "0", "true"],
    ],
}


def _document(**changes: Any) -> Dict[str, Any]:
    document = copy.deepcopy(BASE)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    document["disclosureRules"]["none"] = {}
    document["references"] = {"product": copy.deepcopy(TABLE)}
    document.update(changes)
    return document


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = _document()
    change(document["references"]["product"])
    return document


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def test_a_reference_table_is_read_whole_in_published_order() -> None:
    profile = parse_profile(_document())
    assert profile.references == {
        "product": ReferenceTable(
            disclosure="none",
            columns=("sku", "name", "price", "retired"),
            rows=(
                ("A-1", "Anvil", "12.5", "false"),
                ("B-2", "Bolt", "0.25", None),
                ("C-3", "Crate", "0", "true"),
            ),
        )
    }
    assert profile.invariants, "a 1-3-0 document keeps the sections of 1-2-0"


def test_a_table_may_hold_no_rows_and_a_profile_no_table() -> None:
    document = _edit(lambda table: table.update(rows=[]))
    assert parse_profile(document).references["product"].rows == ()
    document = _document(references={})
    assert parse_profile(document).references == {}


def test_an_earlier_format_reads_with_no_references() -> None:
    for url in (SCHEMA_URL, SCHEMA_URL_1_1_0, SCHEMA_URL_1_2_0):
        document = copy.deepcopy(BASE)
        if url != SCHEMA_URL_1_2_0:
            document = json.loads((SPEC / "profile-example.json").read_text("utf-8"))
        profile = parse_profile(document)
        assert profile.references == {}


@pytest.mark.parametrize("url", [SCHEMA_URL, SCHEMA_URL_1_1_0, SCHEMA_URL_1_2_0])
def test_an_earlier_format_that_holds_references_is_refused(url: str) -> None:
    old = json.loads((SPEC / "profile-example.json").read_text("utf-8"))
    document = _document(_schemaURL=url)
    for section in ("invariants", "privacy"):
        document.pop(section, None)
    document["tables"] = old["tables"]
    message = _refused(document)
    assert "profile.references" in message and "1-3-0" in message


def test_the_digests_of_existing_profiles_are_unchanged() -> None:
    expected = {
        "profile-example.json": "sha256:af97633046a8c9621d32fec595dd02672bc10e3fabd98c846a2ffcd713e89361",
    }
    for name, digest in expected.items():
        document = json.loads((SPEC / name).read_text("utf-8"))
        assert digest_of_document(document, subject="p").value == digest
        parse_profile(document)
    # The digest covers the document, so reading a section adds nothing to a document without it.
    plain = copy.deepcopy(BASE)
    before = digest_of_document(plain, subject="p").value
    parse_profile(plain)
    assert digest_of_document(plain, subject="p").value == before


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda t: t.update(extra=1), "profile.references.product"),
        (lambda t: t.pop("rows"), "profile.references.product"),
        (lambda t: t.update(disclosure="nothing-like-it"), "profile.references.product.disclosure"),
        (lambda t: t.update(disclosure=3), "profile.references.product.disclosure"),
        (lambda t: t.update(columns=[]), "profile.references.product.columns"),
        (lambda t: t.update(columns="sku"), "profile.references.product.columns"),
        (lambda t: t.update(columns=["a", "a", "b", "c"]), "profile.references.product.columns"),
        (lambda t: t.update(columns=["a", "", "b", "c"]), "profile.references.product.columns[1]"),
        (lambda t: t.update(rows={}), "profile.references.product.rows"),
        (lambda t: t["rows"][1].pop(), "profile.references.product.rows[1]"),
        (lambda t: t["rows"][1].append("x"), "profile.references.product.rows[1]"),
        (lambda t: t["rows"].__setitem__(0, "A-1"), "profile.references.product.rows[0]"),
        (lambda t: t["rows"][0].__setitem__(2, 12), "profile.references.product.rows[0][2]"),
        (lambda t: t["rows"][0].__setitem__(2, True), "profile.references.product.rows[0][2]"),
        (lambda t: t["rows"][0].__setitem__(1, ["Anvil"]), "profile.references.product.rows[0][1]"),
        (lambda t: t["rows"][0].__setitem__(1, "é"), "profile"),
    ],
)
def test_a_broken_table_is_refused_where_it_breaks(
    change: Callable[[Dict[str, Any]], None], where: str
) -> None:
    assert where in _refused(_edit(change))


def test_a_references_section_that_is_not_an_object_is_refused() -> None:
    assert "profile.references" in _refused(_document(references=[]))


def test_a_float_cell_is_refused_at_the_text() -> None:
    text = json.dumps(_document()).replace('"12.5"', "12.5")
    from flux_stats.profile import load_profile_text

    with pytest.raises(FluxError) as refused:
        load_profile_text(text)
    assert refused.value.code.code == "FLX-5036"


def test_the_bounds_hold_at_their_edges() -> None:
    wide = ["c%d" % index for index in range(1000)]
    at_columns = _edit(lambda t: t.update(columns=wide, rows=[[None] * 1000]))
    assert len(parse_profile(at_columns).references["product"].columns) == 1000
    over_columns = _edit(lambda t: t.update(columns=wide + ["more"], rows=[]))
    assert "columns" in _refused(over_columns)

    at_rows = _edit(lambda t: t.update(columns=["a"], rows=[["x"]] * 100_000))
    assert len(parse_profile(at_rows).references["product"].rows) == 100_000
    over_rows = _edit(lambda t: t.update(columns=["a"], rows=[["x"]] * 100_001))
    assert "rows" in _refused(over_rows)

    at_cells = _edit(lambda t: t.update(columns=wide[:10], rows=[[None] * 10] * 100_000))
    assert len(parse_profile(at_cells).references["product"].rows) == 100_000
    over_cells = _edit(lambda t: t.update(columns=wide[:11], rows=[[None] * 11] * 90_910))
    assert "cells" in _refused(over_cells)
    just_over = _edit(lambda t: t.update(columns=wide[:10], rows=[[None] * 10] * 100_001))
    assert "rows" in _refused(just_over)

    at_text = _edit(lambda t: t.update(columns=["a"], rows=[["x" * 4096]]))
    assert len(parse_profile(at_text).references["product"].rows[0][0] or "") == 4096
    over_text = _edit(lambda t: t.update(columns=["a"], rows=[["x" * 4097]]))
    assert "characters" in _refused(over_text)

    tables = {f"t{index}": copy.deepcopy(TABLE) for index in range(1024)}
    assert len(parse_profile(_document(references=tables)).references) == 1024
    tables["one more"] = copy.deepcopy(TABLE)
    assert "tables" in _refused(_document(references=tables))


def test_the_schema_of_1_3_0_agrees_with_the_reader() -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert not list(validator.iter_errors(_document()))
    assert list(validator.iter_errors(_edit(lambda t: t["rows"][0].__setitem__(2, 12))))
    assert list(validator.iter_errors(_edit(lambda t: t.update(columns=["a", "a", "b", "c"]))))
