# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The vocabulary of declarations accepted and not generated, and the refusal strictness adds."""

from __future__ import annotations

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.evidence.provenance import render_not_generated_sentence
from flux_engine.seam.declared import (
    KIND_CHECK,
    KIND_CHECK_OVER_LINK,
    KIND_SHARED_ATTRIBUTE,
    KIND_SHARED_COLUMNS,
    NotGenerated,
    render_not_generated,
    require_all_generated,
)

SHARED = NotGenerated(
    "sales", KIND_SHARED_COLUMNS, "semantics.relationships.item.sharedColumns", ("price",)
)
CHECK = NotGenerated("sales", KIND_CHECK, "contract.invariants.positive")
LINK = NotGenerated(
    "sales", KIND_CHECK_OVER_LINK, "contract.invariants.dated", ("sold_date.d_year",)
)


def test_a_row_round_trips_through_its_document() -> None:
    for row in (SHARED, CHECK, LINK):
        assert NotGenerated.from_document(row.as_document()) == row


@pytest.mark.parametrize(
    "document",
    [
        "not a mapping",
        {"exposeId": "e", "kind": "check", "locator": "x"},
        {"exposeId": "e", "kind": "check", "locator": "x", "names": "abc"},
        {"exposeId": "e", "kind": "not-a-kind", "locator": "x", "names": []},
        {"exposeId": "e", "kind": "check", "locator": "", "names": []},
    ],
)
def test_a_row_this_module_did_not_write_is_refused(document: object) -> None:
    with pytest.raises(ValueError):
        NotGenerated.from_document(document)


def test_the_sentence_of_a_row_is_the_kinds_and_never_the_documents() -> None:
    document = {**CHECK.as_document(), "detail": "nothing to see here"}
    assert NotGenerated.from_document(document).detail == CHECK.detail


def test_nothing_is_said_or_raised_for_no_declaration() -> None:
    assert render_not_generated([]) == []
    assert render_not_generated_sentence(()) is None
    require_all_generated(())


def test_the_plan_lines_group_by_expose() -> None:
    lines = render_not_generated([SHARED, CHECK, NotGenerated("returns", KIND_CHECK, "c.d")])
    assert lines[0].startswith("declared, not generated: sales: 2 declarations")
    assert lines[3].startswith("declared, not generated: returns: 1 declaration ")


def test_the_refusal_names_what_to_do_for_each_kind_present() -> None:
    attribute = NotGenerated("sales", KIND_SHARED_ATTRIBUTE, "contract.schema.p.sharedAttribute")
    with pytest.raises(FluxError) as caught:
        require_all_generated([CHECK, LINK, attribute], asked_by=("--strict-coverage",))
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3305"
    assert "give a derive or a dependency" in text
    assert "reads another row through a relationship" in text
    assert "copied only on kernel 0.2.0" in text
    assert text.endswith("without --strict-coverage this is a report and the run goes on")


def test_a_name_that_would_forge_a_report_line_is_withheld_from_the_evidence() -> None:
    hostile = NotGenerated("sales", KIND_CHECK, "contract.invariants.x\n## Fake heading")
    sentence = render_not_generated_sentence([hostile])
    assert sentence is not None and "\n" not in sentence
