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
the whole emit can settle. The other property kinds have no sampled
estimator yet: a sampled check names them as not estimated and compares
nothing for them.

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
from flux_stats.fixedpoint import FLX_5005, require_sample
from flux_stats.profile import Profile
from flux_stats.reading import encode_value, group_keys, read_column
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
        if self.reason is not None:
            document["reason"] = self.reason
        return document


@dataclass(frozen=True)
class SampledProperty:
    """One ``mean/1`` property estimated on the sample, group by group."""

    property_id: str
    columns: Tuple[str, ...]
    groups: Tuple[SampledGroup, ...]

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
        return {
            "propertyId": self.property_id,
            "columns": list(self.columns),
            "outcome": self.outcome,
            "groups": [group.to_document() for group in self.groups],
        }


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
        text = (
            f"sampled {self.blocks_read} of {self.blocks} blocks ({self.records_read} of "
            f"{self.records} records): means {counts[WITHIN]} within, {counts[OUTSIDE]} outside, "
            f"{counts[UNDECIDED]} undecided by the 95 % interval"
        )
        if self.not_estimated:
            text += "; not estimated: " + ", ".join(self.not_estimated)
        if self.refusals:
            count = len(self.refusals)
            text += f"; {count} fail-closed condition{'' if count == 1 else 's'} met"
        return text


def _block_totals(
    first_row: int,
    rows: Sequence[Mapping[str, Any]],
    entry: CompiledProperty,
    compiled: CompiledFidelity,
) -> Dict[str, Tuple[int, int]]:
    """Each group's total of encoded values and record count over one block's *rows*.

    The block starts at the emit's record *first_row*, so a refusal names a
    row as the full comparison names it.
    """
    declaration = entry.declaration
    block = compiled.block
    column = block.columns[declaration.columns[0]]
    grouping = block.columns[declaration.group_by] if declaration.group_by is not None else None
    read = partial(encode_value, column=column, grid=block.encoding.grid)
    totals: Dict[str, Tuple[int, int]] = {}
    for key, indices in group_keys(rows, grouping, first_row=first_row):
        values = read_column(rows, indices, column, read, first_row=first_row)
        totals[key] = (sum(values), len(values))
    return totals


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
    """One ``mean/1`` property on the sample, held to the full comparison's floors.

    A group's floor is ``records.min`` for a grouped property and 1 for an
    ungrouped one. The profile's groups are held to it as the full
    comparison holds them. The run's groups are held to it when every block
    was read; on a partial sample a group whose chosen blocks hold fewer
    records than its floor is undecided, since the blocks not read may hold
    the rest. A sample of every block holds exactly the profile's groups, as
    the full comparison's two sides must.
    """
    table = profile.tables[compiled.block.expose_id]
    reference = ClearProfile(compiled=compiled, profile=profile, synthetic_rows=())._reference(
        entry, table
    )
    per_block = [
        _block_totals(first_row, rows, entry, compiled)
        for first_row, rows in sorted(blocks.items())
    ]
    grouped = entry.declaration.group_by is not None
    floor = compiled.block.records.minimum if grouped else 1
    profiled = {group.key for group in reference.groups}
    _require_groups(entry, per_block, profiled, floor, whole=len(blocks) == population)
    scale = compiled.block.encoding.grid.scale
    groups: List[SampledGroup] = []
    for ordinal, group in enumerate(sorted(reference.groups, key=lambda found: found.key)):
        if not group.published or group.statistic is None:
            groups.append(SampledGroup(ordinal=ordinal, outcome=CENSORED))
            continue
        where = f"{entry.property_id} group #{ordinal}" if grouped else entry.property_id
        require_sample(group.statistic.k, minimum=floor, subject=f"profile: {where}")
        centre, half = _band(entry, group.statistic.sum_x, group.statistic.k)
        pairs = [totals.get(group.key, (0, 0)) for totals in per_block]
        held = sum(count for _, count in pairs)
        if held < floor:
            reason = (
                "no record of this group in the chosen blocks"
                if held == 0
                else f"the chosen blocks hold {held} record(s) of this group, below its floor"
            )
            groups.append(SampledGroup(ordinal=ordinal, outcome=UNDECIDED, reason=reason))
            continue
        found = ratio_estimate(pairs, population)
        groups.append(
            SampledGroup(
                ordinal=ordinal, outcome=_decide(found, centre, half), estimate=found, scale=scale
            )
        )
    return SampledProperty(
        property_id=entry.property_id,
        columns=tuple(entry.declaration.columns),
        groups=tuple(groups),
    )


def evaluate_sample(
    compiled: CompiledFidelity,
    profile: Profile,
    blocks: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    population: int,
    records: int,
) -> SampledEvaluation:
    """Compare one emit's ``mean/1`` properties with the profile on the chosen *blocks*.

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
        if entry.declaration.kind is not PropertyKind.MEAN:
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
