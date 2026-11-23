# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The verifier -- SEAM-FIDELITY-SPEC §5.

    Inputs: the contract set (recomputes ``H``, ``G``), ``S``, ``C1``,
    ``C2``, and D′ (recomputes D′'s statistics on the same grid and D′'s
    digest = ``outputDigest``). Evaluates each property in the integer
    cross-multiplied form [...]. Fail-closed conditions: ``k = 0``, count
    outside ``records``, group below ``records.min``, a value outside its
    range or its declared support, a column absent, ``ε ≥ τ``. **No proof is generated in v0.1**;
    the ``ProofSystem`` port has one implementation, ``none``, and the
    record says so.

Three properties this module is shaped to keep.

**Nothing here divides.** Every comparison is one of
:mod:`flux_stats.fixedpoint`'s integer cross-multiplied forms, over
integers both sides formed with the same function on the same grid. There
is no floating point on the path and no place for one to appear.

**A refusal is not a verdict.** §5's fail-closed conditions become
:class:`Refusal` entries, and an evaluation carrying any refusal is not
within its margins whatever its property outcomes say. The two are
separate fields; nothing collapses them into a single boolean that could
be read as "the check ran and passed" when it did not run.

**The backend states what it is.** ``proofSystem`` is ``"none"`` and says
so in the record, because an omitted field reads as an unremarkable
default and a stated ``"none"`` reads as what it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.boundary.slice import CommittedState
from flux_engine.concordance.boundary.statistic import (
    GroupStatistic,
    Row,
    compute_property_statistic,
    level_cells,
    require_record_count,
)
from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty, render_exact
from flux_engine.concordance.errors import FLX_5011, FLX_5037
from flux_engine.concordance.predicate import (
    ColumnDeclaration,
    ColumnKind,
    PropertyKind,
    ValueDomain,
)
from flux_engine.concordance.rank import RankComparison, compare_rank, run_rank_value
from flux_engine.concordance.similarity import similarity
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import DigestRef, digest_of_stream
from flux_engine.core.errors import FluxError
from flux_stats import Check, StatisticDefinition, definition_for_property
from flux_stats.fixedpoint import FLX_5005, require_sample
from flux_stats.noise import sampling_widening, widening
from flux_stats.profile import (
    ColumnEntry,
    LinkGrouping,
    Profile,
    ProfileGroup,
    StatisticEntry,
    TableEntry,
)
from flux_stats.reading import MS_PER_DAY

__all__ = [
    "CENSORED",
    "CellCounts",
    "ClearProfile",
    "ClearStatistic",
    "EMIT_STREAM_PREFIX",
    "FidelityEvaluation",
    "GroupOutcome",
    "NoProofSystem",
    "Outcome",
    "PropertyOutcome",
    "Refusal",
    "emit_stream_digest",
]

#: The two words §6's ``properties[].outcome`` takes. A closed pair: there
#: is no third value meaning "could not tell", because that case is a
#: :class:`Refusal` and belongs in a different field.
Outcome = str
WITHIN: Outcome = "within"
OUTSIDE: Outcome = "outside"

#: Not scored. Only the profile path (:class:`ClearProfile`) says it, for a
#: group whose statistic the profile suppressed or whose every cell it
#: withheld, and for a property none of whose groups was scored: a
#: suppressed value is unknown, not zero, so it is left out of the result
#: rather than scored (ADR 0003, amended 2026-11-16).
#: It is not a refusal either: nothing failed closed. The slice path never
#: says it, so a committed state's record keeps §6's two words.
CENSORED: Outcome = "censored"

#: KERNEL-SPEC §8's framing label for the emit stream.
EMIT_STREAM_PREFIX = b"flux-emit/1\x00"


def _listed(names: Sequence[str]) -> str:
    """``groupBy``, ``groupBy and bins``, ``groupBy, bins and level``."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


@dataclass(frozen=True)
class NoProofSystem:
    """The v0.1 proof system: there is none, and the record says so."""

    name: str = "none"


# ---------------------------------------------------------------------------
# D-prime's digest
# ---------------------------------------------------------------------------


def emit_stream_digest(
    *,
    header: Mapping[str, Any],
    rows: Sequence[Sequence[Any]],
    subject: str,
) -> DigestRef:
    """Recompute ``outputDigest`` over an emit stream (KERNEL-SPEC §8).

        ``outputDigest = "sha256:" + hex(sha256(b"flux-emit/1\\x00" +
        JCS(header) + b"\\x00" + concat(line(row) for row in rows)))``

    with each line ``JCS([v1, v2, ...]) + b"\\n"`` -- arrays aligned to the
    header's column order, because JCS sorts object keys and objects would
    erase that order.

    The rule belongs to the seam (``seam/emit.py``), which computes the
    digest above the sink so the sink cannot influence it. It is
    implemented here because the gate has to recompute the digest "from
    the rows actually placed in the pack" (§7) and the seam module does
    not exist yet; :func:`~flux_engine.concordance.gate.run_gate` takes
    the recomputation as an injectable callable so the seam's own
    implementation replaces this one without a change at the call site.
    """
    body = EMIT_STREAM_PREFIX + canonical_bytes(header) + b"\x00"
    body += b"".join(canonical_bytes(list(row)) + b"\n" for row in rows)
    return digest_of_stream(body, subject=subject)


# ---------------------------------------------------------------------------
# Outcomes and refusals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellCounts:
    """One cell a histogram group was scored over: its label and the records each side put in it.

    ``profile`` is the profile's published count and ``run`` the run's, for the same cell. A
    cell the profile withheld is not here, as the score leaves it out.
    """

    label: str
    profile: int
    run: int


@dataclass(frozen=True)
class GroupOutcome:
    """One property's outcome inside one group.

    ``group`` is the empty string for an ungrouped property. The two
    counts travel with the outcome because they are what a reader needs to
    tell a comparison over a thousand records from one over the
    ``records.min`` floor.

    ``score`` is the group's similarity to its reference, in ``[0, 1]``
    (:mod:`flux_engine.concordance.similarity`). Only the profile path
    forms it, for a group it scored; the slice path, a censored group and a
    refused one leave it ``None``.
    """

    group: str
    committed_count: int
    synthetic_count: int
    outcome: Outcome
    score: Optional[Fraction] = None
    #: How much the group's margin was widened for the noise of its reference statistic
    #: (SCORECARD-SPEC.md section 9); ``None`` when the reference carries none.
    widening: Optional[Fraction] = None
    #: How much the group's margin was widened because the run sampled its rows (a distance
    #: tolerance that declares ``sampling: true``, ADR 0016 section 8); ``None`` without the key.
    sampling: Optional[Fraction] = None
    #: The cells of a histogram group that the score was computed over, for the evidence report
    #: to draw. Neither the scorecard nor the fidelity record reads them, and two outcomes that
    #: differ only here compare equal.
    cells: Tuple[CellCounts, ...] = field(default=(), compare=False, repr=False)

    @property
    def within(self) -> bool:
        """Whether this group's comparison landed inside the margin."""
        return self.outcome == WITHIN

    @property
    def censored(self) -> bool:
        """Whether the profile withheld this group, or each of its cells, so it was not scored."""
        return self.outcome == CENSORED


@dataclass(frozen=True)
class PropertyOutcome:
    """One declared property's outcome, as §6's record row carries it."""

    property_id: str
    kind: PropertyKind
    columns: Tuple[str, ...]
    group_by: Optional[str]
    tolerance: Mapping[str, str]
    margin: Optional[str]
    epsilon: str
    outcome: Outcome
    groups: Tuple[GroupOutcome, ...] = ()
    #: The variance of each cell of the profile statistic this property was compared with, when
    #: that statistic is noisy (format 1-2-0); ``None`` for an exact one.
    cell_variance: Optional[Fraction] = None
    #: The mechanism that statistic was released under (``gaussian``, ``discreteGaussian`` or
    #: ``laplace``); for a statistic formed from a clique, the clique's.
    noise_mechanism: Optional[str] = None
    #: A rank correlation's comparison (ADR 0035 decision 7): both values, the distance, the
    #: widenings and the verdict. ``None`` for every other kind.
    rank: Optional[RankComparison] = None

    @property
    def widening(self) -> Optional[Fraction]:
        """The largest widening any group's margin took, or ``None`` when the reference is exact."""
        found = [group.widening for group in self.groups if group.widening is not None]
        return max(found) if found else None

    @property
    def sampling(self) -> Optional[Fraction]:
        """The largest widening sampling gave any group's margin, or ``None`` without the key."""
        found = [group.sampling for group in self.groups if group.sampling is not None]
        return max(found) if found else None

    @property
    def within(self) -> bool:
        """Whether every group of this property landed inside the margin."""
        return self.outcome == WITHIN

    @property
    def censored(self) -> bool:
        """Whether every group of this property was censored, so none was scored."""
        return self.outcome == CENSORED

    def to_record_row(self) -> Dict[str, Any]:
        """The property row of the fidelity record (§6).

        Exactly §6's keys. A single-column property spells its column as
        ``column``; a correlation spells its pair as ``columns``. The
        per-group detail stays on the evaluation: §6 fixes one row per
        declared property, and adding a group list here would make a
        record that a reader of the specification cannot parse.
        """
        row: Dict[str, Any] = {"property": self.kind.value}
        if len(self.columns) == 1:
            row["column"] = self.columns[0]
        else:
            row["columns"] = list(self.columns)
        if self.group_by is not None:
            row["groupBy"] = self.group_by
        row["tolerance"] = dict(sorted(self.tolerance.items()))
        row["margin"] = self.margin
        row["epsilon"] = self.epsilon
        row["outcome"] = self.outcome
        return row


@dataclass(frozen=True)
class Refusal:
    """One fail-closed condition of §5, with the code that names it.

    ``reason`` names a column, a row index, a property or a count -- never
    a value (KERNEL-SPEC §9), because a refusal that quoted the record it
    tripped on would be a record leaving the boundary by another route.
    """

    code: str
    reason: str
    property_id: Optional[str] = None

    def to_document(self) -> Dict[str, Any]:
        """The canonical form carried in a gate decision."""
        return {
            "code": self.code,
            "reason": self.reason,
            "propertyId": self.property_id,
        }


@dataclass(frozen=True)
class FidelityEvaluation:
    """What the verifier found. Two fields, and no boolean that merges them."""

    expose_id: str
    outcomes: Tuple[PropertyOutcome, ...]
    refusals: Tuple[Refusal, ...] = ()
    synthetic_rows: int = 0
    #: The properties this comparison named and did not compare: a rank correlation against a
    #: committed slice, whose ``S`` and ``A4`` do not add up across blocks (ADR 0035 decision 7).
    not_compared: Tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        """Whether any fail-closed condition of §5 was met."""
        return bool(self.refusals)

    @property
    def within(self) -> bool:
        """Whether the check ran to completion and every property is within.

        A refusal makes this ``False`` whatever the outcomes say: §5's
        conditions are the cases where the verifier declines to answer,
        and an unanswered question is not an answer of yes.

        A censored property is left out, being neither within nor outside,
        but an evaluation whose every property was censored scored nothing
        and is not within. The slice path censors nothing, so there this is
        every property within.
        """
        scored = [entry for entry in self.outcomes if not entry.censored]
        return (
            not self.refusals
            and all(entry.within for entry in scored)
            and (bool(scored) or not self.outcomes)
        )

    @property
    def unscored(self) -> bool:
        """Whether the block declares no property, so nothing was compared and nothing is verified.

        Such an evaluation is not outside anything, and it is not a pass: a caller reports it
        as not scored. A refusal is a different answer, and keeps its own.
        """
        return not self.outcomes and not self.refusals

    @property
    def outside(self) -> Tuple[PropertyOutcome, ...]:
        """The properties that landed outside their compiled margin."""
        return tuple(entry for entry in self.outcomes if not entry.within and not entry.censored)

    @property
    def censored(self) -> Tuple[PropertyOutcome, ...]:
        """The properties the profile left nothing to score for."""
        return tuple(entry for entry in self.outcomes if entry.censored)


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


def _check_of(compiled: CompiledProperty) -> Check:
    """What one comparison of *compiled* needs: its margins and its name.

    The registered definitions (:mod:`flux_stats`) cannot import the
    compiler, so the verifier hands them this instead of the compiled
    property.
    """
    declaration = compiled.declaration
    return Check(
        subject=declaration.property_id,
        tolerance=declaration.tolerance.kind.value,
        margin=compiled.margin,
        margin_units=compiled.margin_units,
        max_non_members=compiled.max_non_members,
        min_variance=declaration.min_variance,
        level=declaration.level,
        # A quantile's bins are the edges the comparison reads (quantile/2);
        # a histogram's are read from the cells it counts, so it carries none.
        edges=compiled.edges if definition_for_property(declaration).binned else (),
    )


def _unscored(entry: CompiledProperty) -> PropertyOutcome:
    """One property's row before any group is compared: outside until shown within."""
    declaration = entry.declaration
    return PropertyOutcome(
        property_id=entry.property_id,
        kind=declaration.kind,
        columns=declaration.columns,
        group_by=declaration.group_by,
        tolerance=declaration.tolerance.declared,
        margin=render_exact(entry.margin) if entry.margin is not None else None,
        epsilon=render_exact(entry.epsilon),
        outcome=OUTSIDE,
    )


def _allocated(block: Any, name: str) -> bool:
    column = block.columns.get(name)
    return getattr(column, "allocation", None) == "exact"


def _exact_allocation(block: Any, declaration: Any) -> bool:
    """Whether the run's counts of a property's cells are exact, so only rounding widens them.

    A property over one column is exact when that column declares ``allocation: exact``. A
    property over two columns (the second a ``groupBy``, or a second column) is exact only when
    one of them is exact and declared ``given`` the other, which is itself exact: each pair of a
    level of one and a level of the other then holds its share (ADR 0016 section 6). Two columns
    that are each exact on their own are not exact as a pair. Anything else keeps the sampling
    variance. Read defensively: a declaration that carries no ``allocation`` allocates by sampling.
    """
    names = list(declaration.columns)
    if getattr(declaration, "group_by", None) is not None:
        names.append(declaration.group_by)
    unique = list(dict.fromkeys(names))
    if len(unique) == 1:
        return _allocated(block, unique[0])
    if len(unique) != 2:
        return False
    for child, parent in (unique, unique[::-1]):
        given = getattr(block.columns.get(child), "given", ())
        if _allocated(block, child) and _allocated(block, parent) and tuple(given) == (parent,):
            return True
    return False


def sampling_margin(
    block: Any,
    compiled: CompiledProperty,
    reference: GroupStatistic,
    synthetic: GroupStatistic,
) -> Optional[Fraction]:
    """How much sampling widens one group's margin, or ``None`` for a tolerance without the key.

    A distance tolerance that declares ``sampling: true`` is widened by
    :func:`flux_stats.noise.sampling_widening`, for the cells compared (``m``), the rows the run
    has in the group (``n``, the records of *synthetic*) and the largest share of the reference
    (``p_max``). For a column declared ``allocation: exact`` only the rounding term remains.
    """
    declaration = compiled.declaration
    if not declaration.tolerance.sampling:
        return None
    statistic = definition_for_property(declaration)
    if not statistic.cell_table:
        return None
    cells = reference.cell_counts(statistic.kind)
    total = sum(cells)
    if not cells or total <= 0 or synthetic.k < 1:
        return None
    return sampling_widening(
        len(cells),
        synthetic.k,
        Fraction(max(cells), total),
        exact_allocation=_exact_allocation(block, declaration),
    )


def _group_outcome(
    compiled: CompiledProperty,
    committed: GroupStatistic,
    synthetic: GroupStatistic,
    block: Any = None,
) -> Tuple[bool, Optional[Fraction]]:
    """Compare one group in the integer form §5 gives its kind; also its sampling widening.

    The comparison is the property's one registered definition, the same
    function whichever side the reference statistic came from.
    """
    statistic = definition_for_property(compiled.declaration)
    check = _check_of(compiled)
    widened = sampling_margin(block, compiled, committed, synthetic) if block is not None else None
    if widened is not None and check.margin is not None:
        check = replace(check, margin=check.margin + widened)
    return statistic.compare(synthetic, committed, check), widened


@dataclass(frozen=True)
class ClearStatistic:
    """The v0.1 verifier: it recomputes the aggregates and compares them.

    Holds what §5 says a verifier takes -- the compiled contract set, the
    committed state's published quantities, and D′'s rows -- and nothing
    that would let it see a committed record.

    Satisfies :class:`flux_engine.api.fidelity.FidelityVerifier`
    structurally: :attr:`proof_system` names the backend and
    :meth:`check` returns the claim the evaluation supports.
    """

    compiled: CompiledFidelity
    committed: CommittedState
    synthetic_rows: Sequence[Row]
    proof_system: NoProofSystem = NoProofSystem()

    def evaluate(self) -> FidelityEvaluation:
        """Re-evaluate every declared property, fail-closed.

        Recomputes ``G`` from the compiled contract set and refuses
        (``FLX-5011``) a committed state formed under a different one;
        recomputes D′'s statistics on the declared grid; then compares
        each property in its integer cross-multiplied form. Every
        fail-closed condition becomes a :class:`Refusal` rather than an
        exception, so one call reports every reason the check declined
        instead of only the first.
        """
        refusals: List[Refusal] = []
        if self.committed.g != self.compiled.g.value:
            refusals.append(
                Refusal(
                    code=FLX_5011.code,
                    reason=(
                        "the committed state was formed under different declarations; "
                        "a new committed state is required"
                    ),
                )
            )
        if self.committed.expose_id != self.compiled.block.expose_id:
            refusals.append(
                Refusal(
                    code=FLX_5005.code,
                    reason="the committed state answers for a different expose",
                )
            )
        if refusals:
            return FidelityEvaluation(
                expose_id=self.compiled.block.expose_id,
                outcomes=(),
                refusals=tuple(refusals),
                synthetic_rows=len(self.synthetic_rows),
            )

        try:
            require_record_count(
                self.synthetic_rows, self.compiled.block, subject="synthetic dataset"
            )
        except FluxError as error:
            return FidelityEvaluation(
                expose_id=self.compiled.block.expose_id,
                outcomes=(),
                refusals=(Refusal(code=error.code.code, reason=error.detail),),
                synthetic_rows=len(self.synthetic_rows),
            )

        outcomes: List[PropertyOutcome] = []
        not_compared: List[str] = []
        for entry in self.compiled.properties:
            if entry.declaration.kind is PropertyKind.RANK_CORRELATION:
                not_compared.append(entry.property_id)
                continue
            outcome, property_refusals = self._evaluate_property(entry)
            refusals.extend(property_refusals)
            outcomes.append(outcome)
        return FidelityEvaluation(
            expose_id=self.compiled.block.expose_id,
            outcomes=tuple(outcomes),
            refusals=tuple(refusals),
            synthetic_rows=len(self.synthetic_rows),
            not_compared=tuple(not_compared),
        )

    def _evaluate_property(self, entry: CompiledProperty) -> Tuple[PropertyOutcome, List[Refusal]]:
        """Compare one property, refusing only that property on a fail-closed.

        The synthetic side's quantities are formed here rather than for the
        whole dataset at once, so a value outside its range on one column
        refuses the properties over that column and leaves the rest
        answerable.
        """
        property_id = entry.property_id
        refusals: List[Refusal] = []
        base = _unscored(entry)
        committed_statistic = self.committed.statistic.properties.get(property_id)
        if committed_statistic is None:
            refusals.append(
                Refusal(
                    code=FLX_5005.code,
                    reason="the committed state carries no statistic for this property",
                    property_id=property_id,
                )
            )
            return base, refusals
        try:
            synthetic_statistic = compute_property_statistic(
                self.synthetic_rows,
                entry,
                self.compiled.block,
                subject="synthetic dataset",
            )
        except FluxError as error:
            refusals.append(
                Refusal(code=error.code.code, reason=error.detail, property_id=property_id)
            )
            return base, refusals

        committed_groups = {group.group: group for group in committed_statistic.groups}
        synthetic_groups = {group.group: group for group in synthetic_statistic.groups}
        if set(committed_groups) != set(synthetic_groups):
            # Naming the differing keys would name category values, which
            # KERNEL-SPEC §9 keeps out of diagnostics. The counts are
            # enough to act on.
            refusals.append(
                Refusal(
                    code=FLX_5005.code,
                    reason=(
                        f"the two sides group into different sets: "
                        f"{len(committed_groups)} committed and {len(synthetic_groups)} "
                        "synthetic"
                    ),
                    property_id=property_id,
                )
            )
            return base, refusals

        group_outcomes: List[GroupOutcome] = []
        for key in sorted(committed_groups):
            widened_by_sampling: Optional[Fraction] = None
            try:
                within, widened_by_sampling = _group_outcome(
                    entry, committed_groups[key], synthetic_groups[key], self.compiled.block
                )
            except FluxError as error:
                refusals.append(
                    Refusal(code=error.code.code, reason=error.detail, property_id=property_id)
                )
                within = False
            group_outcomes.append(
                GroupOutcome(
                    group=key,
                    committed_count=committed_groups[key].k,
                    synthetic_count=synthetic_groups[key].k,
                    outcome=WITHIN if within else OUTSIDE,
                    sampling=widened_by_sampling,
                )
            )
        every = all(group.within for group in group_outcomes)
        return (
            PropertyOutcome(
                property_id=base.property_id,
                kind=base.kind,
                columns=base.columns,
                group_by=base.group_by,
                tolerance=base.tolerance,
                margin=base.margin,
                epsilon=base.epsilon,
                outcome=WITHIN if every else OUTSIDE,
                groups=tuple(group_outcomes),
            ),
            refusals,
        )

    def check(self, statement: Any, attestation: Optional[bytes] = None) -> Any:
        """Re-evaluate *statement* and return the claim it supports.

        Implements :class:`flux_engine.api.fidelity.FidelityVerifier`.
        The claim's sentence names the outcome -- it never says agreement
        for an evaluation that refused or that found a property outside
        its margin -- and its basis is ``none`` because a fidelity check
        measures the synthetic dataset against a committed state, not
        against the world.

        *attestation* must be ``None``: the ``"none"`` backend produces no
        artefact, so a caller handing one over has confused this backend
        for another and is told so rather than having it ignored.
        """
        from flux_engine.api.claims import Claim, ClaimBasis, ClaimType

        if attestation is not None:
            raise FluxError(
                FLX_5005,
                "the 'none' backend produces and accepts no attestation artefact",
            )
        expose_id = getattr(statement, "expose_id", None)
        if expose_id is not None and expose_id != self.compiled.block.expose_id:
            raise FluxError(
                FLX_5005,
                f"the statement answers for expose {expose_id!r}, this backend for "
                f"{self.compiled.block.expose_id!r}",
            )
        evaluation = self.evaluate()
        if evaluation.refused:
            sentence = (
                f"the fidelity check declined to answer for expose "
                f"{self.compiled.block.expose_id!r}: {len(evaluation.refusals)} fail-closed "
                "conditions were met"
            )
        elif evaluation.unscored:
            sentence = (
                f"the fidelity check scored nothing for expose "
                f"{self.compiled.block.expose_id!r}: it declares no property, so no statement "
                "about its fidelity is made"
            )
        elif evaluation.within:
            sentence = (
                f"every one of the {len(evaluation.outcomes)} declared properties of expose "
                f"{self.compiled.block.expose_id!r} is within its compiled margin against the "
                f"committed state at ordinal {self.committed.ordinal}"
            )
        else:
            sentence = (
                f"{len(evaluation.outside)} of the {len(evaluation.outcomes)} declared "
                f"properties of expose {self.compiled.block.expose_id!r} are outside their "
                "compiled margin"
            )
        return Claim(
            claim_type=ClaimType.FIDELITY,
            statement=sentence,
            basis=ClaimBasis.NONE,
            evidence=(self.compiled.h, self.compiled.g),
            verifier=f"{type(self).__name__}/{self.proof_system.name}",
        )


# ---------------------------------------------------------------------------
# The profile path
# ---------------------------------------------------------------------------


def _domain_of(column: ColumnDeclaration) -> str:
    """The profile domain a declared column is read in (docs/spec/PROFILE-SPEC.md).

    A DATE column is a timestamp to a profile, in milliseconds (a day of the
    declaration is :data:`~flux_stats.reading.MS_PER_DAY` of them).
    """
    if column.kind is ColumnKind.SUPPORT:
        return "category"
    if column.domain in (ValueDomain.TIMESTAMP, ValueDomain.DATE):
        return "timestamp"
    return "number"


def _is_date(column: ColumnDeclaration) -> bool:
    return column.kind is ColumnKind.RANGE and column.domain is ValueDomain.DATE


def _whole_days(value: int, what: str) -> int:
    """*value*, a profile's milliseconds, as days; refused (``FLX-5037``) unless it is exact.

    A statistic of a DATE column is converted exactly or not at all. One that is
    not a whole number of days came from a column that holds more than dates,
    and flooring it would score a different statistic than the profile holds.
    """
    days, remainder = divmod(value, MS_PER_DAY)
    if remainder:
        raise FluxError(
            FLX_5037,
            f"the profile's {what} for a DATE column is not a whole number of days",
        )
    return days


def _group_in_days(group: ProfileGroup) -> ProfileGroup:
    """A published group of a DATE column, read in the days of the declaration.

    A sum and an order statistic are encoded values, so each is divided by
    the day's milliseconds; counts and ranks are unchanged.
    """
    state = group.statistic
    if not group.published or state is None:
        return group
    witness = state.quantile_witness
    return replace(
        group,
        statistic=replace(
            state,
            sum_x=_whole_days(state.sum_x, "sum"),
            quantile_witness=None if witness is None else _whole_days(witness, "order statistic"),
        ),
    )


def _with_cells(
    state: GroupStatistic, statistic: StatisticDefinition, cells: Tuple[int, ...], k: int
) -> GroupStatistic:
    """*state* with its declared cells and its record count replaced."""
    if statistic.kind == PropertyKind.LEVEL_HISTOGRAM.value:
        return replace(state, k=k, level_counts=cells)
    return replace(state, k=k, bin_counts=cells)


def _published_cells(
    statistic: StatisticDefinition, profiled: ProfileGroup, synthetic: GroupStatistic
) -> Optional[Tuple[GroupStatistic, GroupStatistic]]:
    """The two groups a comparison reads: a count table over its published cells.

    A cell the profile withheld is unknown, so it is censored the way a
    withheld group is: it is left out on both sides, never scored as zero.
    Each side keeps the records it puts in the published cells, and that
    sum is its count, so the total-variation inequality compares the two
    sides' distributions over the published cells, each renormalised to
    them. That distance bounds the distance over all the cells in neither
    direction, and how many records either side puts in the withheld cells
    is not scored.

    The profile's count is the sum of the cells compared, never its group's
    ``k``: a disclosure rule may round each published count and their total
    independently (ACRO's base 5), so ``k`` less the published cells is no
    count of the withheld ones.

    ``None`` when the profile withheld every cell: nothing is left to score.
    """
    assert profiled.statistic is not None
    reference = profiled.statistic
    if not statistic.cell_table:
        return reference, synthetic
    kind = statistic.kind
    withheld = set(profiled.suppressed_cells)
    cells = reference.cell_counts(kind)
    published = [index for index in range(len(cells)) if index not in withheld]
    if not published:
        return None
    reference_cells = tuple(cells[index] for index in published)
    synthetic_cells = tuple(synthetic.cell_counts(kind)[index] for index in published)
    return (
        _with_cells(reference, statistic, reference_cells, sum(reference_cells)),
        _with_cells(synthetic, statistic, synthetic_cells, sum(synthetic_cells)),
    )


def _cell_labels(kind: PropertyKind, reference: StatisticEntry) -> Tuple[str, ...]:
    """The label of each declared cell of a histogram: a level's name, or a bin's two edges.

    Empty for any other kind, which has no cells to draw.
    """
    if kind is PropertyKind.LEVEL_HISTOGRAM:
        return tuple(reference.levels)
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        return tuple(
            f"{render_exact(low)} to {render_exact(high)}"
            for low, high in zip(reference.bins, reference.bins[1:], strict=False)
        )
    return ()


def _reach(variance: Fraction, cells: int, group: ProfileGroup) -> Fraction:
    """:func:`flux_stats.noise.widening` of one noisy group: its exact total and its records.

    A group whose noisy values add up to nothing above zero has no records to widen a margin over
    (:func:`flux_stats.noise.point_estimate`), so it is refused (``FLX-5005``) rather than compared.
    """
    assert group.statistic is not None
    total = sum(group.noisy, Fraction(0)) if group.noisy else Fraction(group.statistic.k)
    if total <= 0 or group.statistic.k <= 0:
        raise FluxError(
            FLX_5005,
            "a noisy statistic's cells add up to no records above zero, so no tolerance can be "
            "widened over it",
        )
    return widening(variance, cells, total, group.statistic.k)


@dataclass(frozen=True)
class ClearProfile:
    """The profile path: each property's reference is read from a profile.

    The synthetic side is formed exactly as :class:`ClearStatistic` forms
    it, and every group is compared by the same registered definition, so a
    profile formed over a committed slice under the block's declarations
    gives the outcomes the slice path gives. What differs is where the
    reference comes from, and what a profile may withhold:

    * a group the profile suppressed is :data:`CENSORED`, and a property none
      of whose groups was scored is too;
    * a count-table cell it suppressed is censored: the table is compared
      over its published cells only, each side renormalised to its records
      in them (:func:`_published_cells`), so a withheld cell is never scored
      as zero, and a group whose every cell was withheld is
      :data:`CENSORED`.

    The fail-closed conditions of §5 hold on the profile's side as on a
    committed state's: a record count outside ``records``, a group below
    ``records.min``, values outside the declared range (when the profile
    publishes the column's ``min`` and ``max``). A profile that holds no
    statistic of the property's shape is ``FLX-5037``.
    """

    compiled: CompiledFidelity
    profile: Profile
    synthetic_rows: Sequence[Row]
    #: For a property grouped by a link (``groupBy: <relationship>.<column>``), the grouping
    #: the profile publishes it under, by that name. The rows carry the linked column under the
    #: same name (:mod:`flux_engine.concordance.linked`).
    link_groupings: Mapping[str, LinkGrouping] = field(default_factory=dict)

    def _refused(self, refusal: Refusal) -> FidelityEvaluation:
        return FidelityEvaluation(
            expose_id=self.compiled.block.expose_id,
            outcomes=(),
            refusals=(refusal,),
            synthetic_rows=len(self.synthetic_rows),
        )

    def evaluate(self) -> FidelityEvaluation:
        """Compare every declared property with the profile, fail-closed."""
        block = self.compiled.block
        table = self.profile.tables.get(block.expose_id)
        if table is None:
            return self._refused(
                Refusal(code=FLX_5037.code, reason="the profile holds no table for this expose")
            )
        try:
            if table.row_count is not None:
                require_sample(
                    table.row_count,
                    minimum=block.records.minimum,
                    maximum=block.records.maximum,
                    subject="profile",
                )
            require_record_count(self.synthetic_rows, block, subject="synthetic dataset")
        except FluxError as error:
            return self._refused(Refusal(code=error.code.code, reason=error.detail))
        outcomes: List[PropertyOutcome] = []
        refusals: List[Refusal] = []
        for entry in self.compiled.properties:
            outcome, property_refusals = self._evaluate_property(entry, table)
            outcomes.append(outcome)
            refusals.extend(property_refusals)
        return FidelityEvaluation(
            expose_id=block.expose_id,
            outcomes=tuple(outcomes),
            refusals=tuple(refusals),
            synthetic_rows=len(self.synthetic_rows),
        )

    def _require_columns(self, entry: CompiledProperty, table: TableEntry) -> None:
        declaration = entry.declaration
        names = list(declaration.columns)
        if declaration.group_by is not None:
            names.append(declaration.group_by)
        for name in names:
            if name in self.link_groupings:
                # A column of the linked row: the profile holds it as a grouping, not a column.
                continue
            column = self.compiled.block.columns[name]
            found: Optional[ColumnEntry] = table.columns.get(name)
            if found is None:
                raise FluxError(FLX_5037, f"the profile holds no column {name!r} for this expose")
            expected = _domain_of(column)
            # A profile that reads a DATE column as a number holds days, as the
            # declaration does: that was the only reading before DATE had its own.
            if found.domain != expected and not (_is_date(column) and found.domain == "number"):
                raise FluxError(
                    FLX_5037,
                    f"the profile reads column {name!r} as {found.domain}, the declaration as "
                    f"{expected}",
                )
            if column.kind is ColumnKind.RANGE:
                interval = column.interval
                for bound in ("min", "max"):
                    metric = found.metrics.get(bound)
                    if metric is not None and metric.value is not None:
                        published = Fraction(metric.value)
                        if _is_date(column) and found.domain == "timestamp":
                            published /= MS_PER_DAY
                        if not interval.contains(published):
                            raise FluxError(
                                FLX_5005,
                                f"the profile's values of column {name!r} lie outside the "
                                "declared range",
                            )

    def _read_in_days(self, name: str, table: TableEntry) -> bool:
        """Whether column *name* is a DATE column the profile holds as milliseconds."""
        column = self.compiled.block.columns[name]
        found = table.columns.get(name)
        return _is_date(column) and found is not None and found.domain == "timestamp"

    def _reference(self, entry: CompiledProperty, table: TableEntry) -> StatisticEntry:
        """The profile's statistic for *entry*, or refuse (``FLX-5037``)."""
        declaration = entry.declaration
        block = self.compiled.block
        statistic = definition_for_property(declaration)
        self._require_columns(entry, table)
        if statistic.on_grid and self.profile.fractional_bits != block.encoding.fractional_bits:
            raise FluxError(
                FLX_5037,
                f"the profile forms {statistic.identifier} on fractionalBits "
                f"{self.profile.fractional_bits}, the block declares "
                f"{block.encoding.fractional_bits}",
            )
        if declaration.kind is PropertyKind.RANK_CORRELATION:
            return self._rank_reference(entry)
        levels: Tuple[str, ...] = ()
        if declaration.kind is PropertyKind.LEVEL_HISTOGRAM:
            levels = level_cells(block.columns[declaration.columns[0]])
        in_days = self._read_in_days(declaration.columns[0], table)
        if declaration.kind is PropertyKind.CORRELATION:
            if any(self._read_in_days(name, table) for name in declaration.columns):
                raise FluxError(
                    FLX_5037,
                    "a correlation over a DATE column is not scored against a profile",
                )
            candidates = self.profile.pair_statistics(block.expose_id, declaration.columns)
        else:
            candidates = self.profile.column_statistics(block.expose_id, declaration.columns[0])
        # What differs, of each candidate of the property's statistic: the shape the property
        # declares is groupBy, bins, levels and level, and the refusal names the ones the
        # nearest candidate lacks.
        link = self.link_groupings.get(declaration.group_by or "")
        grouping = None if link is not None else declaration.group_by
        nearest: Optional[List[str]] = None
        for candidate in candidates:
            bins = candidate.bins
            if in_days:
                bins = tuple(Fraction(edge) / MS_PER_DAY for edge in bins)
            if candidate.identifier == statistic.identifier:
                differs = [
                    name
                    for name, same in (
                        (
                            "groupBy",
                            candidate.group_by == grouping and candidate.group_by_link == link,
                        ),
                        ("bins", bins == declaration.bins),
                        ("levels", candidate.levels == levels),
                        ("level", candidate.level == declaration.level),
                    )
                    if not same
                ]
                if nearest is None or len(differs) < len(nearest):
                    nearest = differs
            if (
                candidate.identifier == statistic.identifier
                and candidate.group_by == grouping
                and candidate.group_by_link == link
                and not candidate.per
                and bins == declaration.bins
                and candidate.levels == levels
                and candidate.level == declaration.level
            ):
                if in_days:
                    return replace(
                        candidate,
                        bins=bins,
                        groups=tuple(_group_in_days(group) for group in candidate.groups),
                    )
                return candidate
        if nearest:
            raise FluxError(
                FLX_5037,
                f"the profile holds {statistic.identifier} over this property's columns, but "
                f"not with this property's {_listed(nearest)}",
            )
        raise FluxError(
            FLX_5037,
            f"the profile holds no {statistic.identifier} statistic over this property's columns",
        )

    def _rank_reference(self, entry: CompiledProperty) -> StatisticEntry:
        """The profile's rank correlation of the property's pair, read in either order, or refuse.

        Both statistics are symmetric, so the pair is found whichever way the profile names it.
        """
        declaration = entry.declaration
        statistic = definition_for_property(declaration)
        expose_id = self.compiled.block.expose_id
        pair = tuple(declaration.columns)
        for order in (pair, pair[::-1]):
            for candidate in self.profile.pair_statistics(expose_id, order):
                if candidate.identifier == statistic.identifier:
                    return candidate
        raise FluxError(
            FLX_5037,
            f"the profile holds no {statistic.identifier} statistic over this property's columns",
        )

    def _evaluate_rank(
        self, entry: CompiledProperty, table: TableEntry
    ) -> Tuple[PropertyOutcome, List[Refusal]]:
        """Compare one rank correlation with the profile's (ADR 0035 decision 7, :mod:`flux_engine.concordance.rank`)."""
        property_id = entry.property_id
        block = self.compiled.block
        base = _unscored(entry)
        try:
            reference = self._reference(entry, table)
            run, records = run_rank_value(
                self.synthetic_rows,
                entry.declaration,
                block.columns,
                block,
                subject="synthetic dataset",
            )
        except FluxError as error:
            return base, [
                Refusal(code=error.code.code, reason=error.detail, property_id=property_id)
            ]
        group = reference.groups[0]
        if not group.published or group.value is None:
            return (
                replace(
                    base,
                    outcome=CENSORED,
                    groups=(GroupOutcome("", 0, records, CENSORED),),
                ),
                [],
            )
        assert entry.margin is not None
        found = compare_rank(
            entry.declaration,
            entry.margin,
            run=run,
            records=records,
            reference=group.value,
            variance=reference.cell_variance,
        )
        outcome = WITHIN if found.within else OUTSIDE
        return (
            replace(
                base,
                outcome=outcome,
                groups=(
                    GroupOutcome(
                        group="",
                        committed_count=group.statistic.k if group.statistic is not None else 0,
                        synthetic_count=records,
                        outcome=outcome,
                        score=found.score,
                        widening=found.noise,
                    ),
                ),
                cell_variance=reference.cell_variance,
                noise_mechanism=reference.noise.mechanism if reference.noise is not None else None,
                rank=found,
            ),
            [],
        )

    def _draw_statistic(
        self, entry: CompiledProperty, table: TableEntry, reference: StatisticEntry
    ) -> Optional[StatisticEntry]:
        """The clique's own answer to the reference's question, when the run draws from it.

        A column in a clique is drawn from the clique, so a property compared with a statistic the
        document holds for the column (which carries its own noise) is compared across two noisy
        statistics. The clique's answer is the one the column's draw rests on; ``None`` when the
        reference is the clique's own, or no clique holds the column.
        """
        if reference.noise is None or reference.derived or reference.group_by_link is not None:
            return None
        column = table.columns.get(entry.declaration.columns[0])
        if column is None:
            return None
        return next(
            (
                found
                for found in column.clique_statistics
                if (found.identifier, found.group_by, found.levels)
                == (reference.identifier, reference.group_by, reference.levels)
            ),
            None,
        )

    @staticmethod
    def _score(
        statistic: StatisticDefinition,
        check: Check,
        group: ProfileGroup,
        synthetic: GroupStatistic,
        where: str,
        floor: int,
        width_units: Optional[int] = None,
        cell_variance: Optional[Fraction] = None,
        draw: Optional[Tuple[Fraction, ProfileGroup]] = None,
        sampling: Optional[Tuple[Any, CompiledProperty]] = None,
        labels: Tuple[str, ...] = (),
    ) -> Tuple[
        Outcome, Optional[Fraction], Optional[Fraction], Optional[Fraction], Tuple[CellCounts, ...]
    ]:
        """One published group's outcome, similarity, widenings and cells, or a fail-closed ``FLX-5005``.

        The cells are the two sides' counts in the published cells the score was computed over,
        labelled by *labels* (one per declared cell); empty for a statistic that is no count table.

        A reference that is a noisy statistic (*cell_variance* is its cells' variance) widens the
        margin by :func:`flux_stats.noise.widening`, for the cells compared and the total of the
        noisy values, and the third item is that widening. When the column is drawn from another
        noisy statistic than its reference (*draw*: the variance of its cells and its group), the
        run is within the reach of the two noises of each other, so their widenings add
        (SCORECARD-SPEC.md section 9).

        A distance tolerance that declares ``sampling: true`` (*sampling*: the block and the
        compiled property) is widened further by :func:`sampling_margin`, and the fourth item is
        that widening (ADR 0016 section 8).

        The group floor reads the group's ``k``. A count table then needs a
        record in its published cells on each side: with none, that side has
        no distribution over them to compare. The similarity is formed from
        the same two groups the comparison read, so a count table is scored
        over its published cells, renormalised, as it is compared.
        """
        assert group.statistic is not None
        require_sample(group.statistic.k, minimum=floor, subject=f"profile: {where}")
        compared = _published_cells(statistic, group, synthetic)
        if compared is None:
            return CENSORED, None, None, None, ()
        reference, renormalised = compared
        if statistic.cell_table:
            require_sample(reference.k, subject=f"profile: {where}, in the cells it published")
            require_sample(
                renormalised.k,
                subject=f"synthetic dataset: {where}, in the cells the profile published",
            )
        widened = None
        if cell_variance is not None and check.margin is not None:
            cells = len(reference.cell_counts(statistic.kind))
            widened = _reach(cell_variance, cells, group)
            if draw is not None:
                widened += _reach(draw[0], cells, draw[1])
            check = replace(check, margin=check.margin + widened)
        sampled = None
        if sampling is not None and check.margin is not None:
            sampled = sampling_margin(sampling[0], sampling[1], reference, renormalised)
            if sampled is not None:
                check = replace(check, margin=check.margin + sampled)
        within = statistic.compare(renormalised, reference, check)
        score = similarity(
            statistic, renormalised, reference, width_units=width_units, edges=check.edges
        )
        drawn: Tuple[CellCounts, ...] = ()
        if statistic.cell_table and labels:
            withheld = set(group.suppressed_cells)
            names = [labels[index] for index in range(len(labels)) if index not in withheld]
            drawn = tuple(
                CellCounts(label, profile, run)
                for label, profile, run in zip(
                    names,
                    reference.cell_counts(statistic.kind),
                    renormalised.cell_counts(statistic.kind),
                    strict=True,
                )
            )
        return (WITHIN if within else OUTSIDE), score, widened, sampled, drawn

    def _width_units(self, entry: CompiledProperty) -> Optional[int]:
        """The declared range's width on the block's grid, for a mean or a quantile.

        Both ends are placed on the grid the way a value is (floored), so the
        width is in the units the two sides' sums and witnesses are in.
        """
        declaration = entry.declaration
        if declaration.kind not in (PropertyKind.MEAN, PropertyKind.QUANTILE):
            return None
        column = self.compiled.block.columns[declaration.columns[0]]
        interval = column.interval
        grid = self.compiled.block.encoding.grid
        return grid.quantise(interval.hi, subject="range max") - grid.quantise(
            interval.lo, subject="range min"
        )

    def _evaluate_property(
        self, entry: CompiledProperty, table: TableEntry
    ) -> Tuple[PropertyOutcome, List[Refusal]]:
        """Compare one property with the profile, refusing only that property."""
        if entry.declaration.kind is PropertyKind.RANK_CORRELATION:
            return self._evaluate_rank(entry, table)
        property_id = entry.property_id
        block = self.compiled.block
        base = _unscored(entry)
        try:
            reference = self._reference(entry, table)
            synthetic_statistic = compute_property_statistic(
                self.synthetic_rows, entry, block, subject="synthetic dataset"
            )
        except FluxError as error:
            return base, [
                Refusal(code=error.code.code, reason=error.detail, property_id=property_id)
            ]
        profiled = {group.key: group for group in reference.groups}
        synthetic_groups = {group.group: group for group in synthetic_statistic.groups}
        if set(profiled) != set(synthetic_groups):
            return base, [
                Refusal(
                    code=FLX_5005.code,
                    reason=(
                        f"the two sides group into different sets: {len(profiled)} in the "
                        f"profile and {len(synthetic_groups)} synthetic"
                    ),
                    property_id=property_id,
                )
            ]
        statistic = definition_for_property(entry.declaration)
        check = replace(_check_of(entry), reference_side="profile")
        grouped = entry.declaration.group_by is not None
        width_units = self._width_units(entry)
        refusals: List[Refusal] = []
        group_outcomes: List[GroupOutcome] = []
        drawn = self._draw_statistic(entry, table, reference)
        labels = _cell_labels(entry.declaration.kind, reference)
        draws = {group.key: group for group in drawn.groups} if drawn is not None else {}
        for ordinal, key in enumerate(sorted(profiled)):
            group = profiled[key]
            synthetic = synthetic_groups[key]
            outcome, committed, score, widened, sampled = CENSORED, 0, None, None, None
            cells: Tuple[CellCounts, ...] = ()
            if group.published and group.statistic is not None:
                committed = group.statistic.k
                where = f"{property_id} group #{ordinal}" if grouped else property_id
                floor = block.records.minimum if grouped else 1
                try:
                    outcome, score, widened, sampled, cells = self._score(
                        statistic,
                        check,
                        group,
                        synthetic,
                        where,
                        floor,
                        width_units,
                        reference.cell_variance,
                        (
                            (drawn.cell_variance, draws[key])
                            if drawn is not None
                            and drawn.cell_variance is not None
                            and key in draws
                            and draws[key].statistic is not None
                            else None
                        ),
                        (block, entry),
                        labels,
                    )
                except FluxError as error:
                    refusals.append(
                        Refusal(code=error.code.code, reason=error.detail, property_id=property_id)
                    )
                    outcome, score, widened, sampled, cells = OUTSIDE, None, None, None, ()
            group_outcomes.append(
                GroupOutcome(
                    group=key,
                    committed_count=committed,
                    synthetic_count=synthetic.k,
                    outcome=outcome,
                    score=score,
                    widening=widened,
                    sampling=sampled,
                    cells=cells,
                )
            )
        scored = [outcome for outcome in group_outcomes if not outcome.censored]
        if not scored:
            outcome = CENSORED
        else:
            outcome = WITHIN if all(group.within for group in scored) else OUTSIDE
        return (
            replace(
                base,
                outcome=outcome,
                groups=tuple(group_outcomes),
                cell_variance=reference.cell_variance,
                noise_mechanism=reference.noise.mechanism if reference.noise is not None else None,
            ),
            refusals,
        )


# The extension contract this backend targets is
# ``flux_engine.api.fidelity.FidelityVerifier``. It is satisfied at runtime
# (``isinstance(backend, FidelityVerifier)`` holds, and a test pins that),
# but not yet as a *static* assertion: that Protocol spells
# ``proof_system`` and ``ProofSystem.name`` as settable variables, and mypy
# refuses a frozen dataclass against a settable protocol member. Every
# dataclass on that API surface is frozen by its own doctrine, so the
# spelling excludes the implementations it is written for. The fix belongs
# in ``flux_engine/api/fidelity.py`` (read-only properties, which a mutable
# attribute also satisfies) and is filed as a request rather than worked
# around here by unfreezing a value object.
