# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seven statistics the fidelity check compares, one definition each.

SEAM-FIDELITY-SPEC §4 lists the quantities ``S`` keeps per property and
group, and §5 the integer comparison each property kind is judged by. Each
definition below is one row of that pair: the quantities a kind keeps, the
pure function that forms them, their canonical spelling, and the
comparison, registered under one identifier.

What every measure here receives is already read: values encoded onto the
grid (:meth:`flux_stats.fixedpoint.Grid.quantise`), level slots, NFC
category strings, or encoded pairs, in row order, after the null policy
and the grouping. Reading a record -- a column absent, a null under
``fail``, a value outside its range or its declared support -- is the
reader's job and its refusals are the reader's (the engine's is
:mod:`flux_engine.concordance.boundary.statistic`), because they are
decided by a declaration this module never sees. What is left here is
integer arithmetic, so a profile and a check that read the same values
form the same numbers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, Sequence, Tuple

from flux_stats.errors import FluxError
from flux_stats.fixedpoint import (
    FLX_5004,
    FLX_5005,
    correlation_within,
    mean_within_absolute,
    mean_within_relative,
    order_statistic_index,
    quantile_bin_within,
    quantile_rank_ok,
    quantile_within,
    support_within,
    total_variation_within,
)
from flux_stats.registry import (
    Check,
    GroupStatistic,
    Parameters,
    StatisticDefinition,
    register,
)

__all__ = [
    "CORRELATION",
    "LEVEL_HISTOGRAM",
    "MARGINAL_HISTOGRAM",
    "MEAN",
    "QUANTILE",
    "QUANTILE_BINNED",
    "SUPPORT",
    "bin_index",
    "level_order",
    "non_member_records",
]


# ---------------------------------------------------------------------------
# The cell rules both sides share
# ---------------------------------------------------------------------------


def bin_index(units: int, edges: Sequence[int]) -> int:
    """The bin a grid value falls in, given encoded edges.

    Bins are half-open ``[e_i, e_{i+1})`` except the last, which is closed
    at the top so a record exactly at the declared maximum is binned
    rather than dropped. Edges partition the declared range (§2) and a
    value outside it has already been refused, so the answer always
    exists.
    """
    last = len(edges) - 2
    for index in range(last + 1):
        if units < edges[index + 1]:
            return index
    return last


def level_order(levels: Iterable[str]) -> Tuple[str, ...]:
    """The order a level histogram counts its declared levels in.

    Sorted by code point, the order "the sorted committed support" uses.
    The levels arrive NFC-normalised and distinct, as the declaration's
    reader makes them, so the order is total and a cell is one level.
    """
    return tuple(sorted(levels))


# ---------------------------------------------------------------------------
# mean
# ---------------------------------------------------------------------------


def _measure_mean(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    return GroupStatistic(group=group, k=len(values), sum_x=sum(values))


def _spell_mean(state: GroupStatistic) -> Dict[str, Any]:
    return {"Sx": str(state.sum_x)}


def _compare_mean(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    if check.tolerance == "absolute":
        assert check.margin_units is not None
        return mean_within_absolute(
            sum_a=synthetic.sum_x,
            count_a=synthetic.k,
            sum_b=reference.sum_x,
            count_b=reference.k,
            tolerance_units=check.margin_units,
        )
    assert check.margin is not None
    return mean_within_relative(
        sum_a=synthetic.sum_x,
        count_a=synthetic.k,
        sum_b=reference.sum_x,
        count_b=reference.k,
        tolerance=check.margin,
    )


MEAN = register(
    StatisticDefinition(
        identifier="mean/1",
        kind="mean",
        summary="k and Sx, the sum of the k encoded values of one column",
        measure=_measure_mean,
        spell=_spell_mean,
        compare=_compare_mean,
        on_grid=True,
        cell_table=False,
    )
)


# ---------------------------------------------------------------------------
# marginalHistogram and levelHistogram: one count per declared cell
# ---------------------------------------------------------------------------


def _measure_bins(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    edges = parameters.edges
    counts = [0] * (len(edges) - 1)
    for units in values:
        counts[bin_index(units, edges)] += 1
    return GroupStatistic(group=group, k=len(values), bin_counts=tuple(counts))


def _spell_bins(state: GroupStatistic) -> Dict[str, Any]:
    return {"binCounts": list(state.bin_counts)}


def _measure_levels(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    counts = [0] * parameters.level_count
    for slot in values:
        counts[slot] += 1
    return GroupStatistic(group=group, k=len(values), level_counts=tuple(counts))


def _spell_levels(state: GroupStatistic) -> Dict[str, Any]:
    return {"levelCounts": list(state.level_counts)}


def _total_variation(
    kind: str, synthetic: GroupStatistic, reference: GroupStatistic, check: Check
) -> bool:
    # One inequality over the declared cells: a histogram's bins or a level
    # histogram's levels, which both sides count in the same order. A value
    # with no cell was refused while either side was read.
    assert check.margin is not None
    return total_variation_within(
        counts_a=synthetic.cell_counts(kind),
        count_a=synthetic.k,
        counts_b=reference.cell_counts(kind),
        count_b=reference.k,
        tolerance=check.margin,
    )


def _compare_bins(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    return _total_variation(MARGINAL_HISTOGRAM.kind, synthetic, reference, check)


def _compare_levels(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    return _total_variation(LEVEL_HISTOGRAM.kind, synthetic, reference, check)


MARGINAL_HISTOGRAM = register(
    StatisticDefinition(
        identifier="marginalHistogram/1",
        kind="marginalHistogram",
        summary="k and one record count per declared bin, on the encoded bin edges",
        measure=_measure_bins,
        spell=_spell_bins,
        compare=_compare_bins,
        on_grid=True,
        cell_table=True,
    )
)

LEVEL_HISTOGRAM = register(
    StatisticDefinition(
        identifier="levelHistogram/1",
        kind="levelHistogram",
        summary="k and one record count per declared level, in level_order",
        measure=_measure_levels,
        spell=_spell_levels,
        compare=_compare_levels,
        on_grid=False,
        cell_table=True,
    )
)


# ---------------------------------------------------------------------------
# support
# ---------------------------------------------------------------------------


def _measure_support(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    members: Dict[str, int] = {}
    for category in values:
        members[category] = members.get(category, 0) + 1
    support = tuple(sorted(members))
    return GroupStatistic(
        group=group,
        k=len(values),
        support=support,
        support_counts=tuple(members[value] for value in support),
    )


def _spell_support(state: GroupStatistic) -> Dict[str, Any]:
    return {"support": list(state.support)}


def non_member_records(synthetic: GroupStatistic, reference: GroupStatistic) -> int:
    """How many of *synthetic*'s records hold a value *reference*'s support lacks.

    §5's "count of non-members" is counted in RECORDS, not in distinct
    strange values. Counting distinct values would let a dataset every one
    of whose records carries one undeclared level count as a single
    non-member and pass under any ``max >= 1``, so the tolerance would
    bound the size of the strange set rather than how much of the dataset
    is strange. The comparison below reads this one count, and so does
    flux-sim-engine's scorecard (its similarity for ``support/1``).
    """
    members = set(reference.support)
    return sum(
        occurrences
        for value, occurrences in zip(synthetic.support, synthetic.support_counts, strict=True)
        if value not in members
    )


def _compare_support(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    assert check.max_non_members is not None
    return support_within(
        non_member_count=non_member_records(synthetic, reference),
        max_non_members=check.max_non_members,
    )


SUPPORT = register(
    StatisticDefinition(
        identifier="support/1",
        kind="support",
        summary="k and the sorted distinct members of one categorical column",
        measure=_measure_support,
        spell=_spell_support,
        compare=_compare_support,
        on_grid=False,
        cell_table=False,
    )
)


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------


def _measure_moments(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    xs = [x for x, _ in values]
    ys = [y for _, y in values]
    return GroupStatistic(
        group=group,
        k=len(xs),
        sum_x=sum(xs),
        sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs),
        sum_yy=sum(y * y for y in ys),
        sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
    )


def _spell_moments(state: GroupStatistic) -> Dict[str, Any]:
    return {
        "Sx": str(state.sum_x),
        "Sy": str(state.sum_y),
        "Sxx": str(state.sum_xx),
        "Sxy": str(state.sum_xy),
        "Syy": str(state.sum_yy),
    }


def _compare_moments(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    assert check.margin is not None and check.min_variance is not None
    return correlation_within(
        moments_a=synthetic.moments,
        moments_b=reference.moments,
        tolerance=check.margin,
        min_variance=check.min_variance,
    )


CORRELATION = register(
    StatisticDefinition(
        identifier="correlation/1",
        kind="correlation",
        summary="k and the encoded sums Sx, Sy, Sxx, Sxy and Syy of one column pair",
        measure=_measure_moments,
        spell=_spell_moments,
        compare=_compare_moments,
        on_grid=True,
        cell_table=False,
    )
)


# ---------------------------------------------------------------------------
# quantile
# ---------------------------------------------------------------------------


def _measure_quantile(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    if not values:
        return GroupStatistic(group=group, k=0)
    assert parameters.level is not None  # guaranteed by the predicate parser
    ordered = sorted(values)
    index = order_statistic_index(len(ordered), parameters.level)
    witness = ordered[index - 1]
    below = sum(1 for units in ordered if units < witness)
    at_or_below = sum(1 for units in ordered if units <= witness)
    return GroupStatistic(
        group=group,
        k=len(ordered),
        quantile_witness=witness,
        quantile_below=below,
        quantile_at_or_below=at_or_below,
    )


def _spell_quantile(state: GroupStatistic) -> Dict[str, Any]:
    return {
        "quantileWitness": (
            str(state.quantile_witness) if state.quantile_witness is not None else None
        ),
        "quantileBelow": state.quantile_below,
        "quantileAtOrBelow": state.quantile_at_or_below,
    }


def _compare_quantile(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    assert check.level is not None and check.margin_units is not None
    for side, statistic in ((check.reference_side, reference), ("synthetic", synthetic)):
        if statistic.quantile_witness is None:
            raise FluxError(FLX_5005, f"{side} side offers no quantile witness for {check.subject}")
        if not quantile_rank_ok(
            count=statistic.k,
            level=check.level,
            count_strictly_below=statistic.quantile_below,
            count_at_or_below=statistic.quantile_at_or_below,
        ):
            raise FluxError(
                FLX_5005,
                f"the {side} side's quantile witness for {check.subject} does not "
                "carry the ranks the order statistic requires",
            )
    assert reference.quantile_witness is not None and synthetic.quantile_witness is not None
    return quantile_within(
        witness_a=synthetic.quantile_witness,
        witness_b=reference.quantile_witness,
        tolerance_units=check.margin_units,
    )


QUANTILE = register(
    StatisticDefinition(
        identifier="quantile/1",
        kind="quantile",
        summary="k, the encoded order statistic at the declared level and its two ranks",
        measure=_measure_quantile,
        spell=_spell_quantile,
        compare=_compare_quantile,
        on_grid=True,
        cell_table=False,
    )
)


# ---------------------------------------------------------------------------
# quantile with bins
# ---------------------------------------------------------------------------


def _measure_quantile_binned(
    group: str, values: Sequence[Any], parameters: Parameters
) -> GroupStatistic:
    """The order statistic as :func:`_measure_quantile` takes it, and the bin that holds it.

    ``bin_below`` is the records below the bin's lower edge and
    ``bin_through`` the records below its upper edge, or all of them for the
    last bin, which is closed at the top (ADR 0012). The witness and its two
    ranks stay in the group: the engine forms the synthetic side's own, and
    :func:`_spell_quantile_binned` leaves them out of every spelling.
    """
    state = _measure_quantile(group, values, parameters)
    witness = state.quantile_witness
    if witness is None:
        return state
    edges = parameters.edges
    if len(edges) < 2:
        raise FluxError(FLX_5004, "a quantile with bins needs at least two encoded edges")
    index = bin_index(witness, edges)
    last = index == len(edges) - 2
    return replace(
        state,
        quantile_bin=index,
        bin_below=sum(1 for units in values if units < edges[index]),
        bin_through=state.k if last else sum(1 for units in values if units < edges[index + 1]),
    )


def _spell_quantile_binned(state: GroupStatistic) -> Dict[str, Any]:
    return {
        "quantileBin": state.quantile_bin,
        "binBelow": state.bin_below,
        "binThrough": state.bin_through,
    }


def _compare_quantile_binned(
    synthetic: GroupStatistic, reference: GroupStatistic, check: Check
) -> bool:
    """The synthetic witness against the bin the reference side publishes (ADR 0012).

    The reference side offers a bin, and its two counts show that the order
    statistic lies in it: ``binBelow < j <= binThrough`` is the order-statistic
    rule of ``quantile/1`` read at the bin's edges. The synthetic side is the
    engine's own data and offers its witness with its own ranks, as
    ``quantile/1`` requires. The property is within when the witness lies
    within the margin of the bin.
    """
    assert check.level is not None and check.margin_units is not None
    side = check.reference_side
    if reference.quantile_bin is None:
        raise FluxError(FLX_5005, f"{side} side offers no quantile bin for {check.subject}")
    if not 0 <= reference.quantile_bin < len(check.edges) - 1:
        raise FluxError(
            FLX_5005,
            f"the {side} side's quantile bin for {check.subject} names no declared bin",
        )
    if not quantile_rank_ok(
        count=reference.k,
        level=check.level,
        count_strictly_below=reference.bin_below,
        count_at_or_below=reference.bin_through,
    ):
        raise FluxError(
            FLX_5005,
            f"the {side} side's quantile bin for {check.subject} does not "
            "carry the ranks the order statistic requires",
        )
    if synthetic.quantile_witness is None:
        raise FluxError(FLX_5005, f"synthetic side offers no quantile witness for {check.subject}")
    if not quantile_rank_ok(
        count=synthetic.k,
        level=check.level,
        count_strictly_below=synthetic.quantile_below,
        count_at_or_below=synthetic.quantile_at_or_below,
    ):
        raise FluxError(
            FLX_5005,
            f"the synthetic side's quantile witness for {check.subject} does not "
            "carry the ranks the order statistic requires",
        )
    return quantile_bin_within(
        witness=synthetic.quantile_witness,
        edges=check.edges,
        bin_index=reference.quantile_bin,
        tolerance_units=check.margin_units,
    )


QUANTILE_BINNED = register(
    StatisticDefinition(
        identifier="quantile/2",
        kind="quantile",
        summary="k, the bin of the declared bins that holds the order statistic at the "
        "declared level, and the records below and through that bin",
        measure=_measure_quantile_binned,
        spell=_spell_quantile_binned,
        compare=_compare_quantile_binned,
        on_grid=True,
        cell_table=False,
        binned=True,
    )
)
