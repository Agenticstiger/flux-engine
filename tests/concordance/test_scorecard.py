# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The scorecard: a tree of exact scores over the profile path's evaluations.

Built from the published vectors' block (six properties, one of each
dimension the engine scores today) and a profile of its committed slice, so
every number below is read back from the real comparison. Pinned: how a
property, a table, a dimension and the run are scored; that a censored or a
refused property is shown and left out of every mean; the thresholds and
the minCredibility reading; and the document's shape, its determinism, and
that it carries no value of the data.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.scorecard import (
    REFUSED,
    SCHEMA_URL,
    ContractShape,
    DeclaredLink,
    Scorecard,
    TableInput,
    build_scorecard,
    display,
)
from flux_engine.concordance.verifier import CENSORED, OUTSIDE, WITHIN, ClearProfile
from flux_engine.seam.scorecard import (
    DIMENSIONS,
    ScorecardDeclaration,
    Threshold,
    dimension_of,
)
from flux_stats.profile import parse_profile
from tests.stats.profiles import edited, profile_from_rows

pytestmark = pytest.mark.unit

EXPOSE = "recovery_moment"
SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA = json.loads((SPEC / "scorecard-1-2.schema.json").read_text(encoding="utf-8"))
DIGEST = "sha256:" + "a" * 64


@pytest.fixture()
def profile_document(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    return profile_from_rows(committed_rows, compiled)


def _scorecard(
    compiled: CompiledFidelity,
    document: Mapping[str, Any],
    rows: List[Mapping[str, Any]],
    *,
    declaration: ScorecardDeclaration | None = None,
    min_credibility: str | None = None,
    extra: List[TableInput] | None = None,
    contract: ContractShape | None = None,
) -> Scorecard:
    profile = parse_profile(document)
    evaluation = ClearProfile(
        compiled=compiled, profile=profile, synthetic_rows=list(rows)
    ).evaluate()
    tables = [
        TableInput(
            expose_id=EXPOSE,
            compiled=compiled,
            evaluation=evaluation,
            min_credibility=min_credibility,
            contract=contract,
        ),
        *(extra or []),
    ]
    return build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=DIGEST,
        run_digest=DIGEST,
        profile_digest=DIGEST,
        tables=tables,
        declaration=declaration,
        profile=profile,
    )


def _case(vectors: Mapping[str, Any], identifier: str) -> List[Mapping[str, Any]]:
    return next(
        case["syntheticRows"] for case in vectors["evaluations"] if case["id"] == identifier
    )


def _property(scorecard: Scorecard, property_id: str) -> Any:
    table = scorecard.table(EXPOSE)
    assert table is not None
    return next(entry for entry in table.properties if entry.property_id == property_id)


def _threshold(scope: str, name: str | None, operator: str, value: str) -> Threshold:
    return Threshold(
        scope=scope, name=name, operator=operator, value=Fraction(value), declared=value
    )


def test_each_kind_is_scored_in_its_dimension(compiled: CompiledFidelity) -> None:
    placed = {
        entry.property_id: dimension_of(
            entry.declaration.kind.value, entry.declaration.group_by is not None
        )
        for entry in compiled.properties
    }
    assert placed == {
        "000:mean:amount": "marginals",
        "001:marginalHistogram:amount": "marginals",
        "002:support:market": "structure",
        "003:mean:amount@market": "joint",
        "004:correlation:amount+attempts": "joint",
        "005:quantile:amount": "marginals",
    }
    assert dimension_of("support", True) == "structure"
    assert dimension_of("levelHistogram", True) == "joint"
    with pytest.raises(ValueError):
        dimension_of("variance", False)


def test_a_run_that_matches_its_profile_scores_one_everywhere_it_is_measured(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    scorecard = _scorecard(compiled, profile_document, committed_rows)
    assert scorecard.overall.score == 1
    assert {name: aggregate.score for name, aggregate in scorecard.dimensions} == {
        "structure": 1,
        "marginals": 1,
        "joint": 1,
        "crossTable": None,
        "temporal": None,
        "invariants": None,
        "queries": None,
        "defects": None,
    }
    assert [name for name, _ in scorecard.dimensions] == list(DIMENSIONS)
    table = scorecard.table(EXPOSE)
    assert table is not None and table.status == "measured" and table.aggregate.score == 1
    assert scorecard.met, "no threshold was declared, and nothing measured fell short"


def test_scores_are_exact_means_up_the_tree(
    vectors: Mapping[str, Any], compiled: CompiledFidelity, profile_document: Dict[str, Any]
) -> None:
    scorecard = _scorecard(compiled, profile_document, _case(vectors, "drifted-mean"))
    grouped = _property(scorecard, "003:mean:amount@market")
    assert grouped.outcome == OUTSIDE
    # A property scores the mean of its scored groups.
    assert grouped.score == (Fraction(17, 28) + Fraction(5, 7)) / 2
    assert grouped.groups_outside == (0, 1) and grouped.groups_scored == 2
    marginals = [
        _property(scorecard, identifier).score
        for identifier in ("000:mean:amount", "001:marginalHistogram:amount", "005:quantile:amount")
    ]
    dimensions = dict(scorecard.dimensions)
    assert dimensions["marginals"].score == sum(marginals, Fraction(0)) / 3
    # The run averages its measured dimensions, each weighted equally.
    measured = [aggregate.score for _, aggregate in scorecard.dimensions if aggregate.score]
    assert scorecard.overall.score == sum(measured, Fraction(0)) / len(measured)
    # The table averages its properties, each weighted equally.
    table = scorecard.table(EXPOSE)
    assert table is not None
    scores = [entry.score for entry in table.properties]
    assert table.aggregate.score == sum(scores, Fraction(0)) / len(scores)


def test_a_censored_property_is_shown_and_left_out_of_every_mean(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    support = document["tables"][EXPOSE]["columns"]["market"]["statistics"][0]
    support["groups"] = [{"group": group["group"], "status": "A"} for group in support["groups"]]
    scorecard = _scorecard(compiled, document, committed_rows)
    entry = _property(scorecard, "002:support:market")
    assert entry.outcome == CENSORED and entry.score is None
    structure = dict(scorecard.dimensions)["structure"]
    assert structure.score is None and structure.status == "not measured"
    assert (structure.properties, structure.censored) == (1, 1)
    assert scorecard.overall.score == 1, "the censored property is not scored as zero"
    assert scorecard.overall.censored == 1
    table = scorecard.table(EXPOSE)
    assert table is not None
    assert len(table.properties) == 6 and table.aggregate.scored == 5


def test_a_refused_property_is_shown_with_its_code_and_left_out(
    vectors: Mapping[str, Any], compiled: CompiledFidelity, profile_document: Dict[str, Any]
) -> None:
    scorecard = _scorecard(compiled, profile_document, _case(vectors, "unseen-category"))
    entry = _property(scorecard, "003:mean:amount@market")
    assert entry.outcome == REFUSED and entry.score is None
    assert [refusal.code for refusal in entry.refusals] == ["FLX-5005"]
    assert dict(scorecard.dimensions)["joint"].refused == 1
    assert _property(scorecard, "002:support:market").score == Fraction(7, 8)


def test_a_table_the_profile_cannot_answer_refuses_every_property(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    document["tables"] = {"another_expose": document["tables"].pop(EXPOSE)}
    scorecard = _scorecard(compiled, document, committed_rows)
    table = scorecard.table(EXPOSE)
    assert table is not None and table.status == REFUSED
    assert [refusal.code for refusal in table.refusals] == ["FLX-5037"]
    assert {entry.outcome for entry in table.properties} == {REFUSED}
    assert scorecard.overall.score is None and scorecard.overall.status == "not measured"


def test_a_table_with_no_fidelity_block_is_listed_and_not_measured(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    scorecard = _scorecard(
        compiled, profile_document, committed_rows, extra=[TableInput(expose_id="another")]
    )
    other = scorecard.table("another")
    assert other is not None and not other.declares_fidelity
    assert other.status == "not measured" and other.properties == ()
    assert [table.expose_id for table in scorecard.tables] == ["another", EXPOSE]


def test_thresholds_read_the_score_they_name(
    vectors: Mapping[str, Any], compiled: CompiledFidelity, profile_document: Dict[str, Any]
) -> None:
    declaration = ScorecardDeclaration(
        thresholds=(
            _threshold("overall", None, "mustBeGreaterOrEqualTo", "0.5"),
            _threshold("dimension", "marginals", "mustBeGreaterThan", "0.9"),
            _threshold("table", EXPOSE, "mustBeGreaterOrEqualTo", "0"),
            _threshold("dimension", "temporal", "mustBeGreaterOrEqualTo", "0"),
        )
    )
    scorecard = _scorecard(
        compiled, profile_document, _case(vectors, "drifted-mean"), declaration=declaration
    )
    met = [result.met for result in scorecard.thresholds]
    assert met == [True, False, True, False], "an unmeasured score never meets a threshold"
    assert not scorecard.met
    lines = scorecard.failure_lines()
    assert lines[0].startswith("threshold dimensions.marginals mustBeGreaterThan 0.9: not met")
    assert "lowest property 005:quantile:amount (quantile of amount)" in lines[0]
    assert lines[1].endswith("no property under it was scored")
    assert scorecard.contributors(declaration.thresholds[1]) == (EXPOSE,)


def test_a_table_threshold_on_no_table_names_every_declaring_table(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    stray = _threshold("table", "renamed", "mustBeGreaterOrEqualTo", "0.5")
    scorecard = _scorecard(
        compiled,
        profile_document,
        committed_rows,
        declaration=ScorecardDeclaration(thresholds=(stray,)),
        extra=[TableInput(expose_id="plain")],
    )
    (result,) = scorecard.thresholds
    assert result.score is None and not result.met
    assert scorecard.contributors(stray) == (EXPOSE,), "fails closed, never withholds nothing"


def test_a_missed_dimension_withholds_only_the_tables_whose_scores_fed_it(
    vectors: Mapping[str, Any],
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    withheld = edited(profile_document)
    for statistic in withheld["tables"][EXPOSE]["columns"]["amount"]["statistics"]:
        statistic["groups"] = [
            {"group": group["group"], "status": "A"} for group in statistic["groups"]
        ]
    censored = ClearProfile(
        compiled=compiled, profile=parse_profile(withheld), synthetic_rows=committed_rows
    ).evaluate()
    floor = _threshold("dimension", "marginals", "mustBeGreaterOrEqualTo", "0.99")
    scorecard = _scorecard(
        compiled,
        profile_document,
        _case(vectors, "drifted-mean"),
        declaration=ScorecardDeclaration(thresholds=(floor,)),
        extra=[TableInput(expose_id="censored", compiled=compiled, evaluation=censored)],
    )
    other = scorecard.table("censored")
    assert other is not None and any(
        entry.dimension == "marginals" and entry.outcome == CENSORED for entry in other.properties
    )
    assert not scorecard.thresholds[0].met
    assert scorecard.contributors(floor) == (EXPOSE,), "a censored property fed nothing"


def test_a_floor_at_one_is_met_only_by_a_perfect_score(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    at_one = _threshold("overall", None, "mustBeGreaterOrEqualTo", "1")
    above_one = _threshold("overall", None, "mustBeGreaterThan", "1")
    scorecard = _scorecard(
        compiled,
        profile_document,
        committed_rows,
        declaration=ScorecardDeclaration(thresholds=(at_one, above_one)),
    )
    assert [result.met for result in scorecard.thresholds] == [True, False]


def test_min_credibility_reads_the_measured_score(
    vectors: Mapping[str, Any], compiled: CompiledFidelity, profile_document: Dict[str, Any]
) -> None:
    drifted = _scorecard(
        compiled, profile_document, _case(vectors, "drifted-mean"), min_credibility="0.9"
    )
    (result,) = drifted.credibility
    assert result.basis == "measured" and result.met is False and not drifted.met
    assert drifted.failure_lines() == [result.line()]
    assert "is below minCredibility 0.9" in result.line()
    unmeasured = build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=DIGEST,
        run_digest=DIGEST,
        profile_digest=DIGEST,
        tables=[TableInput(expose_id="plain", min_credibility="0.7")],
    )
    (declared,) = unmeasured.credibility
    assert declared.basis == "declared" and declared.met is None and unmeasured.met
    assert "stays with the Playback's declared credibility" in declared.line()


def _strings(document: Any) -> Iterator[str]:
    if isinstance(document, str):
        yield document
    elif isinstance(document, Mapping):
        for key, value in document.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(document, list):
        for item in document:
            yield from _strings(item)


def _numbers(document: Any) -> Iterator[Any]:
    if isinstance(document, Mapping):
        for value in document.values():
            yield from _numbers(value)
    elif isinstance(document, list):
        for item in document:
            yield from _numbers(item)
    elif isinstance(document, (int, float)) and not isinstance(document, bool):
        yield document


@pytest.mark.parametrize(
    "case", ["identical", "one-grid-step", "drifted-mean", "unseen-category", "all-amounts-null"]
)
def test_the_document_is_valid_exact_deterministic_and_names_no_value(
    case: str,
    vectors: Mapping[str, Any],
    compiled: CompiledFidelity,
    profile_document: Dict[str, Any],
) -> None:
    rows = _case(vectors, case)
    declaration = ScorecardDeclaration(
        thresholds=(_threshold("overall", None, "mustBeGreaterOrEqualTo", "0.9"),)
    )
    scorecard = _scorecard(
        compiled, profile_document, rows, declaration=declaration, min_credibility="0.7"
    )
    document = scorecard.to_document()
    Draft202012Validator(SCHEMA).validate(document)
    assert document["_schemaURL"] == SCHEMA_URL
    again = _scorecard(
        compiled, profile_document, rows, declaration=declaration, min_credibility="0.7"
    )
    assert scorecard.to_bytes() == again.to_bytes()
    assert json.loads(scorecard.to_bytes()) == document
    # Counts are integers; every other number is a string.
    assert all(isinstance(number, int) for number in _numbers(document))
    # Groups are named by ordinal: no group key of the data appears anywhere.
    keys = {
        group["group"]
        for column in profile_document["tables"][EXPOSE]["columns"].values()
        for statistic in column["statistics"]
        for group in statistic["groups"]
    } - {""}
    assert keys and not keys & set(_strings(document))


def test_each_scored_property_is_an_openlineage_assertion(
    vectors: Mapping[str, Any], compiled: CompiledFidelity, profile_document: Dict[str, Any]
) -> None:
    facet = pytest.importorskip("openlineage.client.generated.data_quality_assertions_dataset")
    scorecard = _scorecard(compiled, profile_document, _case(vectors, "unseen-category"))
    (table,) = scorecard.to_document()["tables"]
    assertions = table["dataQualityAssertions"]["assertions"]
    assert [entry["name"] for entry in assertions] == [
        entry["propertyId"] for entry in table["properties"]
    ]
    for entry in assertions:
        facet.Assertion(**entry)
    by_name = {entry["name"]: entry for entry in assertions}
    assert by_name["002:support:market"]["success"] is False
    assert by_name["000:mean:amount"]["success"] is True
    assert "column" not in by_name["004:correlation:amount+attempts"]
    assert by_name["004:correlation:amount+attempts"]["params"]["columns"] == [
        "amount",
        "attempts",
    ]


def test_a_score_is_shown_exactly_or_rounded_down_to_four_places() -> None:
    assert display(Fraction(3, 4)) == "0.75"
    assert display(Fraction(1)) == "1"
    assert display(Fraction(2, 3)) == "0.6666"
    assert display(Fraction(99999, 100000)) == "0.9999"
    assert display(None) == "not measured"
    assert WITHIN == "within"


def test_the_published_example_is_a_valid_scorecard() -> None:
    example = json.loads((SPEC / "scorecard-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(SCHEMA).validate(example)
    assert example["met"] is False, "the example shows a missed threshold"
    outcomes = {entry["outcome"] for table in example["tables"] for entry in table["properties"]}
    assert outcomes == {WITHIN, OUTSIDE, CENSORED, REFUSED}
    assert example["_schemaURL"] == SCHEMA_URL, "the example is written in the current format"
    shape = example["shapeFlags"]
    assert shape["status"] == "checked"
    flagged = {
        table["exposeId"]
        for table in shape["tables"]
        for column in table.get("columns", ())
        if column.get("flags")
    }
    assert "subscriber_snapshot" in flagged, "a table with no fidelity block is checked too"


# ---------------------------------------------------------------------------
# Coverage: how much of the contract the score speaks for
# ---------------------------------------------------------------------------

#: The contract's columns: three carry a scored property in the block, two do not.
CONTRACT_COLUMNS = ("amount", "attempts", "market", "note", "region")
LINK = DeclaredLink(
    parent="customer",
    parent_columns=("id",),
    child=EXPOSE,
    child_columns=("attempts",),
)


def _with_relationship(document: Dict[str, Any], *, published: bool = True) -> Dict[str, Any]:
    """*document* with a ``customer`` table and children-per-parent statistics for ``LINK``."""
    found = edited(document)
    found["tables"]["customer"] = {"rowCount": 10, "columns": {"id": {"domain": "number"}}}
    statistic = edited(found["tables"][EXPOSE]["columns"]["amount"]["statistics"][0])
    if not published:
        statistic["groups"] = [
            {"group": group["group"], "status": "A"} for group in statistic["groups"]
        ]
    found["relationships"] = [
        {
            "parent": {"table": "customer", "columns": ["id"]},
            "child": {"table": EXPOSE, "columns": ["attempts"]},
            "statistics": [statistic],
        }
    ]
    return found


def test_a_profile_that_constrains_no_relationship_covers_none_of_them(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    contract = ContractShape(columns=CONTRACT_COLUMNS, relationships=(LINK,))
    scorecard = _scorecard(compiled, profile_document, committed_rows, contract=contract)
    coverage = scorecard.coverage.coverage
    assert (coverage.relationships.covered, coverage.relationships.of) == (0, 1)
    assert coverage.relationships.share == 0
    assert "0 of 1 relationship" in scorecard.coverage.text()
    document = scorecard.to_document()
    Draft202012Validator(SCHEMA).validate(document)
    assert document["coverage"]["relationships"] == {"covered": 0, "of": 1, "share": "0"}


def test_a_published_statistic_for_the_pair_constrains_the_relationship(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    contract = ContractShape(columns=CONTRACT_COLUMNS, relationships=(LINK,))
    published = _scorecard(
        compiled, _with_relationship(profile_document), committed_rows, contract=contract
    )
    assert published.coverage.coverage.relationships.covered == 1
    withheld = _scorecard(
        compiled,
        _with_relationship(profile_document, published=False),
        committed_rows,
        contract=contract,
    )
    assert withheld.coverage.coverage.relationships.covered == 0, "nothing was published"
    other = DeclaredLink(
        parent="customer", parent_columns=("id",), child=EXPOSE, child_columns=("market",)
    )
    elsewhere = _scorecard(
        compiled,
        _with_relationship(profile_document),
        committed_rows,
        contract=ContractShape(columns=CONTRACT_COLUMNS, relationships=(other,)),
    )
    assert elsewhere.coverage.coverage.relationships.covered == 0, "another key is not this one"


def test_columns_count_only_where_a_property_was_scored(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    contract = ContractShape(columns=CONTRACT_COLUMNS)
    scorecard = _scorecard(compiled, profile_document, committed_rows, contract=contract)
    table = scorecard.table(EXPOSE)
    assert table is not None and table.coverage is not None
    assert (table.coverage.columns.covered, table.coverage.columns.of) == (3, 5)
    assert table.coverage.columns.share == Fraction(3, 5)
    assert table.coverage.relationships.share is None, "none declared, so no share"
    assert (scorecard.coverage.tables_counted, scorecard.coverage.tables_total) == (1, 1)
    assert scorecard.summary_lines()[0].endswith("(covers 3 of 5 columns, 0 of 0 relationships)")
    # A column whose every property is censored carries no scored property.
    document = edited(profile_document)
    support = document["tables"][EXPOSE]["columns"]["market"]["statistics"][0]
    support["groups"] = [{"group": group["group"], "status": "A"} for group in support["groups"]]
    censored = _scorecard(compiled, document, committed_rows, contract=contract)
    assert censored.coverage.coverage.columns.covered == 2


def test_a_table_with_no_recorded_contract_is_not_counted(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    scorecard = _scorecard(
        compiled,
        profile_document,
        committed_rows,
        contract=ContractShape(columns=CONTRACT_COLUMNS),
        extra=[TableInput(expose_id="subscriber_snapshot")],
    )
    snapshot = scorecard.table("subscriber_snapshot")
    assert snapshot is not None and snapshot.coverage is None
    assert (scorecard.coverage.tables_counted, scorecard.coverage.tables_total) == (1, 2)
    assert "1 of 2 tables not counted" in scorecard.coverage.text()
    document = scorecard.to_document()
    Draft202012Validator(SCHEMA).validate(document)
    assert document["coverage"]["tables"] == {"counted": 1, "total": 2}
    assert [table["coverage"] is None for table in document["tables"]] == [False, True]
    bare = build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=DIGEST,
        run_digest=DIGEST,
        profile_digest=DIGEST,
        tables=[TableInput(expose_id="x")],
    )
    assert "coverage not known" in bare.coverage.text()


def test_a_coverage_floor_is_met_or_missed_by_the_share(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    contract = ContractShape(columns=CONTRACT_COLUMNS, relationships=(LINK,))
    floors = ScorecardDeclaration(
        thresholds=(
            _threshold("coverage", "columns", "mustBeGreaterOrEqualTo", "0.6"),
            _threshold("coverage", "columns", "mustBeGreaterOrEqualTo", "0.61"),
            _threshold("coverage", "relationships", "mustBeGreaterOrEqualTo", "0.5"),
        )
    )
    scorecard = _scorecard(
        compiled, profile_document, committed_rows, declaration=floors, contract=contract
    )
    assert [result.met for result in scorecard.thresholds] == [True, False, False]
    assert [result.score for result in scorecard.thresholds] == [
        Fraction(3, 5),
        Fraction(3, 5),
        Fraction(0),
    ]
    assert not scorecard.met
    assert len(scorecard.failure_lines()) == 2
    assert scorecard.thresholds[1].line() == (
        "threshold coverage.columns mustBeGreaterOrEqualTo 0.61: not met (0.6)"
    )
    assert "covers 3 of 5 columns" in scorecard.failure_lines()[0]
    assert scorecard.contributors(floors.thresholds[1]) == (EXPOSE,)
    Draft202012Validator(SCHEMA).validate(scorecard.to_document())
    constrained = _scorecard(
        compiled,
        _with_relationship(profile_document),
        committed_rows,
        declaration=ScorecardDeclaration(thresholds=(floors.thresholds[2],)),
        contract=contract,
    )
    assert constrained.met, "one of one relationship is constrained"


def test_a_coverage_floor_with_nothing_declared_is_not_met(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    floor = _threshold("coverage", "relationships", "mustBeGreaterOrEqualTo", "0")
    scorecard = _scorecard(
        compiled,
        profile_document,
        committed_rows,
        declaration=ScorecardDeclaration(thresholds=(floor,)),
        contract=ContractShape(columns=CONTRACT_COLUMNS),
    )
    assert scorecard.thresholds[0].score is None and not scorecard.thresholds[0].met
