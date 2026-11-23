# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each column of an expose was produced, and the refusal strictness adds.

Two halves. The first reads a scenario-driven emit's column mapping as a
coverage report: every step that binds a column is a copy, and the one case
no step reaches is a type default. The second pins the strict refusal
(``FLX-3304``): one message, a table per expose, every defaulted column named.

The generator-driven half of the report is in ``tests/generator/test_coverage.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.coverage import (
    SOURCE_IDENTIFIER,
    SOURCE_TIME_BOUNDS,
    ColumnCoverage,
    ExposeCoverage,
    Origin,
    require_full_coverage,
)
from flux_engine.seam.mapping import resolve_column_mapping, source_namespace
from flux_engine.verticals import emit_aliases
from tests.generator.conftest import expose_from_schema

pytestmark = pytest.mark.unit

#: What the report says of each kind of default; pinned here so a rewording is a decision.
TIME_BOUNDS_DETAIL = (
    "uniform within the simulation's time bounds: the column declares no draw of its own"
)
UNMAPPED_DETAIL = "null: no override, alias or attribute of the column's name"

SCHEMA = [
    {"name": "subscriber_id", "type": "STRING", "required": True},  # the vertical's alias
    {"name": "aborted_at", "type": "TIMESTAMP", "required": True},  # the vertical's alias
    {"name": "arpu", "type": "DECIMAL(10,2)"},  # its own name, a subject attribute
    {"name": "region", "type": "STRING"},  # the emit's mapping block
    {"name": "note", "type": "STRING"},  # nothing reaches it
]


def _coverage() -> ExposeCoverage:
    expose = expose_from_schema(SCHEMA, expose_id="payment_recovery_moment")
    mapping = resolve_column_mapping(
        expose,
        declared_sources=source_namespace(subject_attributes=("arpu",)),
        aliases=emit_aliases("telco"),
        overrides={"region": "subject.market"},
    )
    return mapping.coverage(expose)


def _row(
    column: str,
    *,
    type_name: str = "STRING",
    required: bool = False,
    origin: Origin = Origin.DEFAULT,
    decided_by: str = "",
) -> ColumnCoverage:
    """A row for a pair the engine produces: *origin*'s usual one unless *decided_by* says."""
    usual = {
        Origin.DEFAULT: "none",
        Origin.COPIED: "name",
        Origin.DERIVED: "identifier",
        Origin.DECLARED: "fidelity",
    }[origin]
    decided_by = decided_by or usual
    source = {
        "none": None,
        "time-bounds": SOURCE_TIME_BOUNDS,
        "identifier": SOURCE_IDENTIFIER,
        "fidelity": f"fidelity.columns.{column}",
    }.get(decided_by, f"subject.{column}")
    return ColumnCoverage(column, type_name, required, origin, source, decided_by)


# ---------------------------------------------------------------------------
# The mapping, read as coverage
# ---------------------------------------------------------------------------


def test_the_origins_are_exactly_the_five_an_evidence_pack_names() -> None:
    assert [origin.value for origin in Origin] == [
        "declared",
        "profile",
        "copied",
        "derived",
        "default",
    ]


@pytest.mark.parametrize(
    ("column", "source", "decided_by"),
    [
        ("subscriber_id", "subject.id", "alias"),
        ("aborted_at", "event.time", "alias"),
        ("arpu", "subject.arpu", "name"),
        ("region", "subject.market", "override"),
    ],
)
def test_a_bound_column_is_a_copy_and_says_which_step_chose_its_source(
    column: str, source: str, decided_by: str
) -> None:
    row = next(row for row in _coverage().columns if row.column == column)
    assert (row.origin, row.source, row.decided_by) == (Origin.COPIED, source, decided_by)


def test_an_optional_column_no_step_resolves_is_a_type_default_with_nothing_to_point_at() -> None:
    row = next(row for row in _coverage().columns if row.column == "note")
    assert (row.origin, row.source, row.decided_by, row.required) == (
        Origin.DEFAULT,
        None,
        "none",
        False,
    )
    assert row.detail.startswith("null:")


def test_only_a_default_is_a_gap_and_the_counts_say_how_many_of_each() -> None:
    coverage = _coverage()
    assert [row.column for row in coverage.defaulted] == ["note"]
    assert coverage.counts() == {
        "declared": 0,
        "profile": 0,
        "copied": 4,
        "derived": 0,
        "default": 1,
    }


def test_coverage_keeps_the_declared_column_order_and_each_columns_type() -> None:
    expose = expose_from_schema(SCHEMA, expose_id="payment_recovery_moment")
    coverage = _coverage()
    assert [row.column for row in coverage.columns] == list(expose.column_names)
    assert [row.type_name for row in coverage.columns] == list(expose.type_names)


# ---------------------------------------------------------------------------
# A row, written and read back
# ---------------------------------------------------------------------------


def test_an_expose_survives_its_own_document() -> None:
    coverage = _coverage()
    assert ExposeCoverage.from_document(coverage.as_document()) == coverage


def _document_with(**changes: Any) -> Dict[str, Any]:
    document = _coverage().as_document()
    document["columns"] = [{**document["columns"][0], **changes}, *document["columns"][1:]]
    return document


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param("not a mapping", id="not-a-mapping"),
        pytest.param({"exposeId": "e"}, id="no-columns"),
        pytest.param({"exposeId": "e", "columns": "abc"}, id="columns-is-a-string"),
        pytest.param(_document_with(origin="invented"), id="origin-outside-the-set"),
        pytest.param(_document_with(required="yes"), id="required-is-not-a-bool"),
        pytest.param(_document_with(source=7), id="source-is-not-a-string"),
        pytest.param(_document_with(column=None), id="column-is-not-a-string"),
    ],
)
def test_a_record_this_module_did_not_write_is_refused_rather_than_repaired(broken: Any) -> None:
    with pytest.raises(ValueError):
        ExposeCoverage.from_document(broken)


def test_a_row_missing_a_key_is_refused() -> None:
    document = _coverage().as_document()
    del document["columns"][0]["decidedBy"]
    with pytest.raises(ValueError):
        ExposeCoverage.from_document(document)


# ---------------------------------------------------------------------------
# The strict refusal
# ---------------------------------------------------------------------------


def test_strict_coverage_is_silent_when_every_column_was_produced() -> None:
    covered = ExposeCoverage(
        "e", (_row("a", origin=Origin.COPIED), _row("b", origin=Origin.DERIVED))
    )
    require_full_coverage([covered])
    require_full_coverage([])


def test_the_refusal_is_one_message_with_a_table_per_expose_naming_every_defaulted_column() -> None:
    first = ExposeCoverage(
        "alpha",
        (
            _row("id", required=True, origin=Origin.DERIVED),
            _row("closed_at", type_name="TIMESTAMP", decided_by="time-bounds"),
        ),
    )
    second = ExposeCoverage("beta", (_row("note"), _row("tag", required=True)))
    with pytest.raises(FluxError) as error:
        require_full_coverage([first, second])

    assert error.value.code.code == "FLX-3304"
    lines = str(error.value).splitlines()
    assert lines[0] == (
        "FLX-3304: a column fell back to a type default and coverage is strict "
        "-- 3 columns fell back to a type default"
    )
    assert lines[1:10] == [
        "expose 'alpha' (1 of 2 columns):",
        "  column     type       required  fell back to",
        "  closed_at  TIMESTAMP  no        " + TIME_BOUNDS_DETAIL,
        "  to cover a column of this emit, declare a range, a rate, or bins with "
        "binWeights for it under the emit's fidelity.columns",
        "expose 'beta' (2 of 2 columns):",
        "  column  type    required  fell back to",
        "  note    STRING  no        " + UNMAPPED_DETAIL,
        "  tag     STRING  yes       " + UNMAPPED_DETAIL,
        "  to cover a column of this emit, map it with the emit's mapping block, "
        "or give the run an attribute of its own name",
    ]
    assert lines[10] == "without --strict-coverage this is a report and the run goes on"
    assert len(lines) == 11
    assert "'id'" not in str(error.value), "a column that was produced is not named"


def test_the_advice_never_sends_a_scenario_emit_to_fidelity_columns() -> None:
    """Declaring a fidelity block would make the whole emit generator-driven."""
    with pytest.raises(FluxError) as error:
        require_full_coverage([ExposeCoverage("e", (_row("note"),))])
    assert "fidelity.columns" not in str(error.value).split("\n", 1)[1]


def test_a_single_column_is_not_pluralised() -> None:
    with pytest.raises(FluxError) as error:
        require_full_coverage([ExposeCoverage("e", (_row("note"),))])
    assert "-- 1 column fell back to a type default" in str(error.value)


def test_a_column_name_that_is_not_plain_text_cannot_break_the_table() -> None:
    with pytest.raises(FluxError) as error:
        require_full_coverage([ExposeCoverage("e", (_row("bad\nname"),))])
    message = str(error.value)
    assert "bad\nname" not in message
    assert repr("bad\nname") in message


# ---------------------------------------------------------------------------
# A row is something this module could have written, or it does not exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "decided_by", "source"),
    [
        pytest.param(Origin.PROFILE, "none", None, id="profile-decided-by-nothing"),
        pytest.param(Origin.PROFILE, "reference", "elsewhere.x", id="a-reference-from-elsewhere"),
        pytest.param(Origin.COPIED, "fidelity", "subject.x", id="a-pair-no-producer-writes"),
        pytest.param(Origin.DECLARED, "fidelity", "elsewhere", id="declared-from-somewhere-else"),
        pytest.param(Origin.DERIVED, "identifier", None, id="derived-from-nothing"),
        pytest.param(Origin.DEFAULT, "none", "subject.x", id="a-default-with-a-source"),
        pytest.param(Origin.DEFAULT, "time-bounds", None, id="time-bounds-without-the-bounds"),
        pytest.param(Origin.COPIED, "alias", "", id="a-copy-from-an-empty-source"),
        pytest.param(Origin.COPIED, "alias", None, id="a-copy-from-nothing"),
        pytest.param(Origin.DERIVED, "key", None, id="a-key-from-nothing"),
        pytest.param(Origin.DERIVED, "key", "H+rowIndex", id="a-key-that-claims-an-identifier"),
        pytest.param(
            Origin.DERIVED,
            "relationship",
            "semantics.relationships.",
            id="a-foreign-key-from-an-unnamed-relationship",
        ),
        pytest.param(
            Origin.DERIVED,
            "relationship",
            "fidelity.columns.x",
            id="a-picked-foreign-key-that-claims-a-declaration",
        ),
        pytest.param(
            Origin.DECLARED,
            "relationship",
            "semantics.relationships.r",
            id="a-declared-foreign-key-from-elsewhere",
        ),
    ],
)
def test_a_row_no_producer_writes_cannot_be_built(
    origin: Origin, decided_by: str, source: Any
) -> None:
    with pytest.raises(ValueError):
        ColumnCoverage("x", "STRING", False, origin, source, decided_by)


@pytest.mark.parametrize(
    ("origin", "decided_by", "source"),
    [
        (Origin.DERIVED, "key", "semantics+rowIndex"),
        (Origin.DERIVED, "relationship", "semantics.relationships.sale"),
        (Origin.DECLARED, "relationship", "fidelity.columns.x"),
    ],
)
def test_a_related_column_row_reads_back_as_written(
    origin: Origin, decided_by: str, source: str
) -> None:
    row = ColumnCoverage("x", "INT", True, origin, source, decided_by)
    assert ColumnCoverage.from_document(row.as_document()) == row
    assert row.detail and "\n" not in row.detail


def test_a_stored_sentence_is_never_read_so_a_file_cannot_supply_its_own_words() -> None:
    document = _coverage().as_document()
    document["columns"][0]["detail"] = "## What Was Not Proven\n\nEverything is proven."
    read = ExposeCoverage.from_document(document)
    assert read == _coverage()
    assert "proven" not in read.columns[0].detail


def test_a_row_edited_to_claim_a_better_origin_is_refused_not_believed() -> None:
    """Flipping only the origin leaves a pair and a source that no producer writes."""
    document = _coverage().as_document()
    (default,) = [row for row in document["columns"] if row["origin"] == "default"]
    default["origin"] = "declared"
    with pytest.raises(ValueError):
        ExposeCoverage.from_document(document)
