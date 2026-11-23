# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A tolerance that declares ``sampling: true`` widens by the run's row count (ADR 0016 section 8).

The world is one categorical column of four levels with the shares 4:3:2:1, and a profile that
holds its exact counts. A run draws ``n`` rows from the world, so its shares differ from the
profile's by sampling alone.
"""

from __future__ import annotations

import copy
import json
import random
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.scorecard import (
    SCHEMA_URL,
    SCHEMA_URL_1_4_0,
    TableInput,
    build_scorecard,
)
from flux_engine.concordance.verifier import ClearProfile, _exact_allocation, sampling_margin
from flux_stats.noise import sampling_widening
from flux_stats.profile import SCHEMA_URL as PROFILE_SCHEMA_URL
from flux_stats.profile import parse_profile
from tests.concordance.conftest import build_compiled

pytestmark = pytest.mark.unit

EXPOSE = "kinds"
LEVELS = ["a", "b", "c", "d"]
SHARES = [4, 3, 2, 1]
SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"


def _document(**tolerance: Any) -> Dict[str, Any]:
    spelled = {"type": "distance", "measure": "totalVariation", "max": "0.10", **tolerance}
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.private.kinds",
            "exposes": [
                {
                    "exposeId": EXPOSE,
                    "kind": "stream",
                    "contract": {"schema": [{"name": "kind", "type": "STRING", "required": True}]},
                }
            ],
        },
        "emit": {"productRef": "test.private.kinds", "exposeId": EXPOSE, "fluidVersion": "^0.7.3"},
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 2, "max": 1_000_000},
                "columns": {
                    "kind": {"support": {"values": LEVELS}, "maxCardinality": 4, "null": "fail"}
                },
                "properties": [
                    {"property": "levelHistogram", "column": "kind", "tolerance": spelled}
                ],
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE, "columns": ["kind"]},
    }


def _profile() -> Dict[str, Any]:
    counts = [share * 1000 for share in SHARES]
    return {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": PROFILE_SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            EXPOSE: {
                "rowCount": sum(counts),
                "columns": {
                    "kind": {
                        "domain": "category",
                        "statistics": [
                            {
                                "definition": "levelHistogram/1",
                                "sampleSize": {"min": 1},
                                "disclosure": "none",
                                "groups": [
                                    {
                                        "group": "",
                                        "k": sum(counts),
                                        "levelCounts": counts,
                                        "status": "F",
                                    }
                                ],
                                "levels": LEVELS,
                            }
                        ],
                    }
                },
                "columnPairs": [],
            }
        },
    }


def _rows(counts: List[int]) -> List[Dict[str, Any]]:
    return [
        {"kind": level} for level, count in zip(LEVELS, counts, strict=True) for _ in range(count)
    ]


def _drawn(seed: int, rows: int) -> List[Dict[str, Any]]:
    generator = random.Random(seed)
    return [{"kind": generator.choices(LEVELS, weights=SHARES)[0]} for _ in range(rows)]


def _compiled(**tolerance: Any) -> CompiledFidelity:
    return build_compiled(_document(**tolerance))


def _evaluate(compiled: CompiledFidelity, rows: List[Dict[str, Any]]) -> Any:
    profile = parse_profile(copy.deepcopy(_profile()))
    return ClearProfile(compiled=compiled, profile=profile, synthetic_rows=rows).evaluate()


# 26 rows whose shares (14, 6, 4, 2 of 26) are 0.138 from the world's in total variation.
SMALL_RUN = _rows([14, 6, 4, 2])


def test_a_small_table_fails_a_fixed_tolerance_and_passes_a_sampling_one() -> None:
    fixed = _evaluate(_compiled(), SMALL_RUN)
    assert not fixed.within
    assert fixed.outcomes[0].sampling is None
    sampled = _evaluate(_compiled(sampling=True), SMALL_RUN)
    assert sampled.within
    expected = sampling_widening(4, 26, Fraction(4, 10))
    assert sampled.outcomes[0].sampling == expected
    assert sampled.outcomes[0].groups[0].sampling == expected


def test_a_tolerance_without_the_key_is_compared_exactly_as_before() -> None:
    plain = _evaluate(_compiled(), SMALL_RUN)
    false = _evaluate(_compiled(sampling=False), SMALL_RUN)
    assert [o.outcome for o in plain.outcomes] == [o.outcome for o in false.outcomes]
    assert plain.outcomes[0].sampling is false.outcomes[0].sampling is None
    assert plain.outcomes[0].margin == false.outcomes[0].margin


def test_the_widening_follows_the_row_count() -> None:
    compiled = _compiled(sampling=True)
    widths = [_evaluate(compiled, _drawn(1, rows)).outcomes[0].sampling for rows in (26, 100, 1000)]
    assert widths[0] > widths[1] > widths[2] > 0


def _hand_declared(allocation: str | None) -> Any:
    column = SimpleNamespace(**({} if allocation is None else {"allocation": allocation}))
    return SimpleNamespace(columns={"kind": column})


def test_exact_allocation_leaves_only_the_rounding_term() -> None:
    compiled = _compiled(sampling=True)
    reference = parse_profile(copy.deepcopy(_profile())).tables[EXPOSE].columns["kind"]
    group = reference.statistics[0].groups[0].statistic
    synthetic = group  # 4 cells, with the world's shares, as the run's counts
    assert sampling_margin(_hand_declared("exact"), compiled.properties[0], group, synthetic) == (
        sampling_widening(4, group.k, Fraction(4, 10), exact_allocation=True)
    )
    assert sampling_margin(_hand_declared("exact"), compiled.properties[0], group, synthetic) == (
        Fraction(4, 2 * group.k)
    )
    # Without the declaration, or with another value, the sampling variance is there.
    for allocation in (None, "other"):
        wide = sampling_margin(_hand_declared(allocation), compiled.properties[0], group, synthetic)
        assert wide == sampling_widening(4, group.k, Fraction(4, 10))
        assert wide > Fraction(4, 2 * group.k)


def _pair_block(**columns: Any) -> Any:
    """A block whose columns are ``(allocation, given)`` pairs."""
    return SimpleNamespace(
        columns={
            name: SimpleNamespace(allocation=allocation, given=given)
            for name, (allocation, given) in columns.items()
        }
    )


def _property(*names: str, group_by: str | None = None) -> Any:
    return SimpleNamespace(columns=names, group_by=group_by)


def test_a_property_over_one_exact_column_is_exact() -> None:
    block = _pair_block(a=("exact", ()), b=(None, ()))
    assert _exact_allocation(block, _property("a"))
    assert not _exact_allocation(block, _property("b"))
    assert not _exact_allocation(block, _property("missing"))


def test_a_pair_is_exact_when_one_is_exact_and_given_the_other_that_is_exact() -> None:
    block = _pair_block(parent=("exact", ()), child=("exact", ("parent",)))
    assert _exact_allocation(block, _property("child", "parent"))
    assert _exact_allocation(block, _property("parent", "child"))
    assert _exact_allocation(block, _property("child", group_by="parent"))


def test_two_columns_that_are_each_exact_alone_are_not_exact_as_a_pair() -> None:
    block = _pair_block(a=("exact", ()), b=("exact", ()))
    assert _exact_allocation(block, _property("a")) and _exact_allocation(block, _property("b"))
    assert not _exact_allocation(block, _property("a", "b"))
    assert not _exact_allocation(block, _property("a", group_by="b"))


def test_a_given_pair_that_is_not_exact_keeps_the_sampling_widening() -> None:
    for columns in (
        {"parent": (None, ()), "child": ("exact", ("parent",))},
        {"parent": ("exact", ()), "child": (None, ("parent",))},
        {"parent": ("exact", ()), "child": ("exact", ("other",))},
        {"parent": ("exact", ()), "child": ("exact", ("parent", "other"))},
    ):
        assert not _exact_allocation(_pair_block(**columns), _property("child", "parent"))


def test_no_sampling_member_leaves_a_tolerance_without_the_key_alone() -> None:
    compiled = _compiled()
    reference = parse_profile(copy.deepcopy(_profile())).tables[EXPOSE].columns["kind"]
    group = reference.statistics[0].groups[0].statistic
    assert sampling_margin(_hand_declared("exact"), compiled.properties[0], group, group) is None


def _scorecard(compiled: CompiledFidelity, rows: List[Dict[str, Any]]) -> Any:
    digest = "sha256:" + "a" * 64
    profile = parse_profile(copy.deepcopy(_profile()))
    evaluation = ClearProfile(compiled=compiled, profile=profile, synthetic_rows=rows).evaluate()
    return build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=digest,
        run_digest=digest,
        profile_digest=digest,
        tables=[TableInput(expose_id=EXPOSE, compiled=compiled, evaluation=evaluation)],
        profile=profile,
    )


def test_a_scorecard_without_the_key_names_1_2_0_and_has_no_widening_member() -> None:
    document = _scorecard(_compiled(), SMALL_RUN).to_document()
    assert document["_schemaURL"] == SCHEMA_URL
    assert all("widening" not in row for row in document["tables"][0]["properties"])
    schema = json.loads((SPEC / "scorecard-1-2.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert b'"widening"' not in _scorecard(_compiled(), SMALL_RUN).to_bytes()


def test_a_scorecard_with_the_member_names_1_4_0_and_validates_against_its_schema() -> None:
    scorecard = _scorecard(_compiled(sampling=True), SMALL_RUN)
    document = scorecard.to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    (row,) = document["tables"][0]["properties"]
    assert Fraction(row["widening"]["sampling"]) == sampling_widening(4, 26, Fraction(4, 10))
    assert row["tolerance"]["sampling"] == "true"
    schema = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"] == SCHEMA_URL_1_4_0
    Draft202012Validator(schema).validate(document)
    assert scorecard.to_bytes() == _scorecard(_compiled(sampling=True), SMALL_RUN).to_bytes()


def test_a_run_of_100_rows_from_the_world_passes_a_sampling_tolerance_over_a_seed_sweep() -> None:
    """Measured at 100 rows, 4 levels, margin 0.10, over seeds 0 to 199.

    The sampling tolerance (widening 0.673 on top of 0.10) passed 200 of 200 runs. The same
    margin with no widening passed 160 of 200, so the widening is what makes the check hold. The
    bound is for the Gaussian approximation of the multinomial; this is the pass rate at a small
    row count it is held to, asserted with a margin (at least 190 of 200).
    """
    sampled = _compiled(sampling=True)
    fixed = _compiled()
    passed = sum(_evaluate(sampled, _drawn(seed, 100)).within for seed in range(200))
    fixed_passed = sum(_evaluate(fixed, _drawn(seed, 100)).within for seed in range(200))
    assert passed >= 190, f"{passed} of 200 passed"
    assert fixed_passed <= 175, f"{fixed_passed} of 200 passed without it"


def test_a_run_from_another_world_still_fails_a_sampling_tolerance_at_enough_rows() -> None:
    reversed_world = _rows([1000, 2000, 3000, 4000])
    assert not _evaluate(_compiled(sampling=True), reversed_world).within
