# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile comparison read from a sample of whole blocks of an emit's records.

docs/spec/SAMPLE-SPEC.md. For a large run, ``verify --sample`` reads a
fraction of each emit instead of every record: the emit is cut into blocks
of consecutive records, the stand-in for a partition until emits have
partitions, and whole blocks are chosen without replacement by ranking a
hash of the run, the emit, the seed and the block. Only the chosen blocks
are decoded.

Each ``mean/1`` group is estimated from the chosen blocks with the one-stage
cluster ratio estimator (:mod:`flux_stats.sampling`) and carries its 95 %
interval. The interval decides the group against the property's band, the
profile's mean plus or minus the compiled margin: ``within`` when the
interval lies inside the band, ``outside`` when it lies wholly outside it,
and ``undecided`` when it crosses an edge, which only a larger sample or
the whole emit can settle.

A ``marginalHistogram/1`` group is the share of its records in each bin, each
share a ratio of two totals with its own 95 % interval. The shares and the
profile's own, bin by bin, bound the total variation distance between them:
from below by the distance each profile share lies outside its interval, from
above by the farthest end of each interval. The group is ``within`` when the
upper bound is inside the compiled margin, ``outside`` when the lower bound is
past it, and ``undecided`` between. A ``quantile/2`` group (a quantile with
bins) is decided by the two shares its test reads: the records below the
reference bin's lower edge less the margin, and the records at or below its
upper edge plus the margin, each a ratio with its interval, against the level.
The quantile lies inside the margin of the bin exactly when the first share is
below the level and the second at or above it, so the group is ``within`` when
both intervals say so, ``outside`` when either says the opposite, and
``undecided`` otherwise. A sample of every block gives the full comparison's
own answer for each. The other property kinds have no sampled estimator yet: a
sampled check names them as not estimated and compares nothing for them.

Values are read exactly as the full comparison reads them
(:mod:`flux_stats.reading`): the same null policy, the same range refusal,
the same grid. A block's group is its total and its record count, so a
group that misses a block is a block of zero records, as a domain
estimate wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.concordance.verifier import ClearProfile, Refusal
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError
from flux_stats.definitions import bin_index
from flux_stats.fidelity import definition_for_property
from flux_stats.fixedpoint import FLX_5005, quantile_rank_ok, require_sample
from flux_stats.noise import widening
from flux_stats.profile import Profile
from flux_stats.reading import encode_value, group_keys, read_column
from flux_stats.registry import GroupStatistic
from flux_stats.sampling import LEVEL, RatioEstimate, ratio_estimate
from flux_stats.spelling import render_exact

__all__ = [
    "DEFAULT_BLOCK_RECORDS",
    "OUTSIDE",
    "UNDECIDED",
    "WITHIN",
    "CENSORED",
    "SampleRequest",
    "SampledEvaluation",
    "SampledGroup",
    "SampledProperty",
    "choose_blocks",
    "evaluate_sample",
]

#: Records per block when the request names none.
DEFAULT_BLOCK_RECORDS = 1000

WITHIN = "within"
OUTSIDE = "outside"
UNDECIDED = "undecided"
#: A group the profile withheld: nothing to compare it with.
CENSORED = "censored"


@dataclass(frozen=True)
class SampleRequest:
    """What ``verify --sample`` asks for: a fraction of blocks of a given size, and a seed."""

    fraction: Fraction
    block_records: int = DEFAULT_BLOCK_RECORDS
    seed: int = 0

    def __post_init__(self) -> None:
        if not 0 < self.fraction <= 1:
            raise ValueError("the sampled fraction must lie in (0, 1]")
        if self.block_records < 1:
            raise ValueError("a block holds at least one record")
        if self.seed < 0:
            raise ValueError("the seed is a whole number not below 0")

    def to_document(self) -> Dict[str, Any]:
        return {
            "fraction": render_exact(self.fraction),
            "blockRecords": self.block_records,
            "seed": self.seed,
        }


def choose_blocks(records: int, request: SampleRequest, key: str) -> Tuple[int, Tuple[int, ...]]:
    """The number of blocks *records* make, and the ones chosen, in order.

    ``ceil(fraction * blocks)`` of them, and never fewer than two, which a
    variance needs; a sample that would take every block reads them all.
    The choice ranks ``sha256(key, seed, block)``, so the same run, emit and
    seed always read the same blocks, and every set of that size is as
    likely as any other.
    """
    blocks = -(-records // request.block_records)
    if blocks == 0:
        return 0, ()
    wanted = -(-request.fraction.numerator * blocks // request.fraction.denominator)
    count = min(blocks, max(2, wanted))
    ranked = sorted(
        range(blocks),
        key=lambda index: sha256_hex(f"{key}\x00{request.seed}\x00{index}".encode("utf-8")),
    )
    return blocks, tuple(sorted(ranked[:count]))


@dataclass(frozen=True)
class SampledGroup:
    """One group of a sampled ``mean/1`` property: its estimate, interval and outcome."""

    ordinal: int
    outcome: str
    estimate: Optional[RatioEstimate] = None
    scale: int = 1
    reason: Optional[str] = None
    #: A histogram group's share of records in each bin, in bin order.
    shares: Tuple[RatioEstimate, ...] = ()
    #: A binned quantile group's two shares: the records below the reference bin's lower
    #: reach, and the records at or below its upper reach.
    ranks: Tuple[RatioEstimate, ...] = ()
    #: A histogram group's bounds on the total variation distance from the profile.
    distance: Optional[Tuple[Fraction, Fraction]] = None

    def to_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"group": self.ordinal, "outcome": self.outcome}
        found = self.estimate
        if found is not None:
            document.update(
                estimate=render_exact(found.estimate / self.scale),
                interval=[
                    render_exact(found.low / self.scale),
                    render_exact(found.high / self.scale),
                ],
                level=render_exact(LEVEL),
                blocks=found.blocks,
                records=found.records,
            )
        if self.shares:
            document["shares"] = [_interval_document(share) for share in self.shares]
        if self.ranks:
            document["ranks"] = {
                "below": _interval_document(self.ranks[0]),
                "through": _interval_document(self.ranks[1]),
            }
        counted = (self.shares or self.ranks)[:1]
        if counted:
            document.update(
                level=render_exact(LEVEL), blocks=counted[0].blocks, records=counted[0].records
            )
        if self.distance is not None:
            document["distance"] = [render_exact(end) for end in self.distance]
        if self.reason is not None:
            document["reason"] = self.reason
        return document


def _interval_document(found: RatioEstimate) -> Dict[str, Any]:
    return {
        "estimate": render_exact(found.estimate),
        "interval": [render_exact(found.low), render_exact(found.high)],
    }


@dataclass(frozen=True)
class SampledProperty:
    """One ``mean/1`` property estimated on the sample, group by group."""

    property_id: str
    columns: Tuple[str, ...]
    groups: Tuple[SampledGroup, ...]
    #: ``mean``, ``histogram`` or ``quantile``; a mean's document names no kind.
    kind: str = "mean"

    @property
    def outcome(self) -> str:
        """``outside`` if any group is, else ``undecided`` if any is, else ``within``."""
        decided = [group.outcome for group in self.groups if group.outcome != CENSORED]
        if not decided:
            return CENSORED
        for outcome in (OUTSIDE, UNDECIDED):
            if outcome in decided:
                return outcome
        return WITHIN

    def to_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "propertyId": self.property_id,
            "columns": list(self.columns),
            "outcome": self.outcome,
            "groups": [group.to_document() for group in self.groups],
        }
        if self.kind != "mean":
            document["kind"] = self.kind
        return document


@dataclass(frozen=True)
class SampledEvaluation:
    """One emit compared with the profile on a sample of its blocks."""

    expose_id: str
    blocks: int
    blocks_read: int
    records: int
    records_read: int
    properties: Tuple[SampledProperty, ...] = ()
    #: The properties of a kind no sampled estimator covers yet.
    not_estimated: Tuple[str, ...] = ()
    refusals: Tuple[Refusal, ...] = ()

    @property
    def status(self) -> str:
        """``failed``, ``passed``, or ``not checked`` when nothing was estimated."""
        outcomes = [entry.outcome for entry in self.properties]
        if self.refusals or OUTSIDE in outcomes or UNDECIDED in outcomes:
            return "failed"
        return "passed" if WITHIN in outcomes else "not checked"

    def to_document(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "blocks": {"read": self.blocks_read, "of": self.blocks},
            "records": {"read": self.records_read, "of": self.records},
            "properties": [entry.to_document() for entry in self.properties],
            "notEstimated": list(self.not_estimated),
            "refusals": [refusal.to_document() for refusal in self.refusals],
        }

    def detail(self) -> str:
        """One line: what was read, and what the estimates found."""
        counts = {
            outcome: sum(1 for entry in self.properties if entry.outcome == outcome)
            for outcome in (WITHIN, OUTSIDE, UNDECIDED)
        }
        labels = [
            label
            for kind, label in (
                ("mean", "means"),
                ("histogram", "histograms"),
                ("quantile", "quantile bins"),
            )
            if any(entry.kind == kind for entry in self.properties)
        ] or ["means"]
        noun = ", ".join(labels[:-1]) + " and " + labels[-1] if len(labels) > 1 else labels[0]
        text = (
            f"sampled {self.blocks_read} of {self.blocks} blocks ({self.records_read} of "
            f"{self.records} records): {noun} {counts[WITHIN]} within, {counts[OUTSIDE]} outside, "
            f"{counts[UNDECIDED]} undecided by the 95 % interval"
        )
        if self.not_estimated:
            text += "; not estimated: " + ", ".join(self.not_estimated)
        if self.refusals:
            count = len(self.refusals)
            text += f"; {count} fail-closed condition{'' if count == 1 else 's'} met"
        return text


def _block_values(
    first_row: int,
    rows: Sequence[Mapping[str, Any]],
    entry: CompiledProperty,
    compiled: CompiledFidelity,
) -> Dict[str, List[int]]:
    """Each group's encoded values over one block's *rows*, in row order.

    The block starts at the emit's record *first_row*, so a refusal names a
    row as the full comparison names it.
    """
    declaration = entry.declaration
    block = compiled.block
    column = block.columns[declaration.columns[0]]
    grouping = block.columns[declaration.group_by] if declaration.group_by is not None else None
    read = partial(encode_value, column=column, grid=block.encoding.grid)
    return {
        key: read_column(rows, indices, column, read, first_row=first_row)
        for key, indices in group_keys(rows, grouping, first_row=first_row)
    }


def _band(
    entry: CompiledProperty, reference_sum: int, reference_count: int
) -> Tuple[Fraction, Fraction]:
    """The profile's mean and the half-width a run's mean may stray from it, in grid units."""
    centre = Fraction(reference_sum, reference_count)
    if entry.declaration.tolerance.kind.value == "absolute":
        assert entry.margin_units is not None
        return centre, Fraction(entry.margin_units)
    assert entry.margin is not None
    if reference_sum == 0:
        raise FluxError(FLX_5005, "relative comparison against a reference sum of zero")
    return centre, Fraction(entry.margin) * abs(centre)


def _decide(estimate: RatioEstimate, centre: Fraction, half: Fraction) -> str:
    if centre - half <= estimate.low and estimate.high <= centre + half:
        return WITHIN
    if estimate.high < centre - half or estimate.low > centre + half:
        return OUTSIDE
    return UNDECIDED


def _require_groups(
    entry: CompiledProperty,
    per_block: Sequence[Mapping[str, Tuple[int, int]]],
    profiled: Set[str],
    floor: int,
    whole: bool,
) -> None:
    """Refuse (``FLX-5005``) groups the full comparison would refuse, as far as a sample can tell.

    A sample of every block (*whole*) holds the run's groups to their floor
    and the two sides to one set of groups. A partial sample can only show
    a group the profile does not have.
    """
    seen = sorted({key for totals in per_block for key in totals})
    if not whole:
        if not profiled.issuperset(seen):
            raise FluxError(
                FLX_5005,
                f"the sample holds {len(set(seen) - profiled)} group(s) the profile does not",
            )
        return
    grouped = entry.declaration.group_by is not None
    for ordinal, key in enumerate(seen):
        count = sum(totals.get(key, (0, 0))[1] for totals in per_block)
        where = f" group #{ordinal}" if grouped else ""
        require_sample(
            count, minimum=floor, subject=f"synthetic dataset: {entry.property_id}{where}"
        )
    if set(seen) != profiled:
        raise FluxError(
            FLX_5005,
            f"the two sides group into different sets: {len(profiled)} in the profile and "
            f"{len(seen)} synthetic",
        )


def _sampled_property(
    entry: CompiledProperty,
    compiled: CompiledFidelity,
    profile: Profile,
    blocks: Mapping[int, Sequence[Mapping[str, Any]]],
    population: int,
) -> SampledProperty:
    """One property on the sample, held to the full comparison's floors.

    A mean, a marginal histogram or a quantile with bins. A group's floor is
    ``records.min`` for a grouped property and 1 for an ungrouped one. The
    profile's groups are held to it as the full comparison holds them. The run's
    groups are held to it when every block was read; on a partial sample a group
    whose chosen blocks hold fewer records than its floor is undecided, since the
    blocks not read may hold the rest. A sample of every block holds exactly the
    profile's groups, as the full comparison's two sides must.
    """
    table = profile.tables[compiled.block.expose_id]
    reference = ClearProfile(compiled=compiled, profile=profile, synthetic_rows=())._reference(
        entry, table
    )
    per_block = [
        _block_values(first_row, rows, entry, compiled)
        for first_row, rows in sorted(blocks.items())
    ]
    counted = [{key: (0, len(values)) for key, values in found.items()} for found in per_block]
    grouped = entry.declaration.group_by is not None
    floor = compiled.block.records.minimum if grouped else 1
    profiled = {group.key for group in reference.groups}
    _require_groups(entry, counted, profiled, floor, whole=len(blocks) == population)
    scale = compiled.block.encoding.grid.scale
    kind = _sampled_kind(entry)
    groups: List[SampledGroup] = []
    for ordinal, group in enumerate(sorted(reference.groups, key=lambda found: found.key)):
        if not group.published or group.statistic is None:
            groups.append(SampledGroup(ordinal=ordinal, outcome=CENSORED))
            continue
        where = f"{entry.property_id} group #{ordinal}" if grouped else entry.property_id
        require_sample(group.statistic.k, minimum=floor, subject=f"profile: {where}")
        held_values = [found.get(group.key, []) for found in per_block]
        held = sum(len(values) for values in held_values)
        if held < floor:
            reason = (
                "no record of this group in the chosen blocks"
                if held == 0
                else f"the chosen blocks hold {held} record(s) of this group, below its floor"
            )
            groups.append(SampledGroup(ordinal=ordinal, outcome=UNDECIDED, reason=reason))
            continue
        if kind == "mean":
            groups.append(
                _mean_group(ordinal, entry, group.statistic, held_values, population, scale)
            )
        elif kind == "histogram":
            groups.append(
                _histogram_group(
                    ordinal,
                    entry,
                    compiled,
                    group.statistic,
                    held_values,
                    population,
                    reference.cell_variance,
                    sum(group.noisy, Fraction(0)) if group.noisy else None,
                )
            )
        else:
            groups.append(
                _quantile_group(ordinal, entry, compiled, group.statistic, held_values, population)
            )
    return SampledProperty(
        property_id=entry.property_id,
        columns=tuple(entry.declaration.columns),
        groups=tuple(groups),
        kind=kind,
    )


def _sampled_kind(entry: CompiledProperty) -> str:
    """The kind of estimate a property takes on a sample, or ``""`` for one that takes none."""
    kind = entry.declaration.kind
    if kind is PropertyKind.MEAN:
        return "mean"
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        return "histogram"
    if kind is PropertyKind.QUANTILE and definition_for_property(entry.declaration).binned:
        return "quantile"
    return ""


def _mean_group(
    ordinal: int,
    entry: CompiledProperty,
    reference: GroupStatistic,
    held_values: Sequence[Sequence[int]],
    population: int,
    scale: int,
) -> SampledGroup:
    centre, half = _band(entry, reference.sum_x, reference.k)
    pairs = [(sum(values), len(values)) for values in held_values]
    found = ratio_estimate(pairs, population)
    return SampledGroup(
        ordinal=ordinal, outcome=_decide(found, centre, half), estimate=found, scale=scale
    )


def _histogram_group(
    ordinal: int,
    entry: CompiledProperty,
    compiled: CompiledFidelity,
    reference: GroupStatistic,
    held_values: Sequence[Sequence[int]],
    population: int,
    cell_variance: Optional[Fraction] = None,
    total: Optional[Fraction] = None,
) -> SampledGroup:
    """A histogram group: each bin's share with its interval, and bounds on the distance from the profile.

    A noisy reference (*cell_variance* is its cells' variance) widens the margin the distance is
    decided against, as the full comparison does (SCORECARD-SPEC.md section 9).
    """
    assert entry.margin is not None
    grid = compiled.block.encoding.grid
    edges = tuple(grid.quantise(edge, subject="bin edge") for edge in entry.declaration.bins)
    bins = len(edges) - 1
    if len(reference.bin_counts) != bins or bins < 1:
        raise FluxError(
            FLX_5005, f"the profile's bins for {entry.property_id} are not the declared ones"
        )
    per_block = []
    for values in held_values:
        counts = [0] * bins
        for units in values:
            counts[bin_index(units, edges)] += 1
        per_block.append((counts, len(values)))
    shares = tuple(
        ratio_estimate([(counts[index], total) for counts, total in per_block], population)
        for index in range(bins)
    )
    lower = upper = Fraction(0)
    for share, count in zip(shares, reference.bin_counts, strict=True):
        profile_share = Fraction(count, reference.k)
        low, high = max(share.low, Fraction(0)), min(share.high, Fraction(1))
        lower += max(Fraction(0), low - profile_share, profile_share - high)
        upper += max(abs(low - profile_share), abs(high - profile_share))
    lower, upper = lower / 2, upper / 2
    margin = Fraction(entry.margin)
    if cell_variance is not None:
        reach = total if total is not None else Fraction(reference.k)
        if reach <= 0 or reference.k <= 0:
            raise FluxError(
                FLX_5005,
                "a noisy statistic's cells add up to no records above zero, so no tolerance can "
                "be widened over it",
            )
        margin += widening(cell_variance, bins, reach, reference.k)
    outcome = WITHIN if upper <= margin else OUTSIDE if lower > margin else UNDECIDED
    return SampledGroup(ordinal=ordinal, outcome=outcome, shares=shares, distance=(lower, upper))


def _quantile_group(
    ordinal: int,
    entry: CompiledProperty,
    compiled: CompiledFidelity,
    reference: GroupStatistic,
    held_values: Sequence[Sequence[int]],
    population: int,
) -> SampledGroup:
    """A quantile-with-bins group, decided by the two shares its rank test reads.

    The witness lies within the margin of the profile's bin ``[e_i - m, t_i + m]``, with
    ``t_i`` the bin's largest grid value, exactly when the records below ``e_i - m`` are fewer
    than the level's order statistic and the records at or below ``t_i + m`` reach it: the
    first share is below the level, the second at or above it.
    """
    assert entry.margin_units is not None and entry.declaration.level is not None
    side = "profile"
    grid = compiled.block.encoding.grid
    edges = tuple(grid.quantise(edge, subject="bin edge") for edge in entry.declaration.bins)
    index = reference.quantile_bin
    if index is None:
        raise FluxError(FLX_5005, f"{side} side offers no quantile bin for {entry.property_id}")
    if not 0 <= index < len(edges) - 1:
        raise FluxError(
            FLX_5005,
            f"the {side} side's quantile bin for {entry.property_id} names no declared bin",
        )
    level = Fraction(entry.declaration.level)
    if not quantile_rank_ok(
        count=reference.k,
        level=level,
        count_strictly_below=reference.bin_below,
        count_at_or_below=reference.bin_through,
    ):
        raise FluxError(
            FLX_5005,
            f"the {side} side's quantile bin for {entry.property_id} does not carry the ranks "
            "the order statistic requires",
        )
    low = edges[index] - entry.margin_units
    top = (edges[-1] if index == len(edges) - 2 else edges[index + 1] - 1) + entry.margin_units
    below = [(sum(1 for units in values if units < low), len(values)) for values in held_values]
    through = [(sum(1 for units in values if units <= top), len(values)) for values in held_values]
    first = ratio_estimate(below, population)
    second = ratio_estimate(through, population)
    if first.high < level and second.low >= level:
        outcome = WITHIN
    elif first.low >= level or second.high < level:
        outcome = OUTSIDE
    else:
        outcome = UNDECIDED
    return SampledGroup(ordinal=ordinal, outcome=outcome, ranks=(first, second))


def evaluate_sample(
    compiled: CompiledFidelity,
    profile: Profile,
    blocks: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    population: int,
    records: int,
) -> SampledEvaluation:
    """Compare one emit's means, histograms and quantile bins with the profile on the chosen *blocks*.

    *blocks* maps each chosen block's first record, its index in the emit,
    to its rows; *population* is the emit's number of blocks and *records*
    its number of records. The profile's row count and the emit's record
    count are checked against the block's declared ``records`` as the full
    comparison checks them, and a property that cannot be read refuses that
    property alone.
    """
    block = compiled.block
    read = sum(len(rows) for rows in blocks.values())
    base: Dict[str, Any] = {
        "expose_id": block.expose_id,
        "blocks": population,
        "blocks_read": len(blocks),
        "records": records,
        "records_read": read,
    }
    table = profile.tables.get(block.expose_id)
    if table is None:
        refusal = Refusal(code="FLX-5037", reason="the profile holds no table for this expose")
        return SampledEvaluation(**base, refusals=(refusal,))
    try:
        if table.row_count is not None:
            require_sample(
                table.row_count,
                minimum=block.records.minimum,
                maximum=block.records.maximum,
                subject="profile",
            )
        require_sample(
            records,
            minimum=block.records.minimum,
            maximum=block.records.maximum,
            subject="synthetic dataset",
        )
    except FluxError as error:
        return SampledEvaluation(**base, refusals=(Refusal(error.code.code, error.detail),))
    properties: List[SampledProperty] = []
    not_estimated: List[str] = []
    refusals: List[Refusal] = []
    for entry in compiled.properties:
        if not _sampled_kind(entry):
            not_estimated.append(entry.property_id)
            continue
        try:
            properties.append(_sampled_property(entry, compiled, profile, blocks, population))
        except FluxError as error:
            refusals.append(
                Refusal(code=error.code.code, reason=error.detail, property_id=entry.property_id)
            )
    return SampledEvaluation(
        **base,
        properties=tuple(properties),
        not_estimated=tuple(not_estimated),
        refusals=tuple(refusals),
    )
