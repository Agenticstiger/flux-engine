# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``column-provenance.json``: the record ``apply`` writes and ``ship`` reads.

Pure functions over coverage rows, so each case builds the rows it needs and
nothing else. The same record through a real run is in
``tests/stages/test_coverage_pipeline.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from flux_engine.api.lineage import DatasetRef
from flux_engine.api.verdict import Reproducibility
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.evidence.provenance import (
    PROVENANCE_DIGEST_KEY,
    PROVENANCE_FORMAT,
    build_provenance_document,
    parse_provenance,
    provenance_digest,
    quoted_name,
    render_provenance_section,
    render_uncovered_columns,
)
from flux_engine.evidence.report import render_evidence_report
from flux_engine.seam.coverage import (
    SOURCE_TIME_BOUNDS,
    ColumnCoverage,
    ExposeCoverage,
    Origin,
)
from flux_engine.stages.plan import (
    render_document as _text,
)  # the way plan and apply write a record

pytestmark = pytest.mark.unit

DATASET = DatasetRef(namespace="kafka://broker", name="payments")

COVERAGE = ExposeCoverage(
    "payment_recovery_moment",
    (
        ColumnCoverage("id", "STRING", True, Origin.COPIED, "subject.id", "alias"),
        ColumnCoverage(
            "amount", "DECIMAL(18,4)", False, Origin.COPIED, "event.data.amount", "name"
        ),
        ColumnCoverage(
            "tier", "STRING", False, Origin.DECLARED, "fidelity.columns.tier", "fidelity"
        ),
        ColumnCoverage(
            "opened", "TIMESTAMP", False, Origin.DEFAULT, SOURCE_TIME_BOUNDS, "time-bounds"
        ),
        ColumnCoverage("note", "STRING", False, Origin.DEFAULT, None, "none"),
    ),
)


def _document() -> Dict[str, Any]:
    return build_provenance_document(simulation_id="sim-1", exposes=[(COVERAGE, DATASET)])


def test_the_record_names_its_format_its_simulation_and_each_expose_with_its_dataset() -> None:
    document = _document()
    assert document["format"] == PROVENANCE_FORMAT == "flux-column-provenance/1"
    assert document["simulationId"] == "sim-1"
    (entry,) = document["exposes"]
    assert entry["exposeId"] == "payment_recovery_moment"
    assert entry["dataset"] == {"namespace": "kafka://broker", "name": "payments"}
    assert entry["counts"] == {"declared": 1, "profile": 0, "copied": 2, "derived": 0, "default": 2}


def test_a_copied_column_has_its_source_attribute_as_the_input_field_and_no_other_column_has_one() -> (
    None
):
    (entry,) = _document()["exposes"]
    fields = entry["facets"]["columnLineage"]["fields"]
    assert fields["id"]["inputFields"][0]["namespace"] == "flux://sim-1"
    assert (fields["id"]["inputFields"][0]["name"], fields["id"]["inputFields"][0]["field"]) == (
        "subject",
        "id",
    )
    assert (
        fields["amount"]["inputFields"][0]["name"],
        fields["amount"]["inputFields"][0]["field"],
    ) == ("event", "data.amount")
    assert fields["tier"]["inputFields"] == []
    assert fields["opened"]["inputFields"] == []
    assert fields["note"]["inputFields"] == []
    assert list(fields) == ["id", "amount", "tier", "opened", "note"], "declared column order"


def test_a_copy_a_rule_decides_names_no_made_up_input_dataset() -> None:
    rule_copies = ExposeCoverage(
        "invoice",
        (
            ColumnCoverage(
                "region",
                "STRING",
                False,
                Origin.COPIED,
                "semantics.relationships.account.sharedColumns.region",
                "rule",
            ),
            ColumnCoverage(
                "a_score", "INT", False, Origin.COPIED, "contract.sharedAttribute.score", "rule"
            ),
            ColumnCoverage(
                "mirrored", "INT", False, Origin.COPIED, "contract.invariants.mirror", "rule"
            ),
            ColumnCoverage("id", "STRING", True, Origin.COPIED, "subject.id", "alias"),
        ),
    )
    document = build_provenance_document(simulation_id="sim-1", exposes=[(rule_copies, DATASET)])
    fields = document["exposes"][0]["facets"]["columnLineage"]["fields"]
    assert fields["region"]["inputFields"] == []
    assert fields["a_score"]["inputFields"] == []
    assert fields["mirrored"]["inputFields"] == []
    assert fields["id"]["inputFields"][0]["name"] == "subject"


def test_the_record_is_a_function_of_the_declarations_and_nothing_else() -> None:
    assert json.dumps(_document(), sort_keys=True) == json.dumps(_document(), sort_keys=True)


def test_a_record_survives_being_rendered_and_parsed() -> None:
    assert parse_provenance(_text(_document())) == (COVERAGE,)


def test_the_rendered_record_is_indented_sorted_and_ends_in_one_newline() -> None:
    text = _text(_document())
    assert text == json.dumps(_document(), indent=2, sort_keys=True) + "\n"
    assert text.endswith("}\n") and not text.endswith("\n\n")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"format": "something-else", "exposes": []}', id="another-format"),
        pytest.param(
            f'{{"format": "{PROVENANCE_FORMAT}", "exposes": "no"}}', id="exposes-a-string"
        ),
        pytest.param(
            f'{{"format": "{PROVENANCE_FORMAT}", "exposes": [{{"exposeId": "e", "columns": [1]}}]}}',
            id="a-row-that-is-not-a-row",
        ),
    ],
)
def test_a_text_this_module_did_not_write_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_provenance(text)


def test_the_digest_is_over_the_exact_bytes_so_any_change_to_the_file_moves_it() -> None:
    """Not the parsed record: reformatting, a reordered key or a duplicated key all count."""
    text = _text(_document())
    digest = provenance_digest(text.encode("utf-8"))
    assert digest.value.startswith("sha256:")
    assert digest == provenance_digest(text.encode("utf-8")), "a pure function of the bytes"

    document = json.loads(text)
    variants = {
        "compact": json.dumps(document, separators=(",", ":")),
        "no trailing newline": text.rstrip("\n"),
        "crlf": text.replace("\n", "\r\n"),
        "keys in another order": json.dumps(document, indent=2),
        "a duplicated key": text.replace('"format":', '"format": "x", "format":', 1),
        "a value moved": text.replace('"declared": 1', '"declared": 2', 1),
    }
    for name, changed in variants.items():
        assert changed != text, f"{name} changed nothing"
        assert provenance_digest(changed.encode("utf-8")) != digest, name


def test_the_digest_is_named_for_the_key_the_plan_records_it_under() -> None:
    assert PROVENANCE_DIGEST_KEY == "columnProvenanceDigest"
    digest = provenance_digest(_text(_document()).encode("utf-8"))
    assert digest.subject == PROVENANCE_DIGEST_KEY
    assert digest.domain.value == "raw-bytes", "KERNEL-SPEC section 2.3: file bytes as on disk"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[" * 200_000, id="nested-past-what-the-parser-reads"),
        pytest.param(b'{"a": NaN}', id="a-value-no-canonical-form-has"),
        pytest.param(b"\xff\xfe\x00", id="not-utf-8"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_damaged_file_still_has_a_digest_so_it_is_a_mismatch_and_never_a_crash(
    data: bytes,
) -> None:
    """Nothing is parsed to decide whether a file is the one the plan vouches for."""
    assert provenance_digest(data) != provenance_digest(_text(_document()).encode())


def test_the_uncovered_sentence_lists_only_the_exposes_that_have_one() -> None:
    covered = ExposeCoverage("covered", (COVERAGE.columns[0],))
    assert render_uncovered_columns([covered, COVERAGE]) == (
        "These columns fell back to a type default, so no declaration or source describes "
        "their values: `payment_recovery_moment`: `opened`, `note`."
    )
    assert render_uncovered_columns([covered]).startswith("No column fell back")
    assert render_uncovered_columns(None).startswith("Column provenance is not bound to this run")


def test_the_section_says_nothing_is_bound_rather_than_nothing_is_wrong() -> None:
    assert "No column provenance is bound to this run" in render_provenance_section(None)
    assert render_provenance_section([]) == "This run declared no exposes."


def _without_the_note(section: str) -> str:
    """*section* minus the sentence about what the record is, said once at the top."""
    note, _, rest = section.partition("\n\n")
    assert note.startswith("This is what `apply` recorded for the run.")
    assert "The run's plan holds a digest of it" in note
    assert "`ship` checked the record against that digest" in note
    assert "does not work out again how each column was produced" in note
    return rest


def test_the_section_says_once_that_it_is_what_apply_recorded_and_not_re_derived() -> None:
    section = render_provenance_section([COVERAGE, ExposeCoverage("second", ())])
    assert section.count("This is what `apply` recorded") == 1


def test_the_section_lists_each_column_with_its_type_and_how_it_was_produced() -> None:
    assert _without_the_note(render_provenance_section([COVERAGE])) == "\n".join(
        [
            "### `payment_recovery_moment`",
            "",
            "5 columns: 1 declared, 2 copied, 2 default.",
            "",
            "- `id` (STRING): copied from `subject.id` (named by the vertical's alias table)",
            "- `amount` (DECIMAL(18,4)): copied from `event.data.amount` "
            "(an attribute with the column's own name)",
            "- `tier` (STRING): declared in `fidelity.columns.tier`",
            "- `opened` (TIMESTAMP): default: uniform within the simulation's time bounds: "
            "the column declares no draw of its own",
            "- `note` (STRING): default: null: no override, alias or attribute of the column's name",
        ]
    )


def test_an_expose_with_no_columns_has_a_heading_and_a_count_and_no_list() -> None:
    section = _without_the_note(render_provenance_section([ExposeCoverage("empty", ())]))
    assert section == "### `empty`\n\n0 columns: none."


# ---------------------------------------------------------------------------
# A name the report may not print
# ---------------------------------------------------------------------------

WITHHELD = ExposeCoverage(
    "payment_recovery_moment",
    (
        ColumnCoverage("certified_at", "TIMESTAMP", False, Origin.COPIED, "event.time", "name"),
        ColumnCoverage("tier", "STRING", False, Origin.COPIED, "subject.traits.certified", "name"),
        ColumnCoverage("proves_it", "STRING", False, Origin.DEFAULT, None, "none"),
    ),
)


def test_a_name_with_a_reserved_word_is_withheld_from_the_prose_and_the_report_still_renders() -> (
    None
):
    """A bundle must not become unshippable because one of its columns has such a name."""
    section = render_provenance_section([WITHHELD])
    assert "certified" not in section and "proves" not in section
    assert "- column 1 (TIMESTAMP): copied from `event.time` (an attribute with" in section
    assert (
        "- `tier` (STRING): copied from an attribute whose name this report does not print (an attribute with"
        in section
    )
    assert "- column 3 (STRING): default: null: no override" in section

    report = render_evidence_report(
        bundle_name="demo",
        kernel_version="0.1.0",
        run_digest=DigestRef("sha256:" + "aa" * 32, DigestDomain.JCS_DOCUMENT, "run-digest"),
        generated_at="2026-01-01T00:00:00Z",
        conformance_summary="ok",
        reproducibility=Reproducibility.NOT_CHECKED,
        coverage=[WITHHELD],
    )
    assert "a column whose name this report does not print" in report


def test_the_record_keeps_the_real_name_the_prose_withholds() -> None:
    document = build_provenance_document(
        simulation_id="sim-1", exposes=[(WITHHELD, DatasetRef("flux", "payments"))]
    )
    (entry,) = document["exposes"]
    assert [row["column"] for row in entry["columns"]] == ["certified_at", "tier", "proves_it"]
    assert "certified_at" in entry["facets"]["columnLineage"]["fields"]


def test_an_ordinary_name_is_quoted_and_a_withheld_one_is_replaced() -> None:
    assert quoted_name("amount", "x") == "`amount`"
    assert quoted_name("certified_amount", "x") == "x"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a\n## What Was Not Proven\nfake", id="a-name-that-starts-a-heading"),
        pytest.param("tab\there", id="a-tab"),
        pytest.param("bell\x07", id="a-control-character"),
        pytest.param("tick`s", id="a-backtick"),
    ],
)
def test_a_name_that_is_not_plain_text_cannot_forge_a_line_or_end_its_quoting(name: str) -> None:
    assert quoted_name(name, "withheld") == "withheld"
    coverage = ExposeCoverage(
        "payment_recovery_moment",
        (ColumnCoverage(name, "STRING", False, Origin.DEFAULT, None, "none"),),
    )
    section = render_provenance_section([coverage])
    assert "## What Was Not Proven" not in section
    assert (
        len(_without_the_note(section).splitlines()) == 5
    ), "one heading, a count line, one bullet and the blanks"
