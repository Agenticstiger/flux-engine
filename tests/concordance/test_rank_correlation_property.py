# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A ``rankCorrelation`` property against a profile (ADR 0035 decision 7).

The reference values are formed here by brute force over every pair of rows (and mid-ranks by
counting), not with the code under test, so the verifier's sort-based forms are checked against the
definitions. The profile is built from them: exact, or noisy with a stated scale.
"""

from __future__ import annotations

import json
import math
import random
from fractions import Fraction
from itertools import combinations
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.rank import compare_rank, rank_score
from flux_engine.concordance.scorecard import (
    SCHEMA_URL,
    SCHEMA_URL_1_5_0,
    PropertyScore,
    TableInput,
    build_scorecard,
)
from flux_engine.concordance.verifier import (
    CENSORED,
    OUTSIDE,
    WITHIN,
    ClearProfile,
    ClearStatistic,
)
from flux_engine.core.errors import FluxError
from flux_stats.fidelity import parse_fidelity_block
from flux_stats.profile import SCHEMA_URL_1_4_0, parse_profile
from flux_stats.rank_correlation import rank_null_variance, rank_reach
from tests.concordance.conftest import build_compiled, build_expose

pytestmark = pytest.mark.unit

EXPOSE = "readings"
GRID_BITS = 16


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def brute_s(pairs: Sequence[Tuple[int, int]]) -> int:
    return sum(_sign(a[0] - b[0]) * _sign(a[1] - b[1]) for a, b in combinations(pairs, 2))


def _mid_ranks(values: Sequence[int]) -> List[Fraction]:
    return [
        Fraction(sum(1 for w in values if w < v))
        + Fraction(sum(1 for w in values if w == v) + 1, 2)
        for v in values
    ]


def brute_a4(pairs: Sequence[Tuple[int, int]]) -> int:
    n = len(pairs)
    left, right = _mid_ranks([p[0] for p in pairs]), _mid_ranks([p[1] for p in pairs])
    centre = Fraction(n + 1, 2)
    total = 4 * sum((r - centre) * (q - centre) for r, q in zip(left, right, strict=True))
    assert total.denominator == 1
    return int(total)


def _document(
    properties: List[Dict[str, Any]], *, first: str = "a", second: str = "b"
) -> Dict[str, Any]:
    columns = {
        "a": {"range": {"min": "0", "max": "1000"}, "null": "exclude"},
        "b": {"range": {"min": "0", "max": "1000"}, "null": "exclude"},
        "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}, "null": "exclude"},
    }
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.private.readings",
            "exposes": [
                {
                    "exposeId": EXPOSE,
                    "kind": "stream",
                    "contract": {
                        "schema": [
                            {"name": "a", "type": "INT"},
                            {"name": "b", "type": "INT"},
                            {"name": "d", "type": "DATE"},
                        ]
                    },
                }
            ],
        },
        "emit": {
            "productRef": "test.private.readings",
            "exposeId": EXPOSE,
            "fluidVersion": "^0.7.3",
        },
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": GRID_BITS, "rounding": "floor"},
                "records": {"min": 2, "max": 1_000_000},
                "columns": columns,
                "properties": properties,
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE, "columns": ["a", "b", "d"]},
    }


KENDALL = {"property": "rankCorrelation", "columns": ["a", "b"], "measure": "kendall"}
SPEARMAN = {"property": "rankCorrelation", "columns": ["a", "b"], "measure": "spearman"}


def _compiled(*properties: Dict[str, Any]) -> CompiledFidelity:
    return build_compiled(_document(list(properties)))


def _rows(pairs: Sequence[Tuple[int, int]]) -> List[Dict[str, Any]]:
    return [{"a": a, "b": b, "d": "2026-03-04"} for a, b in pairs]


def _correlated(n: int, seed: int, noise: int = 40) -> List[Tuple[int, int]]:
    """Pairs of whole numbers in [0, 1000] with a positive relation and many ties."""
    generator = random.Random(seed)
    found = []
    for _ in range(n):
        a = generator.randint(0, 20) * 50
        b = min(1000, max(0, a + generator.randint(-noise, noise) * 5)) // 10 * 10
        found.append((a, b))
    return found


def _grid(pairs: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    return [(a << GRID_BITS, b << GRID_BITS) for a, b in pairs]


def _profile(
    *statistics: Dict[str, Any], columns: Tuple[str, str] = ("a", "b"), bits: int = GRID_BITS
) -> Dict[str, Any]:
    return {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": bits, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        **(
            {
                "privacy": {
                    "measure": "zeroConcentratedDP",
                    "privacyLoss": {"rho": "1000"},
                    "privacyUnit": {"expose": EXPOSE},
                }
            }
            if any("noise" in statistic for statistic in statistics)
            else {}
        ),
        "tables": {
            EXPOSE: {
                "columns": {
                    "a": {"domain": "number"},
                    "b": {"domain": "number"},
                    "d": {"domain": "timestamp"},
                },
                "columnPairs": [{"columns": list(columns), "statistics": list(statistics)}],
            }
        },
    }


def exact_kendall(pairs: Sequence[Tuple[int, int]], *, floor: int | None = None) -> Dict[str, Any]:
    grid = _grid(pairs)
    return {
        "definition": "kendallTau/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "k": len(grid), "S": str(brute_s(grid)), "status": "F"}],
        **({"rowFloor": floor} if floor is not None else {}),
    }


def exact_spearman(pairs: Sequence[Tuple[int, int]]) -> Dict[str, Any]:
    grid = _grid(pairs)
    return {
        "definition": "spearmanRho/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "k": len(grid), "A4": str(brute_a4(grid)), "status": "F"}],
    }


def noisy(definition: str, value: str, scale: str, mechanism: str = "gaussian") -> Dict[str, Any]:
    loss = {"rho": "1"} if mechanism == "gaussian" else {"epsilon": "1", "delta": "0"}
    return {
        "definition": definition,
        "rowFloor": 100,
        "sampleSize": {"min": 1},
        "disclosure": "noisy",
        "noise": {"mechanism": mechanism, "scale": scale, "privacyLoss": loss},
        "groups": [{"group": "", "status": "F", "noisyValue": value}],
    }


def evaluate(compiled: CompiledFidelity, document: Dict[str, Any], rows: Any) -> Any:
    return ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=rows
    ).evaluate()


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_a_rank_correlation_declares_a_measure_two_columns_and_defaults_its_tolerance() -> None:
    compiled = _compiled(KENDALL)
    declaration = compiled.properties[0].declaration
    assert (declaration.measure, declaration.columns) == ("kendall", ("a", "b"))
    assert declaration.tolerance.declared == {"max": "0.05", "type": "absolute"}
    assert declaration.tolerance.magnitude == Fraction(1, 20)
    # Nothing is encoded that the grid could move across an order: epsilon is zero, and the margin is
    # the tolerance rounded onto the grid in the tightening direction.
    assert compiled.properties[0].epsilon == 0
    assert Fraction(1, 20) - compiled.properties[0].margin < Fraction(1, 2**GRID_BITS)
    assert compiled.plan_document()["properties"][0]["property"] == "rankCorrelation"
    assert compiled.properties[0].magnitudes == {}


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"measure": "pearson"}, "measure"),
        ({"columns": ["a", "a"]}, "two distinct columns"),
        ({"columns": ["a"]}, "two columns"),
        ({"groupBy": "b"}, "ungrouped"),
        ({"tolerance": {"type": "absolute", "value": "0.1"}}, "needs a max"),
        ({"tolerance": {"type": "absolute", "max": "0.1", "value": "0.1"}}, "no value"),
        ({"tolerance": {"type": "relative", "value": "0.1"}}, "relative"),
        ({"tolerance": {"type": "absolute", "max": "0"}}, "positive"),
        ({"minVariance": "1"}, "minVariance"),
    ],
)
def test_a_malformed_rank_correlation_is_refused(change: Dict[str, Any], text: str) -> None:
    document = _document([{**KENDALL, **change}])
    with pytest.raises(FluxError) as refused:
        parse_fidelity_block(document["extension"]["fidelity"], expose=build_expose(document))
    assert text in str(refused.value)


def test_a_measure_belongs_to_a_rank_correlation_alone() -> None:
    document = _document(
        [
            {
                "property": "correlation",
                "columns": ["a", "b"],
                "measure": "kendall",
                "minVariance": "1",
                "tolerance": {"type": "absolute", "value": "0.1"},
            }
        ]
    )
    with pytest.raises(FluxError, match="only a rankCorrelation carries a measure"):
        parse_fidelity_block(document["extension"]["fidelity"], expose=build_expose(document))


def test_a_rank_correlation_needs_a_measure() -> None:
    document = _document([{"property": "rankCorrelation", "columns": ["a", "b"]}])
    with pytest.raises(FluxError, match="needs a measure"):
        parse_fidelity_block(document["extension"]["fidelity"], expose=build_expose(document))


# ---------------------------------------------------------------------------
# The statistic and the score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(4))
def test_the_runs_statistic_is_the_plain_one_formed_by_brute_force(seed: int) -> None:
    pairs = _correlated(60, seed)
    compiled = _compiled(KENDALL, SPEARMAN)
    document = _profile(exact_kendall(pairs), exact_spearman(pairs))
    evaluation = evaluate(compiled, document, _rows(pairs))
    kendall, spearman = evaluation.outcomes
    assert kendall.rank is not None and spearman.rank is not None
    n = len(pairs)
    grid = _grid(pairs)
    assert kendall.rank.run == Fraction(brute_s(grid), n * (n - 1) // 2)
    assert spearman.rank.run == Fraction(3 * brute_a4(grid), n * (n * n - 1))
    # Run and profile are the same rows, so the distance is zero and the score is one.
    assert kendall.rank.distance == 0 and kendall.rank.score == 1
    assert [o.outcome for o in evaluation.outcomes] == [WITHIN, WITHIN]
    assert kendall.groups[0].score == 1


def test_a_pair_is_read_in_either_order() -> None:
    pairs = _correlated(50, 7)
    flipped = [(b, a) for a, b in pairs]
    compiled = _compiled(KENDALL)
    forward = evaluate(compiled, _profile(exact_kendall(pairs)), _rows(pairs))
    backward = evaluate(
        compiled, _profile(exact_kendall(flipped), columns=("b", "a")), _rows(pairs)
    )
    assert forward.outcomes[0].rank == backward.outcomes[0].rank
    assert backward.outcomes[0].outcome == WITHIN


def test_a_date_column_needs_no_conversion() -> None:
    days = [(i % 9, (i * 7) % 11) for i in range(40)]
    rows = [{"a": a, "b": b, "d": f"2026-02-{i % 28 + 1:02d}"} for i, (a, b) in enumerate(days)]
    # ``d`` against ``b``: the profile holds it as milliseconds, the declaration as days.
    pairs = [((i % 28) * 86_400_000, b) for i, (_, b) in enumerate(days)]
    document = _profile(
        {
            "definition": "kendallTau/1",
            "sampleSize": {"min": 1},
            "disclosure": "none",
            "groups": [
                {
                    "group": "",
                    "k": 40,
                    "S": str(brute_s([(x << GRID_BITS, y << GRID_BITS) for x, y in pairs])),
                    "status": "F",
                }
            ],
        },
        columns=("d", "b"),
    )
    compiled = build_compiled(
        _document([{"property": "rankCorrelation", "columns": ["d", "b"], "measure": "kendall"}])
    )
    # The run has its own dates, so the statistic of its day numbers is its own; what matters is that
    # a DATE column is scored at all against a profile that holds it in milliseconds.
    evaluation = evaluate(compiled, document, rows)
    assert not evaluation.refusals
    assert evaluation.outcomes[0].rank is not None


def test_a_profile_on_another_grid_is_refused() -> None:
    pairs = _correlated(30, 1)
    evaluation = evaluate(_compiled(KENDALL), _profile(exact_kendall(pairs), bits=8), _rows(pairs))
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5037"]
    assert "fractionalBits" in evaluation.refusals[0].reason


def test_a_profile_without_the_statistic_is_refused_and_names_no_value() -> None:
    pairs = _correlated(30, 1)
    evaluation = evaluate(_compiled(SPEARMAN), _profile(exact_kendall(pairs)), _rows(pairs))
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5037"]
    assert evaluation.refusals[0].reason == (
        "the profile holds no spearmanRho/1 statistic over this property's columns"
    )


def test_a_suppressed_group_is_censored() -> None:
    pairs = _correlated(30, 1)
    suppressed = {
        "definition": "kendallTau/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "status": "C"}],
    }
    evaluation = evaluate(_compiled(KENDALL), _profile(suppressed), _rows(pairs))
    assert evaluation.outcomes[0].outcome == CENSORED
    assert not evaluation.refusals


def test_a_run_with_fewer_than_two_pairs_is_refused() -> None:
    pairs = _correlated(30, 1)
    rows = _rows(pairs[:1]) + [{"a": None, "b": 3, "d": "2026-03-04"}]
    evaluation = evaluate(_compiled(KENDALL), _profile(exact_kendall(pairs)), rows)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5005"]


def test_a_run_against_the_opposite_ordering_is_outside() -> None:
    pairs = _correlated(300, 3)
    reversed_pairs = [(a, 1000 - b) for a, b in pairs]
    evaluation = evaluate(_compiled(KENDALL), _profile(exact_kendall(pairs)), _rows(reversed_pairs))
    found = evaluation.outcomes[0]
    assert found.outcome == OUTSIDE and not evaluation.within
    assert found.rank is not None and found.rank.distance > 1
    assert found.groups[0].score is not None and found.groups[0].score < Fraction(1, 2)


# ---------------------------------------------------------------------------
# The widenings
# ---------------------------------------------------------------------------


def test_the_null_variance_and_the_reach_follow_the_stated_forms() -> None:
    assert rank_null_variance("kendallTau/1", 10) == Fraction(2 * 25, 9 * 10 * 9)
    assert rank_null_variance("spearmanRho/1", 10) == Fraction(1, 9)
    # sqrt(10 v), rounded up at 12 places: 10 / 9 has a root of 1.054092553389...
    assert rank_reach(Fraction(1, 9)) == Fraction("1.054092553390")
    assert rank_reach(Fraction(0)) == 0
    assert rank_reach(Fraction(1, 10)) == 1


def test_a_score_is_one_minus_half_the_distance_rounded_down_at_twelve_places() -> None:
    assert rank_score(Fraction(0)) == 1
    assert rank_score(Fraction(2)) == 0
    assert rank_score(Fraction(1, 3)) == Fraction("0.833333333333")
    assert rank_score(Fraction(1, 3)) <= 1 - Fraction(1, 6)


def test_a_small_run_is_widened_by_its_sampling_and_a_large_one_hardly() -> None:
    compiled = _compiled(KENDALL)
    declaration = compiled.properties[0].declaration
    margin = compiled.properties[0].margin
    assert margin is not None
    small = compare_rank(
        declaration, margin, run=Fraction(0), records=50, reference=Fraction(0), variance=None
    )
    large = compare_rank(
        declaration, margin, run=Fraction(0), records=50_000, reference=Fraction(0), variance=None
    )
    assert small.sampling == rank_reach(rank_null_variance("kendallTau/1", 50))
    assert small.sampling > 5 * large.sampling
    assert small.noise is None and large.noise is None
    assert small.reach == margin + small.sampling
    # A distance just inside the reach is within, one just past it is not.
    inside = compare_rank(
        declaration, margin, run=small.reach, records=50, reference=Fraction(0), variance=None
    )
    past = compare_rank(
        declaration,
        margin,
        run=small.reach + Fraction(1, 10**6),
        records=50,
        reference=Fraction(0),
        variance=None,
    )
    assert inside.within and not past.within


def test_a_noisy_reference_widens_by_the_reach_of_its_variance() -> None:
    pairs = _correlated(2000, 5)
    exact = exact_kendall(pairs)
    n = len(pairs)
    tau = Fraction(brute_s(_grid(pairs)), n * (n - 1) // 2)
    compiled = _compiled(KENDALL)
    # A released value 0.13 away from the run's, with noise of scale 0.05: a reach of sqrt(10 * 0.0025)
    # = 0.158 > 0.13 - margin - sampling, so the run is within; with the exact reference it is not.
    off = tau + Fraction(13, 100)
    document = _profile(noisy("kendallTau/1", str(float(off)), "0.05"))
    found = evaluate(compiled, document, _rows(pairs)).outcomes[0]
    assert found.noise_mechanism == "gaussian" and found.cell_variance == Fraction(1, 400)
    assert found.rank is not None
    assert found.rank.noise == Fraction("0.158113883009")  # sqrt(10 / 400) rounded up at 12 places
    assert found.widening == found.rank.noise == found.groups[0].widening
    assert found.outcome == WITHIN
    assert (
        found.rank.reach == compiled.properties[0].margin + found.rank.noise + found.rank.sampling
    )
    # The noisy value clamped to [-1, 1] is the reference.
    clamped = evaluate(
        compiled, _profile(noisy("kendallTau/1", "7", "0.05")), _rows(pairs)
    ).outcomes[0]
    assert clamped.rank is not None and clamped.rank.reference == 1
    del exact


def test_a_laplace_reference_widens_by_the_reach_of_twice_its_squared_scale() -> None:
    pairs = _correlated(500, 2)
    document = _profile(noisy("spearmanRho/1", "0.5", "0.1", mechanism="laplace"))
    document["privacy"] = {
        "measure": "approximateDP",
        "privacyLoss": {"epsilon": "1000", "delta": "0"},
        "privacyUnit": {"expose": EXPOSE},
    }
    found = evaluate(_compiled(SPEARMAN), document, _rows(pairs)).outcomes[0]
    assert found.cell_variance == Fraction(2, 100)
    assert found.rank is not None and found.rank.noise == rank_reach(Fraction(2, 100))
    assert found.noise_mechanism == "laplace"


def test_a_reference_too_noisy_to_tell_is_marked_and_every_run_is_within() -> None:
    pairs = _correlated(200, 4)
    reversed_pairs = [(a, 1000 - b) for a, b in pairs]
    document = _profile(noisy("kendallTau/1", "0.9", "1"))
    found = evaluate(_compiled(KENDALL), document, _rows(reversed_pairs)).outcomes[0]
    assert found.rank is not None
    assert found.rank.too_noisy and found.rank.reach >= 2
    assert found.outcome == WITHIN


def test_three_rows_are_too_small_to_tell_whatever_the_reference() -> None:
    rows = _rows([(1, 3), (2, 2), (3, 1)])
    # Kendall's null variance at n = 3 is 22 / 54: its reach, 2.018..., is already above 2.
    found = evaluate(_compiled(KENDALL), _profile(exact_kendall([(1, 1), (2, 2), (3, 3)])), rows)
    assert found.outcomes[0].rank is not None
    assert found.outcomes[0].rank.too_noisy
    assert found.outcomes[0].outcome == WITHIN


# ---------------------------------------------------------------------------
# A committed slice names the property and compares nothing
# ---------------------------------------------------------------------------


def test_a_committed_slice_names_a_rank_correlation_and_compares_nothing() -> None:
    from flux_engine.concordance.boundary.slice import SliceReference, commit_state

    compiled = _compiled(
        {"property": "mean", "column": "a", "tolerance": {"type": "absolute", "value": "500"}},
        KENDALL,
    )
    rows = _rows(_correlated(40, 1))
    state = commit_state(
        rows=rows,
        compiled=compiled,
        ordinal=1,
        committed_at="2026-01-01T00:00:00.000Z",
        blinding=bytes(range(32)),
        slice_reference=SliceReference(
            uri="file:///slice.ndjson", first_line=1, last_line=len(rows)
        ),
    )
    assert sorted(state.statistic.properties) == ["000:mean:a"]
    evaluation = ClearStatistic(compiled=compiled, committed=state, synthetic_rows=rows).evaluate()
    assert evaluation.not_compared == ("001:rankCorrelation:a+b",)
    assert [o.property_id for o in evaluation.outcomes] == ["000:mean:a"]
    assert evaluation.within


# ---------------------------------------------------------------------------
# The scorecard row and the format it names
# ---------------------------------------------------------------------------


def _scorecard(compiled: CompiledFidelity, evaluation: Any, alphas: Any = None) -> Any:
    return build_scorecard(
        run_id="0" * 16,
        plan_digest="sha256:" + "0" * 64,
        run_digest="sha256:" + "1" * 64,
        profile_digest="sha256:" + "2" * 64,
        tables=[TableInput(expose_id=EXPOSE, compiled=compiled, evaluation=evaluation)],
        copula_alphas=alphas,
    )


def test_a_scorecard_with_a_rank_correlation_names_1_5_0_and_carries_the_row() -> None:
    pairs = _correlated(80, 6)
    compiled = _compiled(KENDALL)
    evaluation = evaluate(compiled, _profile(exact_kendall(pairs)), _rows(pairs))
    document = _scorecard(compiled, evaluation, {EXPOSE: "0.05"}).to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_5_0
    (row,) = document["tables"][0]["properties"]
    assert (row["property"], row["statistic"], row["dimension"]) == (
        "rankCorrelation",
        "kendallTau/1",
        "joint",
    )
    assert row["columns"] == ["a", "b"] and row["outcome"] == "within" and row["score"] == "1"
    assert row["rank"]["alpha"] == "0.05" and row["rank"]["measure"] == "kendall"
    assert row["rank"]["distance"] == "0" and row["rank"]["tooNoisyToTell"] is False
    assert "noise" not in row and "widening" not in row
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "docs"
            / "spec"
            / "scorecard-1-5.schema.json"
        ).read_text("utf-8")
    )
    Draft202012Validator(schema).validate(document)


def test_a_noisy_rank_row_carries_the_noise_member_and_validates() -> None:
    pairs = _correlated(80, 6)
    compiled = _compiled(SPEARMAN)
    evaluation = evaluate(compiled, _profile(noisy("spearmanRho/1", "0.2", "0.1")), _rows(pairs))
    document = _scorecard(compiled, evaluation).to_document()
    (row,) = document["tables"][0]["properties"]
    assert row["noise"]["mechanism"] == "gaussian" and row["noise"]["cellVariance"] == "0.01"
    assert "alpha" not in row["rank"]
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs" / "spec" / "scorecard-1-5.schema.json"
        ).read_text("utf-8")
    )
    Draft202012Validator(schema).validate(document)


def test_a_scorecard_without_the_new_rows_keeps_the_format_it_named_before() -> None:
    compiled = _compiled(
        {"property": "mean", "column": "a", "tolerance": {"type": "absolute", "value": "500"}}
    )
    mean = {
        "definition": "mean/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "k": 3, "Sx": str(300 << GRID_BITS), "status": "F"}],
    }
    document = _profile()
    document["tables"][EXPOSE]["columns"]["a"]["statistics"] = [mean]
    evaluation = evaluate(compiled, document, _rows([(100, 1), (100, 2), (100, 3)]))
    assert evaluation.within
    assert _scorecard(compiled, evaluation).to_document()["_schemaURL"] == SCHEMA_URL


def test_every_group_of_a_rank_property_is_one_group_named_by_the_empty_string() -> None:
    pairs = _correlated(40, 1)
    found = evaluate(_compiled(KENDALL), _profile(exact_kendall(pairs)), _rows(pairs)).outcomes[0]
    assert [(g.group, g.committed_count, g.synthetic_count) for g in found.groups] == [("", 40, 40)]
    assert isinstance(found.groups[0].score, Fraction)
    assert math.isfinite(float(found.groups[0].score))


def test_a_score_row_of_a_rank_correlation_counts_in_the_joint_dimension() -> None:
    pairs = _correlated(80, 6)
    compiled = _compiled(KENDALL)
    evaluation = evaluate(compiled, _profile(exact_kendall(pairs)), _rows(pairs))
    card = _scorecard(compiled, evaluation)
    joint = dict(card.dimensions)["joint"]
    assert (joint.properties, joint.scored, joint.score) == (1, 1, Fraction(1))
    assert isinstance(card.tables[0].properties[0], PropertyScore)
