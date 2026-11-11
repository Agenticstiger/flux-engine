# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The verifier -- SEAM-FIDELITY-SPEC §5.

    Inputs: the contract set (recomputes ``H``, ``G``), ``S``, ``C1``,
    ``C2``, and D′ (recomputes D′'s statistics on the same grid and D′'s
    digest = ``outputDigest``). Evaluates each property in the integer
    cross-multiplied form [...]. Fail-closed conditions: ``k = 0``, count
    outside ``records``, group below ``records.min``, a value outside its
    range, a column absent, ``ε ≥ τ``. **No proof is generated in v0.1**;
    the ``ProofSystem`` port has one implementation, ``none``, and the
    record says so.

Three properties this module is shaped to keep.

**Nothing here divides.** Every comparison is one of
:mod:`flux_engine.core.fixedpoint`'s integer cross-multiplied forms, over
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

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.boundary.slice import CommittedState
from flux_engine.concordance.boundary.statistic import (
    GroupStatistic,
    Row,
    compute_property_statistic,
    require_record_count,
)
from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty, render_exact
from flux_engine.concordance.errors import FLX_5011
from flux_engine.concordance.predicate import PropertyKind, ToleranceKind
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import DigestRef, digest_of_stream
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import (
    FLX_5005,
    correlation_within,
    mean_within_absolute,
    mean_within_relative,
    quantile_rank_ok,
    quantile_within,
    support_within,
    total_variation_within,
)

__all__ = [
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

#: KERNEL-SPEC §8's framing label for the emit stream.
EMIT_STREAM_PREFIX = b"flux-emit/1\x00"


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
class GroupOutcome:
    """One property's outcome inside one group.

    ``group`` is the empty string for an ungrouped property. The two
    counts travel with the outcome because they are what a reader needs to
    tell a comparison over a thousand records from one over the
    ``records.min`` floor.
    """

    group: str
    committed_count: int
    synthetic_count: int
    outcome: Outcome

    @property
    def within(self) -> bool:
        """Whether this group's comparison landed inside the margin."""
        return self.outcome == WITHIN


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

    @property
    def within(self) -> bool:
        """Whether every group of this property landed inside the margin."""
        return self.outcome == WITHIN

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
        """
        return not self.refusals and all(entry.within for entry in self.outcomes)

    @property
    def outside(self) -> Tuple[PropertyOutcome, ...]:
        """The properties that landed outside their compiled margin."""
        return tuple(entry for entry in self.outcomes if not entry.within)


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


def _group_outcome(
    compiled: CompiledProperty,
    committed: GroupStatistic,
    synthetic: GroupStatistic,
) -> bool:
    declaration = compiled.declaration
    kind = declaration.kind

    if kind is PropertyKind.MEAN:
        if declaration.tolerance.kind is ToleranceKind.ABSOLUTE:
            assert compiled.margin_units is not None
            return mean_within_absolute(
                sum_a=synthetic.sum_x,
                count_a=synthetic.k,
                sum_b=committed.sum_x,
                count_b=committed.k,
                tolerance_units=compiled.margin_units,
            )
        assert compiled.margin is not None
        return mean_within_relative(
            sum_a=synthetic.sum_x,
            count_a=synthetic.k,
            sum_b=committed.sum_x,
            count_b=committed.k,
            tolerance=compiled.margin,
        )

    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        assert compiled.margin is not None
        return total_variation_within(
            counts_a=synthetic.bin_counts,
            count_a=synthetic.k,
            counts_b=committed.bin_counts,
            count_b=committed.k,
            tolerance=compiled.margin,
        )

    if kind is PropertyKind.SUPPORT:
        assert compiled.max_non_members is not None
        members = set(committed.support)
        # §5's "count of non-members" is counted in RECORDS, not in
        # distinct strange values. Counting distinct values would let a
        # dataset every one of whose records carries one undeclared level
        # count as a single non-member and pass under any ``max >= 1``,
        # so the tolerance would bound the size of the strange set rather
        # than how much of the dataset is strange.
        strangers = sum(
            occurrences
            for value, occurrences in zip(synthetic.support, synthetic.support_counts, strict=True)
            if value not in members
        )
        return support_within(
            non_member_count=strangers,
            max_non_members=compiled.max_non_members,
        )

    if kind is PropertyKind.CORRELATION:
        assert compiled.margin is not None and declaration.min_variance is not None
        return correlation_within(
            moments_a=synthetic.moments,
            moments_b=committed.moments,
            tolerance=compiled.margin,
            min_variance=declaration.min_variance,
        )

    # QUANTILE
    assert declaration.level is not None and compiled.margin_units is not None
    for side, statistic in (("committed", committed), ("synthetic", synthetic)):
        if statistic.quantile_witness is None:
            raise FluxError(
                FLX_5005, f"{side} side offers no quantile witness for {declaration.property_id}"
            )
        if not quantile_rank_ok(
            count=statistic.k,
            level=declaration.level,
            count_strictly_below=statistic.quantile_below,
            count_at_or_below=statistic.quantile_at_or_below,
        ):
            raise FluxError(
                FLX_5005,
                f"the {side} side's quantile witness for {declaration.property_id} does not "
                "carry the ranks the order statistic requires",
            )
    assert committed.quantile_witness is not None and synthetic.quantile_witness is not None
    return quantile_within(
        witness_a=synthetic.quantile_witness,
        witness_b=committed.quantile_witness,
        tolerance_units=compiled.margin_units,
    )


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
        for entry in self.compiled.properties:
            outcome, property_refusals = self._evaluate_property(entry)
            refusals.extend(property_refusals)
            outcomes.append(outcome)
        return FidelityEvaluation(
            expose_id=self.compiled.block.expose_id,
            outcomes=tuple(outcomes),
            refusals=tuple(refusals),
            synthetic_rows=len(self.synthetic_rows),
        )

    def _evaluate_property(self, entry: CompiledProperty) -> Tuple[PropertyOutcome, List[Refusal]]:
        """Compare one property, refusing only that property on a fail-closed.

        The synthetic side's quantities are formed here rather than for the
        whole dataset at once, so a value outside its range on one column
        refuses the properties over that column and leaves the rest
        answerable.
        """
        declaration = entry.declaration
        property_id = entry.property_id
        refusals: List[Refusal] = []
        base = PropertyOutcome(
            property_id=property_id,
            kind=declaration.kind,
            columns=declaration.columns,
            group_by=declaration.group_by,
            tolerance=declaration.tolerance.declared,
            margin=render_exact(entry.margin) if entry.margin is not None else None,
            epsilon=render_exact(entry.epsilon),
            outcome=OUTSIDE,
        )
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
            try:
                within = _group_outcome(entry, committed_groups[key], synthetic_groups[key])
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
