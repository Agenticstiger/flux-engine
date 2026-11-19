# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A quantile that declares bins, end to end (ADR 0012).

A quantile property with ``bins`` is formed and compared by ``quantile/2``:
the order statistic as ``quantile/1`` takes it, published as the bin that
holds it and the records below and through that bin, and the property is
within when the synthetic order statistic lies within the margin of that bin.
This module pins the whole path in the engine: the compiler's margin and
edges, the committed state's ``S``, which spells the bin and never the
witness, the slice path's verdict, the profile path's verdict (within,
outside, censored, refused), the scorecard's similarity and its statistic
identifier.

A quantile without ``bins`` is ``quantile/1``, unchanged. The published
vectors (``tests/vectors/fidelity-vectors.json``, recomputed by
``tests/concordance/test_vectors.py``) pin the digests, ``S`` and ``C2`` of
a block whose quantile has no bins, so they show that nothing moved for it;
the last test here also shows the unbinned property of a block that holds a
binned one forms exactly what it forms alone.

Every expected number is worked out by hand in the comments: the grid is
four fractional bits, so a value ``v`` is ``16 * v`` encoded units, and the
declared range ``[0, 100]`` is ``[0, 1600]``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from flux_engine.concordance.boundary.slice import SliceReference, commit_state
from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.scorecard import Scorecard, TableInput, build_scorecard
from flux_engine.concordance.similarity import similarity, similarity_identifiers
from flux_engine.concordance.verifier import (
    CENSORED,
    OUTSIDE,
    WITHIN,
    ClearProfile,
    ClearStatistic,
    FidelityEvaluation,
)
from flux_engine.core.errors import FluxError
from flux_stats import GroupStatistic, Parameters, definition_for_property
from flux_stats.definitions import QUANTILE, QUANTILE_BINNED
from flux_stats.fixedpoint import epsilon_quantile
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_1_0, parse_profile
from tests.concordance.conftest import (
    blinding_of,
    build_compiled,
    build_expose,
    vectors_document,
)
from tests.stats.profiles import edited, profile_from_rows

pytestmark = pytest.mark.unit

EXPOSE_ID = "amount_moment"
BINNED = "000:quantile:amount"
PLAIN = "001:quantile:amount"
MEAN = "002:mean:amount"
BINS = ["0", "25", "50", "100"]
#: The declared edges on the grid: 16 units a step.
EDGES = (0, 400, 800, 1600)
TOLERANCE = {"type": "absolute", "value": "2"}


def _document(
    *,
    bins: Optional[List[str]] = BINS,
    level: str = "0.5",
    properties: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """A vectors-shaped document: one range column, and the quantile with and without bins."""
    binned: Dict[str, Any] = {
        "property": "quantile",
        "column": "amount",
        "level": level,
        "tolerance": dict(TOLERANCE),
    }
    if bins is not None:
        binned["bins"] = list(bins)
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.gold.amount",
            "exposes": [
                {
                    "exposeId": EXPOSE_ID,
                    "kind": "stream",
                    "contract": {"schema": [{"name": "amount", "type": "DECIMAL(18,4)"}]},
                }
            ],
        },
        "emit": {
            "productRef": "test.gold.amount",
            "exposeId": EXPOSE_ID,
            "fluidVersion": "^0.7.3",
        },
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": 4, "rounding": "floor"},
                "records": {"min": 2, "max": 64},
                "columns": {"amount": {"range": {"min": "0", "max": "100"}, "null": "fail"}},
                "properties": (
                    properties
                    if properties is not None
                    else [
                        binned,
                        {
                            "property": "quantile",
                            "column": "amount",
                            "level": level,
                            "tolerance": dict(TOLERANCE),
                        },
                        {"property": "mean", "column": "amount", "tolerance": dict(TOLERANCE)},
                    ]
                ),
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE_ID, "columns": ["amount"]},
    }


def _rows(values: Sequence[str]) -> List[Dict[str, Any]]:
    return [{"amount": value} for value in values]


#: Ten committed records. At level 1/2 the order statistic is the fifth smallest,
#: 33, which is 528 units, in bin 1 (``[400, 800)``). Three records lie below 25
#: and seven below 50, so the bin states ``binBelow`` 3 and ``binThrough`` 7:
#: ``3 < 5 <= 7``.
COMMITTED = _rows(["5", "12", "20", "30", "33", "40", "47", "60", "75", "90"])


def _commit(rows: Sequence[Mapping[str, Any]], compiled: CompiledFidelity) -> Any:
    return commit_state(
        rows=rows,
        compiled=compiled,
        ordinal=1,
        committed_at="2026-11-01T00:00:00Z",
        blinding=blinding_of(vectors_document()),
        slice_reference=SliceReference(
            uri="file:///committed.ndjson", first_line=1, last_line=len(rows)
        ),
    )


def _sliced(synthetic: Sequence[Mapping[str, Any]]) -> FidelityEvaluation:
    compiled = build_compiled(_document())
    committed = _commit(COMMITTED, compiled)
    return ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=synthetic
    ).evaluate()


def _profiled(
    synthetic: Sequence[Mapping[str, Any]], document: Optional[Mapping[str, Any]] = None
) -> FidelityEvaluation:
    compiled = build_compiled(_document())
    profile = document if document is not None else profile_from_rows(COMMITTED, compiled)
    return ClearProfile(
        compiled=compiled, profile=parse_profile(profile), synthetic_rows=list(synthetic)
    ).evaluate()


def _outcome(evaluation: FidelityEvaluation, property_id: str) -> Any:
    return next(entry for entry in evaluation.outcomes if entry.property_id == property_id)


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


def test_the_compiler_keeps_the_quantile_margin_and_carries_the_encoded_edges() -> None:
    compiled = build_compiled(_document())
    binned = compiled.property_by_id(BINNED)
    plain = compiled.property_by_id(PLAIN)
    assert binned.edges == EDGES
    assert plain.edges == ()
    # The bound and the margin are the quantile's own: the witness moves by
    # less than one grid step, bins or none, and the tolerance stays absolute.
    assert binned.epsilon == epsilon_quantile(compiled.block.grid) == plain.epsilon
    assert binned.margin == plain.margin == Fraction(2) - Fraction(1, 16)
    assert binned.margin_units == plain.margin_units == 31
    assert binned.declaration.tolerance.kind.value == "absolute"
    assert binned.magnitudes == plain.magnitudes


def test_a_quantile_picks_its_definition_from_its_declaration() -> None:
    compiled = build_compiled(_document())
    assert definition_for_property(compiled.property_by_id(BINNED).declaration) is QUANTILE_BINNED
    assert definition_for_property(compiled.property_by_id(PLAIN).declaration) is QUANTILE
    assert definition_for_property(compiled.property_by_id(MEAN).declaration).identifier == "mean/1"


def test_the_bins_are_part_of_the_declaration_so_they_move_h_and_g() -> None:
    with_bins = build_compiled(_document())
    without = build_compiled(_document(bins=None))
    assert with_bins.h != without.h and with_bins.g != without.g
    other = build_compiled(_document(bins=["0", "10", "100"]))
    assert other.g != with_bins.g
    # A tolerance is excluded from G, as it is for every property.
    looser = _document()
    looser["extension"]["fidelity"]["properties"][0]["tolerance"]["value"] = "3"
    assert build_compiled(looser).g == with_bins.g
    assert build_compiled(looser).h != with_bins.h


def test_an_edge_off_the_grid_is_refused_as_a_histograms_is() -> None:
    document = _document(bins=["0", "25.03", "100"])
    with pytest.raises(FluxError) as caught:
        compile_fidelity(
            contract=document["contract"],
            emit=document["emit"],
            extension=document["extension"],
            expose=build_expose(document),
        )
    assert caught.value.code.code == "FLX-5004"
    assert "bins[1]" in caught.value.detail


def test_bins_that_do_not_partition_the_range_are_refused() -> None:
    document = _document(bins=["0", "25", "90"])
    with pytest.raises(FluxError) as caught:
        build_compiled(document)
    assert caught.value.code.code == "FLX-5032"


def test_the_plan_names_the_property_by_its_kind() -> None:
    entries = build_compiled(_document()).plan_document()["properties"]
    assert [entry["property"] for entry in entries] == ["quantile", "quantile", "mean"]
    assert entries[0]["margin"] == entries[1]["margin"]


# ---------------------------------------------------------------------------
# S, on the slice path
# ---------------------------------------------------------------------------


def _groups(statistic: Any, property_id: str) -> List[Dict[str, Any]]:
    entries = statistic.to_document()["properties"]
    return list(next(entry for entry in entries if entry["propertyId"] == property_id)["groups"])


def test_s_spells_the_bin_and_its_counts_and_never_the_witness() -> None:
    compiled = build_compiled(_document())
    statistic = compute_statistic(COMMITTED, compiled)
    assert _groups(statistic, BINNED) == [
        {"group": "", "k": 10, "quantileBin": 1, "binBelow": 3, "binThrough": 7}
    ]
    # The unbinned quantile of the same block still states its witness and ranks.
    assert _groups(statistic, PLAIN) == [
        {
            "group": "",
            "k": 10,
            "quantileWitness": "528",
            "quantileBelow": 4,
            "quantileAtOrBelow": 5,
        }
    ]
    spelled = json.dumps(statistic.to_document(), sort_keys=True)
    assert spelled.count("quantileWitness") == 1


def test_the_committed_state_publishes_the_bin_and_not_the_witness() -> None:
    compiled = build_compiled(_document())
    committed = _commit(COMMITTED, compiled)
    published = committed.to_document()["S"]
    entry = next(item for item in published["properties"] if item["propertyId"] == BINNED)
    assert set(entry["groups"][0]) == {"group", "k", "quantileBin", "binBelow", "binThrough"}
    # The witness is formed in process (the engine's own synthetic side forms
    # one) but is not part of what the commitment is taken over.
    held = committed.statistic.properties[BINNED].groups[0]
    assert held.quantile_bin == 1 and held.quantile_witness == 528


def test_the_commitment_moves_with_the_bin_and_with_nothing_else_about_the_witness() -> None:
    compiled = build_compiled(_document())
    first = _commit(COMMITTED, compiled)
    # 33 becomes 31: the witness moves inside bin 1, and the bin's published
    # numbers are the same, but the unbinned quantile of the block changes C2.
    moved = _commit(_rows(["5", "12", "20", "30", "31", "40", "47", "60", "75", "90"]), compiled)
    assert _groups(first.statistic, BINNED) == _groups(moved.statistic, BINNED)
    assert first.c2 != moved.c2
    only = build_compiled(
        _document(properties=[_document()["extension"]["fidelity"]["properties"][0]])
    )
    assert (
        _commit(COMMITTED, only).c2
        == _commit(_rows(["5", "12", "20", "30", "31", "40", "47", "60", "75", "90"]), only).c2
    ), "with only the binned quantile, a witness that stays in its bin leaves C2 where it was"


# ---------------------------------------------------------------------------
# The slice path's verdict
# ---------------------------------------------------------------------------


#: A synthetic set of ten records, every one *value*: its order statistic is
#: *value*, with ranks 0 below and 10 at or below, which show it at level 1/2.
def _all(value: str) -> List[Dict[str, Any]]:
    return _rows([value] * 10)


@pytest.mark.parametrize(
    "value, binned, plain",
    [
        # Bin 1 is [400, 800), whose top grid value is 799. The margin is 31 units.
        ("33", WITHIN, WITHIN),  # the committed witness itself
        ("25", WITHIN, OUTSIDE),  # 400 units, at the bin's lower edge, 128 from 528
        ("49.9375", WITHIN, OUTSIDE),  # 799 units, the top grid value of the bin
        ("23.0625", WITHIN, OUTSIDE),  # 369 units: 31 below the lower edge
        ("23", OUTSIDE, OUTSIDE),  # 368 units: 32 below it
        ("51.875", WITHIN, OUTSIDE),  # 830 units: 31 above the top
        ("51.9375", OUTSIDE, OUTSIDE),  # 831 units: 32 above it
        ("50", WITHIN, OUTSIDE),  # 800 units: 1 above the top, well inside the margin
        ("33.0625", WITHIN, WITHIN),  # one step from the committed witness
    ],
)
def test_the_slice_path_compares_the_synthetic_witness_with_the_bin(
    value: str, binned: str, plain: str
) -> None:
    evaluation = _sliced(_all(value))
    assert not evaluation.refused
    assert _outcome(evaluation, BINNED).outcome == binned, value
    assert _outcome(evaluation, PLAIN).outcome == plain, value


def test_the_bin_slack_is_what_a_declaration_chooses_to_trade() -> None:
    """Within the margin of the reference quantile passes; up to a bin further can too."""
    for step in range(-31, 32):
        value = format(Decimal(528 + step) / 16, "f")
        evaluation = _sliced(_all(value))
        assert _outcome(evaluation, PLAIN).outcome == WITHIN
        assert _outcome(evaluation, BINNED).outcome == WITHIN, step


def test_the_last_bin_is_closed_at_the_top_on_the_slice_path() -> None:
    # k = 10 and level 9/10: the ninth smallest, 100, which is the declared maximum.
    document = _document(level="0.9")
    compiled = build_compiled(document)
    committed = _commit(
        _rows(["5", "12", "20", "30", "33", "80", "90", "100", "100", "100"]), compiled
    )
    group = committed.statistic.properties[BINNED].groups[0]
    assert (group.quantile_witness, group.quantile_bin) == (1600, 2)
    # Five records lie below 50, and the last bin counts all ten through its top.
    assert (group.bin_below, group.bin_through) == (5, 10)
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=_all("100")
    ).evaluate()
    assert _outcome(evaluation, BINNED).outcome == WITHIN
    far = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=_all("40")
    ).evaluate()
    assert _outcome(far, BINNED).outcome == OUTSIDE


def test_a_synthetic_set_outside_the_bins_range_is_refused_before_a_verdict() -> None:
    evaluation = _sliced(_rows(["500"] * 10))
    assert evaluation.refused
    assert {refusal.code for refusal in evaluation.refusals} == {"FLX-5005"}


# ---------------------------------------------------------------------------
# The profile path
# ---------------------------------------------------------------------------


def _profile() -> Dict[str, Any]:
    return profile_from_rows(COMMITTED, build_compiled(_document()))


def _binned_statistic(document: Dict[str, Any]) -> Dict[str, Any]:
    statistics = document["tables"][EXPOSE_ID]["columns"]["amount"]["statistics"]
    return next(entry for entry in statistics if entry["definition"] == "quantile/2")


def test_a_profile_of_the_slice_holds_the_bin_in_format_1_1_0() -> None:
    document = _profile()
    assert document["_schemaURL"] == SCHEMA_URL_1_1_0
    statistic = _binned_statistic(document)
    assert statistic["bins"] == BINS and statistic["level"] == "0.5"
    assert statistic["groups"] == [
        {"group": "", "k": 10, "quantileBin": 1, "binBelow": 3, "binThrough": 7, "status": "F"}
    ]
    assert "quantileWitness" not in json.dumps(statistic)


def test_a_profile_without_a_binned_quantile_names_format_1_0_0() -> None:
    compiled = build_compiled(_document(bins=None))
    assert profile_from_rows(COMMITTED, compiled)["_schemaURL"] == SCHEMA_URL


@pytest.mark.parametrize("value", ["33", "25", "23.0625", "23", "51.875", "51.9375", "70", "2"])
def test_the_profile_path_gives_the_slice_paths_outcomes(value: str) -> None:
    synthetic = _all(value)
    sliced, profiled = _sliced(synthetic), _profiled(synthetic)
    for property_id in (BINNED, PLAIN, MEAN):
        assert _outcome(profiled, property_id).outcome == _outcome(sliced, property_id).outcome
        assert [group.outcome for group in _outcome(profiled, property_id).groups] == [
            group.outcome for group in _outcome(sliced, property_id).groups
        ]


def test_the_profile_path_scores_within_and_outside() -> None:
    within = _outcome(_profiled(_all("33")), BINNED)
    assert within.outcome == WITHIN and within.groups[0].score == 1
    inside_the_bin = _outcome(_profiled(_all("40")), BINNED)
    assert inside_the_bin.outcome == WITHIN and inside_the_bin.groups[0].score == 1
    # 18 is 288 units, 112 below the lower edge of bin 1, over the width 1600.
    outside = _outcome(_profiled(_all("18")), BINNED)
    assert outside.outcome == OUTSIDE
    assert outside.groups[0].score == 1 - Fraction(112, 1600)
    assert outside.groups[0].committed_count == 10 and outside.groups[0].synthetic_count == 10


def test_a_withheld_group_is_censored_and_the_other_properties_are_still_scored() -> None:
    document = _profile()
    _binned_statistic(document)["groups"] = [{"group": "", "status": "A"}]
    evaluation = _profiled(_all("33"), document)
    quantile = _outcome(evaluation, BINNED)
    assert quantile.outcome == CENSORED and quantile.censored
    assert quantile.groups[0].score is None
    assert _outcome(evaluation, PLAIN).outcome == WITHIN
    assert quantile in evaluation.censored and quantile not in evaluation.outside
    # A synthetic set far from the bin is not scored either: the group is unknown.
    far = _profiled(_all("90"), document)
    assert _outcome(far, BINNED).outcome == CENSORED
    assert _outcome(far, PLAIN).outcome == OUTSIDE


def test_a_profile_whose_only_statistics_are_withheld_scores_nothing() -> None:
    document = _profile()
    for entry in document["tables"][EXPOSE_ID]["columns"]["amount"]["statistics"]:
        entry["groups"] = [{"group": "", "status": "D"}]
    evaluation = _profiled(_all("33"), document)
    assert all(outcome.censored for outcome in evaluation.outcomes)
    assert not evaluation.within and not evaluation.refused


def test_a_profile_that_holds_only_the_unbinned_quantile_does_not_answer_the_binned_one() -> None:
    document = _profile()
    statistics = document["tables"][EXPOSE_ID]["columns"]["amount"]["statistics"]
    statistics[:] = [entry for entry in statistics if entry["definition"] != "quantile/2"]
    evaluation = _profiled(_all("33"), document)
    assert [(refusal.code, refusal.property_id) for refusal in evaluation.refusals] == [
        ("FLX-5037", BINNED)
    ]


@pytest.mark.parametrize(
    "bins", [["0", "50", "100"], ["0", "25", "50", "75", "100"]], ids=["fewer", "more"]
)
def test_a_profile_over_other_bins_does_not_answer_the_property(bins: List[str]) -> None:
    document = _profile()
    statistic = _binned_statistic(document)
    statistic["bins"] = bins
    # A first bin has no record below it, so the group stays one a reader admits.
    statistic["groups"][0].update(quantileBin=0, binBelow=0)
    evaluation = _profiled(_all("33"), document)
    assert [(refusal.code, refusal.property_id) for refusal in evaluation.refusals] == [
        ("FLX-5037", BINNED)
    ]


def test_a_profile_over_another_level_does_not_answer_the_property() -> None:
    document = _profile()
    _binned_statistic(document)["level"] = "0.25"
    evaluation = _profiled(_all("33"), document)
    assert [(refusal.code, refusal.property_id) for refusal in evaluation.refusals] == [
        ("FLX-5037", BINNED)
    ]


def test_a_profile_on_another_grid_does_not_answer_the_property() -> None:
    document = _profile()
    document["encoding"]["fractionalBits"] = 8
    evaluation = _profiled(_all("33"), document)
    assert (("FLX-5037", BINNED)) in [
        (refusal.code, refusal.property_id) for refusal in evaluation.refusals
    ]


def test_a_bin_whose_counts_do_not_show_the_order_statistic_in_it_is_refused() -> None:
    document = _profile()
    group = _binned_statistic(document)["groups"][0]
    # 5 < 5 is false: the fifth record is not above the five below the bin.
    group.update(binBelow=5, binThrough=7)
    evaluation = _profiled(_all("33"), document)
    refusals = [(refusal.code, refusal.property_id) for refusal in evaluation.refusals]
    assert refusals == [("FLX-5005", BINNED)]
    assert "ranks" in evaluation.refusals[0].reason
    assert _outcome(evaluation, BINNED).outcome == OUTSIDE
    assert not evaluation.within


def test_a_bin_that_ends_below_the_order_statistic_is_refused() -> None:
    document = _profile()
    _binned_statistic(document)["groups"][0].update(binBelow=7, binThrough=7)
    evaluation = _profiled(_all("33"), document)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5005"]


def test_a_format_1_0_0_profile_that_holds_the_statistic_is_refused_before_it_is_read() -> None:
    document = edited(_profile())
    document["_schemaURL"] = SCHEMA_URL
    with pytest.raises(FluxError) as caught:
        parse_profile(document)
    assert caught.value.code.code == "FLX-5036"


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------


def _scorecard(value: str) -> Scorecard:
    compiled = build_compiled(_document())
    evaluation = _profiled(_all(value))
    digest = "sha256:" + "a" * 64
    return build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=digest,
        run_digest=digest,
        profile_digest=digest,
        tables=[TableInput(expose_id=EXPOSE_ID, compiled=compiled, evaluation=evaluation)],
    )


def _entry(scorecard: Scorecard, property_id: str) -> Any:
    table = scorecard.table(EXPOSE_ID)
    assert table is not None
    return next(entry for entry in table.properties if entry.property_id == property_id)


def test_the_scorecard_names_each_quantiles_statistic() -> None:
    scorecard = _scorecard("33")
    assert _entry(scorecard, BINNED).statistic == "quantile/2"
    assert _entry(scorecard, PLAIN).statistic == "quantile/1"
    assert _entry(scorecard, BINNED).kind == "quantile"
    assert _entry(scorecard, BINNED).dimension == _entry(scorecard, PLAIN).dimension


def test_the_scorecard_scores_the_distance_from_the_bin() -> None:
    inside = _entry(_scorecard("40"), BINNED)
    assert inside.outcome == WITHIN and inside.score == 1
    below = _entry(_scorecard("18"), BINNED)
    assert below.outcome == OUTSIDE and below.score == Fraction(1488, 1600)
    # The unbinned quantile of the same block is scored against the witness.
    assert _entry(_scorecard("40"), PLAIN).score == 1 - Fraction(112, 1600)


def test_a_withheld_quantile_2_group_is_censored_in_the_scorecard() -> None:
    document = _profile()
    _binned_statistic(document)["groups"] = [{"group": "", "status": "A"}]
    compiled = build_compiled(_document())
    digest = "sha256:" + "a" * 64
    scorecard = build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=digest,
        run_digest=digest,
        profile_digest=digest,
        tables=[
            TableInput(
                expose_id=EXPOSE_ID,
                compiled=compiled,
                evaluation=_profiled(_all("33"), document),
            )
        ],
    )
    entry = _entry(scorecard, BINNED)
    assert entry.outcome == CENSORED and entry.score is None and entry.groups_censored == 1


# ---------------------------------------------------------------------------
# The similarity
# ---------------------------------------------------------------------------


def _reference(index: int, below: int, through: int) -> GroupStatistic:
    return GroupStatistic(group="", k=10, quantile_bin=index, bin_below=below, bin_through=through)


def _witness(units: int) -> GroupStatistic:
    return QUANTILE_BINNED.measure("", [units] * 10, _parameters())


def _parameters() -> Parameters:
    return Parameters(edges=EDGES, level=Fraction(1, 2))


def _score(units: int, reference: GroupStatistic, width: Optional[int] = 1600) -> Fraction:
    return similarity(QUANTILE_BINNED, _witness(units), reference, width_units=width, edges=EDGES)


def test_every_registered_statistic_has_a_similarity() -> None:
    assert "quantile/2" in similarity_identifiers()


@pytest.mark.parametrize(
    "units, expected",
    [
        (400, Fraction(1)),
        (528, Fraction(1)),
        (799, Fraction(1)),
        (399, 1 - Fraction(1, 1600)),
        (800, 1 - Fraction(1, 1600)),
        (0, 1 - Fraction(400, 1600)),
        (1600, 1 - Fraction(801, 1600)),
    ],
)
def test_the_similarity_is_one_minus_the_distance_from_the_bin_over_the_width(
    units: int, expected: Fraction
) -> None:
    assert _score(units, _reference(1, 3, 7)) == expected


def test_the_last_bin_counts_its_top_edge_as_inside() -> None:
    last = _reference(2, 3, 10)
    assert _score(1600, last) == 1
    assert _score(800, last) == 1
    assert _score(799, last) == 1 - Fraction(1, 1600)


def test_a_distance_wider_than_the_range_scores_zero_and_never_below() -> None:
    assert _score(0, _reference(2, 3, 10), width=100) == 0
    assert _score(1600, _reference(0, 0, 4), width=100) == 0


def test_a_range_of_one_grid_point_scores_one_or_zero() -> None:
    assert _score(5, _reference(1, 3, 7), width=0) == 0
    assert _score(500, _reference(1, 3, 7), width=0) == 1


def test_the_similarity_needs_a_witness_a_published_bin_and_a_width() -> None:
    with pytest.raises(FluxError) as caught:
        _score(500, GroupStatistic(group="", k=0))
    assert caught.value.code.code == "FLX-5005"
    with pytest.raises(FluxError) as caught:
        similarity(
            QUANTILE_BINNED,
            GroupStatistic(group="", k=0),
            _reference(1, 3, 7),
            width_units=1600,
            edges=EDGES,
        )
    assert caught.value.code.code == "FLX-5005"
    with pytest.raises(FluxError) as caught:
        _score(500, _reference(1, 3, 7), width=None)
    assert caught.value.code.code == "FLX-5037"


def test_the_unbinned_quantile_is_scored_as_it_was() -> None:
    """quantile/1 reads no edges, so a caller that passes some gets the same score."""
    synthetic = QUANTILE.measure("", [528 + 100] * 10, _parameters())
    reference = QUANTILE.measure("", [528] * 10, _parameters())
    assert similarity(QUANTILE, synthetic, reference, width_units=1600) == 1 - Fraction(100, 1600)
    assert similarity(
        QUANTILE, synthetic, reference, width_units=1600, edges=EDGES
    ) == 1 - Fraction(100, 1600)


# ---------------------------------------------------------------------------
# A quantile without bins does not move
# ---------------------------------------------------------------------------


def test_a_block_with_no_binned_quantile_forms_what_it_formed_before() -> None:
    plain_only = build_compiled(_document(bins=None))
    statistic = compute_statistic(COMMITTED, plain_only)
    entries = statistic.to_document()["properties"]
    quantile = next(entry for entry in entries if entry["propertyId"] == BINNED)
    assert quantile["groups"] == [
        {
            "group": "",
            "k": 10,
            "quantileWitness": "528",
            "quantileBelow": 4,
            "quantileAtOrBelow": 5,
        }
    ]
    assert all(plain_only.property_by_id(entry["propertyId"]).edges == () for entry in entries)
    assert not any(
        definition_for_property(entry.declaration).binned for entry in plain_only.properties
    )


def test_the_binned_properties_neighbours_form_exactly_what_they_form_alone() -> None:
    both = compute_statistic(COMMITTED, build_compiled(_document()))
    alone = compute_statistic(
        COMMITTED,
        build_compiled(
            _document(properties=[_document()["extension"]["fidelity"]["properties"][1]])
        ),
    )
    assert _groups(both, PLAIN) == _groups(alone, "000:quantile:amount")
