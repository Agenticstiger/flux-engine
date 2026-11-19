# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile comparison on a sample of whole blocks (docs/spec/SAMPLE-SPEC.md).

The block choice is pinned as a seeded, repeatable choice of the size the
fraction asks for. The comparison is pinned on the fidelity vectors' committed
slice and a profile of those same records: a sample of every block is exact and
finds every ``mean/1`` group within, a profile mean moved past the margin is
outside, a group the profile withheld is censored, the other kinds are named as
not estimated, and a partial sample's interval is the estimator's over the
blocks' own totals. The full comparison's fail-closed floors hold on a sample
too, and a refusal names a row as the emit numbers it. Over a hundred seeds, a
5 % sample of hash-chosen blocks covers each full mean at least ninety times.
"""

from __future__ import annotations

import random
from dataclasses import replace
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional

import pytest

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.predicate import PropertyKind, RecordsRange
from flux_engine.concordance.sampling import (
    CENSORED,
    OUTSIDE,
    UNDECIDED,
    WITHIN,
    SampleRequest,
    choose_blocks,
    evaluate_sample,
)
from flux_engine.concordance.verifier import ClearProfile
from flux_stats.profile import parse_profile
from flux_stats.spelling import render_exact
from tests.stats.profiles import edited, profile_from_rows, statistic_of

pytestmark = pytest.mark.unit

EXPOSE = "recovery_moment"
GROUPED = "003:mean:amount@market"


def _blocks(rows: List[Dict[str, Any]], size: int) -> Dict[int, List[Dict[str, Any]]]:
    """Every block of *rows*, keyed by its first record, as verify hands them over."""
    return {start: rows[start : start + size] for start in range(0, len(rows), size)}


def _evaluate(
    compiled: CompiledFidelity,
    document: Mapping[str, Any],
    rows: List[Dict[str, Any]],
    size: int,
    chosen: Optional[List[int]] = None,
) -> Any:
    every = _blocks(rows, size)
    picked = every if chosen is None else {start: every[start] for start in chosen}
    return evaluate_sample(
        compiled, parse_profile(document), picked, population=len(every), records=len(rows)
    )


def _refusal_of(found: Any, property_id: str) -> Any:
    return next(refusal for refusal in found.refusals if refusal.property_id == property_id)


def _relabelled(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """*rows* with one ``uk`` record moved to ``ie``: the ``uk`` group falls below its floor."""
    moved = [dict(row) for row in rows]
    first_uk = next(row for row in moved if row["market"] == "uk")
    first_uk["market"] = "ie"
    return moved


# ---------------------------------------------------------------------------
# The block choice
# ---------------------------------------------------------------------------


def test_the_choice_is_seeded_repeatable_and_of_the_size_asked_for() -> None:
    request = SampleRequest(fraction=Fraction(5, 100), block_records=10)
    total, chosen = choose_blocks(10_000, request, "run/expose")
    assert total == 1000 and len(chosen) == 50 and list(chosen) == sorted(set(chosen))
    assert choose_blocks(10_000, request, "run/expose") == (total, chosen)
    other = SampleRequest(fraction=Fraction(5, 100), block_records=10, seed=1)
    assert choose_blocks(10_000, other, "run/expose")[1] != chosen
    assert choose_blocks(10_000, request, "run/other")[1] != chosen


def test_a_choice_takes_at_least_two_blocks_and_never_more_than_there_are() -> None:
    small = SampleRequest(fraction=Fraction(1, 1000), block_records=10)
    assert len(choose_blocks(100, small, "k")[1]) == 2
    everything = SampleRequest(fraction=Fraction(1), block_records=10)
    assert choose_blocks(95, everything, "k") == (10, tuple(range(10)))
    assert choose_blocks(0, small, "k") == (0, ())


@pytest.mark.parametrize(
    "fraction, block_records, seed",
    [
        (Fraction(0), 10, 0),
        (Fraction(3, 2), 10, 0),
        (Fraction(1, 2), 0, 0),
        (Fraction(1, 2), 10, -1),
    ],
)
def test_a_request_outside_its_domain_is_refused(
    fraction: Fraction, block_records: int, seed: int
) -> None:
    with pytest.raises(ValueError):
        SampleRequest(fraction=fraction, block_records=block_records, seed=seed)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def test_a_sample_of_every_block_is_exact_and_finds_every_mean_within(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    found = _evaluate(compiled, document, committed_rows, size=4)
    means = [entry for entry in compiled.properties if entry.declaration.kind.value == "mean"]
    assert [entry.property_id for entry in found.properties] == [
        entry.property_id for entry in means
    ]
    assert {entry.outcome for entry in found.properties} == {WITHIN}
    for entry in found.properties:
        for group in entry.groups:
            assert group.estimate is not None and group.estimate.exact
    others = {entry.property_id for entry in compiled.properties} - {e.property_id for e in means}
    assert set(found.not_estimated) == others
    assert found.status == "passed"


def test_a_profile_mean_moved_past_the_margin_is_outside(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = edited(profile_from_rows(committed_rows, compiled))
    (group,) = statistic_of(document, EXPOSE, "amount", "mean/1")["groups"]
    group["Sx"] = str(int(group["Sx"]) + 10_000 * (1 << 16) * group["k"])
    found = _evaluate(compiled, document, committed_rows, size=4)
    amount = next(entry for entry in found.properties if entry.property_id == "000:mean:amount")
    assert amount.outcome == OUTSIDE
    assert found.status == "failed"


def test_a_partial_sample_carries_the_estimators_interval(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    from flux_stats.sampling import ratio_estimate

    document = profile_from_rows(committed_rows, compiled)
    every = _blocks(committed_rows, 4)
    chosen = sorted(every)[: max(2, len(every) // 2)]
    found = _evaluate(compiled, document, committed_rows, size=4, chosen=chosen)
    amount = next(entry for entry in found.properties if entry.property_id == "000:mean:amount")
    (group,) = amount.groups
    scale = compiled.block.encoding.grid.scale
    totals = []
    for index in chosen:
        values = [
            int(Fraction(str(row["amount"])) * scale)
            for row in every[index]
            if row["amount"] is not None
        ]
        totals.append((sum(values), len(values)))
    expected = ratio_estimate(totals, len(every))
    assert group.estimate == expected
    assert group.outcome in (WITHIN, UNDECIDED)
    assert found.blocks_read == len(chosen) and found.blocks == len(every)
    document_form = group.to_document()
    assert document_form["blocks"] == len(chosen)
    assert document_form["level"] == "0.95"


def test_a_group_the_profile_withheld_is_censored(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = edited(profile_from_rows(committed_rows, compiled))
    statistic = statistic_of(document, EXPOSE, "amount", "mean/1")
    statistic["groups"] = [{"group": "", "status": "A"}]
    found = _evaluate(compiled, document, committed_rows, size=4)
    amount = next(entry for entry in found.properties if entry.property_id == "000:mean:amount")
    assert amount.outcome == CENSORED


def test_a_record_count_outside_the_declared_records_refuses_the_emit(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    every = _blocks(committed_rows, 4)
    found = evaluate_sample(
        compiled, parse_profile(document), every, population=len(every), records=10**9
    )
    assert found.status == "failed" and found.refusals and not found.properties


# ---------------------------------------------------------------------------
# The full comparison's fail-closed rules, on a sample
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k, sum_x", [(1, None), (0, "0")])
def test_a_profile_group_below_its_floor_refuses_the_property_as_the_full_comparison_does(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]], k: int, sum_x: Optional[str]
) -> None:
    document = edited(profile_from_rows(committed_rows, compiled))
    statistic = next(
        found
        for found in document["tables"][EXPOSE]["columns"]["amount"]["statistics"]
        if found.get("groupBy") == "market" and found["definition"] == "mean/1"
    )
    group = statistic["groups"][0]
    mean = Fraction(int(group["Sx"]), group["k"])
    group["k"] = k
    group["Sx"] = sum_x if sum_x is not None else str(int(mean * k))
    full = ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=committed_rows
    ).evaluate()
    assert any(refusal.property_id == GROUPED for refusal in full.refusals)
    found = _evaluate(compiled, document, committed_rows, size=4)
    refusal = _refusal_of(found, GROUPED)
    assert refusal.code == "FLX-5005" and f"profile: {GROUPED} group #0" in refusal.reason
    assert found.status == "failed"


def test_a_sample_of_every_block_holds_the_runs_groups_to_their_floor(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    rows = _relabelled(committed_rows)
    full = ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=rows
    ).evaluate()
    assert any(refusal.property_id == GROUPED for refusal in full.refusals)
    found = _evaluate(compiled, document, rows, size=4)
    refusal = _refusal_of(found, GROUPED)
    assert f"synthetic dataset: {GROUPED} group #1" in refusal.reason
    assert found.status == "failed"


def test_a_partial_sample_below_a_groups_floor_leaves_it_undecided(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    found = _evaluate(compiled, document, committed_rows, size=2, chosen=[0, 2])
    grouped = next(entry for entry in found.properties if entry.property_id == GROUPED)
    assert grouped.outcome == UNDECIDED
    assert all(
        group.outcome == UNDECIDED and group.reason is not None and "floor" in group.reason
        for group in grouped.groups
    )


def test_a_sample_of_every_block_holds_exactly_the_profiles_groups(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    only_ie = [row for row in committed_rows if row["market"] == "ie"] * 2
    found = _evaluate(compiled, document, only_ie, size=4)
    assert "group into different sets" in _refusal_of(found, GROUPED).reason
    partial = _evaluate(compiled, document, only_ie, size=2, chosen=[0, 2])
    grouped = next(entry for entry in partial.properties if entry.property_id == GROUPED)
    (uk,) = [group for group in grouped.groups if group.ordinal == 1]
    assert uk.outcome == UNDECIDED and uk.reason == "no record of this group in the chosen blocks"


def test_a_profile_row_count_outside_the_declared_records_refuses_the_emit(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = edited(profile_from_rows(committed_rows, compiled))
    document["tables"][EXPOSE]["rowCount"] = 10**6
    found = _evaluate(compiled, document, committed_rows, size=4)
    assert found.status == "failed" and not found.properties
    assert found.refusals[0].code == "FLX-5005" and "profile" in found.refusals[0].reason


def test_a_refusal_names_the_row_as_the_emit_numbers_it(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    document = profile_from_rows(committed_rows, compiled)
    rows = [dict(row) for row in committed_rows]
    rows[6]["amount"] = "not a number"
    found = _evaluate(compiled, document, rows, size=4)
    assert "row 6" in _refusal_of(found, "000:mean:amount").reason


# ---------------------------------------------------------------------------
# Coverage, through the engine's own block choice and reading
# ---------------------------------------------------------------------------

BLOCKS = 100
BLOCK_RECORDS = 100


def _clustered_rows(compiled: CompiledFidelity, template: Mapping[str, Any]) -> List[Any]:
    """Blocks of records whose centre and market mix vary from block to block.

    The market's share varies between a third and two thirds. Where it varies
    more, a group's linearised interval covers a little less (SAMPLE-SPEC §6).
    """
    generator = random.Random(2026)
    scale = compiled.block.encoding.grid.scale
    interval = compiled.block.columns["amount"].interval
    low, high = int(interval.lo * scale), int(interval.hi * scale)
    rows: List[Any] = []
    for _ in range(BLOCKS):
        centre = generator.uniform(3.0, 6.0)
        share_ie = generator.uniform(0.35, 0.65)
        for _ in range(BLOCK_RECORDS):
            units = min(high, max(low, round((centre + generator.gauss(0, 1.2)) * scale)))
            market = "ie" if generator.random() < share_ie else "uk"
            rows.append(dict(template, amount=render_exact(Fraction(units, scale)), market=market))
    return rows


def test_a_five_percent_sample_of_chosen_blocks_covers_each_full_mean_ninety_times_in_a_hundred(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    means = tuple(
        entry for entry in compiled.properties if entry.declaration.kind is PropertyKind.MEAN
    )
    large = replace(
        compiled,
        block=replace(compiled.block, records=RecordsRange(minimum=4, maximum=10**6)),
        properties=means,
    )
    rows = _clustered_rows(large, committed_rows[0])
    profile = parse_profile(profile_from_rows(rows, large))
    table = profile.tables[EXPOSE]
    full: Dict[Any, Fraction] = {}
    for entry in means:
        reference = ClearProfile(compiled=large, profile=profile, synthetic_rows=())._reference(
            entry, table
        )
        for ordinal, group in enumerate(sorted(reference.groups, key=lambda found: found.key)):
            assert group.statistic is not None
            full[entry.property_id, ordinal] = Fraction(group.statistic.sum_x, group.statistic.k)
    covered = dict.fromkeys(full, 0)
    for seed in range(100):
        request = SampleRequest(fraction=Fraction(5, 100), block_records=BLOCK_RECORDS, seed=seed)
        total, chosen = choose_blocks(len(rows), request, f"run/{EXPOSE}")
        blocks = {
            index * BLOCK_RECORDS: rows[index * BLOCK_RECORDS : (index + 1) * BLOCK_RECORDS]
            for index in chosen
        }
        found = evaluate_sample(large, profile, blocks, population=total, records=len(rows))
        assert not found.refusals
        for entry in found.properties:
            for group in entry.groups:
                assert group.estimate is not None
                value = full[entry.property_id, group.ordinal]
                covered[entry.property_id, group.ordinal] += (
                    group.estimate.low <= value <= group.estimate.high
                )
    assert len(covered) == 3 and min(covered.values()) >= 90, covered
