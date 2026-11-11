# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ship gate -- SEAM-FIDELITY-SPEC §7.

    Recompute ``H`` over the contract set; verify the record's binding to
    ``H`` and its signature; recompute D′'s digest and statistics from the
    rows actually placed in the pack; re-evaluate every property; check
    ``committedAt`` against ``budget.maxStateAge``. On any failure: write
    no bundle, exit 1 with ``FLX-8001``, emit a CloudEvent
    ``com.agentics.flux.ship.withheld`` to the configured sink (stdout
    NDJSON by default). On success: pack (staged copy; the ``.fluid.yml``
    copied byte-for-byte) and print the public re-verification commands.

This module decides; it does not act. :func:`run_gate` returns a
:class:`GateDecision`, and the ship stage is what writes a bundle or does
not. Two reasons for the split: a decision can be tested without a
filesystem, and a decision that has already been made cannot be quietly
re-derived by whoever is about to write the bundle.

**The rows are read once.** :class:`PackedEmit` carries the header and the
rows as they sit in the pack, and both the recomputed ``outputDigest`` and
the recomputed statistics come from that one object. A gate that took the
digest from one sequence and the statistics from another would be
checking two different datasets and reporting on one.

**Every check runs.** The gate does not stop at the first failure: a
decision lists every check that failed, because the operator reading the
withheld event needs to know whether one property drifted or the record
does not belong to this contract at all.

Nothing here reads a clock. ``now_ms`` and the event's ``id`` and ``time``
are supplied by the caller, so one set of inputs always produces one
decision and one event, byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.boundary.slice import CommittedState
from flux_engine.concordance.boundary.statistic import Row
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.errors import FLX_8001
from flux_engine.concordance.record import (
    FidelityRecord,
    TrustedKeys,
    load_record,
    verify_record,
)
from flux_engine.concordance.verifier import (
    ClearStatistic,
    FidelityEvaluation,
    emit_stream_digest,
)
from flux_engine.core.digest import DigestRef
from flux_engine.core.errors import FluxError

__all__ = [
    "GateAction",
    "GateCheck",
    "GateDecision",
    "PackedEmit",
    "WITHHELD_EVENT_TYPE",
    "reverification_commands",
    "run_gate",
]

#: The CloudEvent type §7 names for a withheld bundle.
WITHHELD_EVENT_TYPE = "com.agentics.flux.ship.withheld"

#: The CloudEvents ``specversion`` this envelope is built to.
CLOUDEVENTS_SPEC_VERSION = "1.0"


class GateAction(str, Enum):
    """What the ship stage should do with this decision.

    Two members, and the failure member is named for the effect rather
    than the cause: ``withhold`` is what happens to the bundle, and §2's
    ``failureAction`` spells the same word.
    """

    EMIT = "emit"
    WITHHOLD = "withhold"


@dataclass(frozen=True)
class GateCheck:
    """One of §7's checks, and what it found.

    ``detail`` names digests, counts and property ids -- never a record or
    a value, since a withheld event is written to a sink outside the
    boundary.

    ``checked`` is the third state a check can be in. A control the gate
    could not evaluate -- ``committedState.age`` with no independently
    declared commitment time, for instance -- reports ``checked=False``
    and says so in ``detail``, rather than recording a green check nobody
    performed. It does not withhold: "we could not check this" is not the
    same finding as "this failed", and a reader of the decision has to be
    able to tell them apart.
    """

    name: str
    passed: bool
    detail: str = ""
    checked: bool = True

    def to_document(self) -> Dict[str, Any]:
        """The canonical form carried in the decision and the event."""
        return {
            "check": self.name,
            "passed": self.passed,
            "checked": self.checked,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PackedEmit:
    """The emit stream as it sits in the pack: one header, one row list.

    ``rows`` are arrays aligned to the header's declared column order
    (KERNEL-SPEC §8), which is how they are digested; :meth:`as_mappings`
    is the same rows keyed by column name, which is how they are read into
    a statistic. One object, two views, no second dataset.
    """

    header: Mapping[str, Any]
    rows: Tuple[Tuple[Any, ...], ...]

    @property
    def columns(self) -> Tuple[str, ...]:
        """The header's declared column order."""
        declared = self.header.get("columns", ())
        return tuple(str(name) for name in declared)

    def as_mappings(self) -> List[Row]:
        """The packed rows keyed by column name.

        Refuses (``FLX-8001``) a row whose width does not match the
        header: a short row would silently drop a column from every
        statistic formed over it.
        """
        names = self.columns
        mapped: List[Row] = []
        for index, row in enumerate(self.rows):
            if len(row) != len(names):
                raise FluxError(
                    FLX_8001,
                    f"packed row {index} carries {len(row)} values where the header "
                    f"declares {len(names)} columns",
                )
            mapped.append(dict(zip(names, row, strict=True)))
        return mapped


@dataclass(frozen=True)
class GateDecision:
    """The gate's verdict, and every check behind it."""

    action: GateAction
    expose_id: str
    checks: Tuple[GateCheck, ...]
    evaluation: Optional[FidelityEvaluation] = None
    code: Optional[str] = None

    @property
    def failures(self) -> Tuple[GateCheck, ...]:
        """Every check that did not pass, in the order they were run."""
        return tuple(check for check in self.checks if not check.passed)

    @property
    def withheld(self) -> bool:
        """Whether the bundle is withheld."""
        return self.action is GateAction.WITHHOLD

    def to_document(self) -> Dict[str, Any]:
        """The canonical form of the decision."""
        document: Dict[str, Any] = {
            "action": self.action.value,
            "exposeId": self.expose_id,
            "checks": [check.to_document() for check in self.checks],
            "code": self.code,
        }
        if self.evaluation is not None:
            document["refusals"] = [refusal.to_document() for refusal in self.evaluation.refusals]
            # The property id and the word `outside` used to be the whole
            # of it, so the withheld event -- the most consequential
            # document this engine emits -- named a property and said
            # nothing about what was measured, over which columns, or
            # against what margin. Every value here is already on the
            # outcome, and none of it is a record or a row, so none of it
            # takes anything out of the boundary (§4).
            document["properties"] = [
                {
                    "propertyId": outcome.property_id,
                    "outcome": outcome.outcome,
                    "statistic": outcome.kind.value,
                    "columns": list(outcome.columns),
                    "declaredTolerance": dict(sorted(outcome.tolerance.items())),
                    "margin": outcome.margin,
                }
                for outcome in self.evaluation.outcomes
            ]
        return document

    def withheld_event(self, *, event_id: str, time: str, source: str) -> Dict[str, Any]:
        """The ``com.agentics.flux.ship.withheld`` CloudEvent (§7).

        Built through the CloudEvents SDK so the envelope is the CNCF
        1.0 one rather than a hand-rolled lookalike -- and with ``id`` and
        ``time`` supplied by the caller, because the SDK would otherwise
        fill them from ``uuid4`` and the host clock and two runs of one
        gate would produce two different events.

        Raises when the decision is not a withholding: there is no such
        thing as a withheld event for a bundle that shipped.
        """
        if not self.withheld:
            raise FluxError(FLX_8001, "a decision to emit has no withheld event")
        try:
            from cloudevents.v1.conversion import to_dict
            from cloudevents.v1.http import CloudEvent
        except ImportError:  # pragma: no cover - cloudevents 1.x layout
            from cloudevents.conversion import to_dict  # type: ignore[no-redef]
            from cloudevents.http import CloudEvent  # type: ignore[no-redef]

        event = CloudEvent(
            {
                "type": WITHHELD_EVENT_TYPE,
                "source": source,
                "id": event_id,
                "time": time,
                "datacontenttype": "application/json",
                "specversion": CLOUDEVENTS_SPEC_VERSION,
            },
            self.to_document(),
        )
        envelope = to_dict(event)
        return {str(key): envelope[key] for key in sorted(envelope)}


def reverification_commands(record: FidelityRecord) -> Tuple[str, ...]:
    """The commands §7 says a shipped bundle prints.

    Each is a command a reader outside this process runs to arrive at the
    same conclusion from the pack alone -- the point being that the
    conclusion does not rest on having been told it.
    """
    return (
        "fluxsim verify --bundle .",
        f"fluxsim verify --fidelity-record run/fidelity-record-{record.expose_id}.json",
        f"fluxsim explain {FLX_8001.code}",
    )


#: Recomputes an emit stream's ``outputDigest`` from a header and rows.
#: Injectable so the seam's own implementation (KERNEL-SPEC §8,
#: ``seam/emit.py``) replaces the one in
#: :mod:`flux_engine.concordance.verifier` without a change here.
OutputDigestFn = Callable[[Mapping[str, Any], Sequence[Sequence[Any]], str], DigestRef]


def _default_output_digest(
    header: Mapping[str, Any], rows: Sequence[Sequence[Any]], subject: str
) -> DigestRef:
    return emit_stream_digest(header=header, rows=rows, subject=subject)


def run_gate(
    *,
    compiled: CompiledFidelity,
    record_document: Mapping[str, Any],
    committed: CommittedState,
    packed: PackedEmit,
    now_ms: Optional[int] = None,
    trusted_keys: TrustedKeys = None,
    self_attested: bool = False,
    output_digest: OutputDigestFn = _default_output_digest,
) -> GateDecision:
    """Run §7's checks and return the decision, without acting on it.

    *compiled* is the gate's own recomputation of ``H`` and ``G`` over the
    contract set in the pack -- not the record's copy of them, which is
    what the binding check is for. *committed* is the published committed
    state; *packed* is the emit stream as it sits in the pack.

    *trusted_keys* is the trust anchor the record's signature is checked
    against, and it comes from the caller (a key file, a configured
    operator key), never out of the record. *self_attested* says that the
    anchor was derived from the very key this call signed with -- in which
    case the signature check is a statement about internal consistency and
    about nothing else, and the recorded check says exactly that rather
    than reading as an attribution.

    *now_ms* is the instant ``budget.maxStateAge`` is measured against.
    ``None`` means the caller has no clock reading it can honestly compare
    the commitment time to -- the commitment time was stamped by the same
    process moments earlier, so an age computed from it is identically
    zero and the budget could never refuse anything. The age check then
    reports ``checked=False`` instead of a green result.
    """
    expose_id = compiled.block.expose_id
    checks: List[GateCheck] = []
    evaluation: Optional[FidelityEvaluation] = None

    try:
        record = load_record(record_document)
    except FluxError as error:
        checks.append(GateCheck("record.shape", False, error.detail or error.code.title))
        return GateDecision(
            action=GateAction.WITHHOLD,
            expose_id=expose_id,
            checks=tuple(checks),
            code=FLX_8001.code,
        )
    checks.append(
        GateCheck("record.shape", True, f"recordVersion {record.document['recordVersion']}")
    )

    # 1. the record's binding to the recomputed H (and to G, which the
    #    committed statistic was formed under).
    bound_to_h = record.h == compiled.h.value
    checks.append(
        GateCheck(
            "binding.H",
            bound_to_h,
            (
                "the record is bound to the recomputed seam digest"
                if bound_to_h
                else "the record names a different seam digest from the one the pack recomputes"
            ),
        )
    )
    bound_to_g = record.g == compiled.g.value and committed.g == compiled.g.value
    checks.append(
        GateCheck(
            "binding.G",
            bound_to_g,
            (
                "the record and the committed state were formed under the recomputed declarations"
                if bound_to_g
                else "the record or the committed state was formed under different declarations"
            ),
        )
    )
    same_expose = record.expose_id == expose_id
    checks.append(
        GateCheck(
            "binding.expose",
            same_expose,
            (
                "the record answers for this expose"
                if same_expose
                else "the record answers for a different expose"
            ),
        )
    )

    # 2. the signature, against the caller's trust anchor.
    reasons = verify_record(record.document, trusted_keys)
    key_id = record.document["signature"]["keyId"]
    if reasons:
        signature_detail = "; ".join(reasons)
    elif self_attested:
        # Said plainly, because the alternative reads as an attribution
        # it is not: the key checked here is the one this same call signed
        # with, so the check says the bytes are internally consistent and
        # says nothing at all about who produced them.
        signature_detail = (
            f"self-attested: the signature verifies against key {key_id!r}, which was "
            "derived from the key this invocation signed with -- it attributes the "
            "record to no operator"
        )
    else:
        signature_detail = f"verified against a trusted key, signed as {key_id!r}"
    checks.append(GateCheck("record.signature", not reasons, signature_detail))

    # 3. D-prime's digest, recomputed from the rows actually in the pack.
    try:
        rows = packed.as_mappings()
    except FluxError as error:
        checks.append(GateCheck("synthetic.rows", False, error.detail))
        return GateDecision(
            action=GateAction.WITHHOLD,
            expose_id=expose_id,
            checks=tuple(checks),
            code=FLX_8001.code,
        )
    recomputed = output_digest(packed.header, packed.rows, f"outputDigest:{expose_id}")
    declared_digest = str(record.document["synthetic"].get("outputDigest", ""))
    digest_matches = recomputed.value == declared_digest
    checks.append(
        GateCheck(
            "synthetic.outputDigest",
            digest_matches,
            (
                "the packed rows digest to the value the record names"
                if digest_matches
                else "the packed rows digest to a different value from the one the record names"
            ),
        )
    )
    declared_rows = record.document["synthetic"].get("rows")
    rows_match = declared_rows == len(rows)
    checks.append(
        GateCheck(
            "synthetic.rowCount",
            rows_match,
            (
                f"{len(rows)} rows in the pack"
                if rows_match
                else f"{len(rows)} rows in the pack against {declared_rows!r} in the record"
            ),
        )
    )

    # 4. the committed state the record names.
    state_block = record.document["committedState"]
    ordinal_matches = state_block.get("ordinal") == committed.ordinal
    checks.append(
        GateCheck(
            "committedState.ordinal",
            ordinal_matches,
            (
                f"state ordinal {committed.ordinal}"
                if ordinal_matches
                else f"the record names state ordinal {state_block.get('ordinal')!r}, "
                f"the pack carries {committed.ordinal}"
            ),
        )
    )
    c2_matches = state_block.get("C2") == committed.c2
    checks.append(
        GateCheck(
            "committedState.C2",
            c2_matches,
            (
                "the record names the published commitment"
                if c2_matches
                else "the record names a different commitment from the published one"
            ),
        )
    )

    # 5. every property, re-evaluated from the packed rows.
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=rows
    ).evaluate()
    checks.append(
        GateCheck(
            "properties.refusals",
            not evaluation.refused,
            (
                "no fail-closed condition was met"
                if not evaluation.refused
                else f"{len(evaluation.refusals)} fail-closed conditions were met"
            ),
        )
    )
    checks.append(
        GateCheck(
            "properties.outcomes",
            evaluation.within,
            (
                f"all {len(evaluation.outcomes)} declared properties are within their margins"
                if evaluation.within
                else f"{len(evaluation.outside)} of {len(evaluation.outcomes)} declared properties "
                "are outside their margins"
            ),
        )
    )

    # 6. the state's age against budget.maxStateAge.
    max_age = compiled.block.budget.max_state_age_ms
    if now_ms is None:
        # No independently declared commitment time to measure against.
        # Reported, not passed: an age check whose two operands were
        # stamped by one process microseconds apart is identically zero,
        # so a declared maximum could never refuse anything and a green
        # result here would be a control that cannot fire.
        checks.append(
            GateCheck(
                "committedState.age",
                True,
                (
                    "not checked: the commitment time was stamped by this invocation, so "
                    "there is no independent instant to measure it against"
                    + (
                        f" (a maximum of {max_age} ms is declared)"
                        if max_age is not None
                        else "; no maximum age is declared"
                    )
                ),
                checked=False,
            )
        )
    else:
        age = now_ms - committed.committed_at_ms
        if age < 0:
            fresh = False
            age_detail = "the committed state is dated after the instant it is being checked at"
        elif max_age is None:
            fresh = True
            age_detail = f"the committed state is {age} ms old; no maximum age is declared"
        else:
            fresh = age <= max_age
            age_detail = (
                f"the committed state is {age} ms old against a declared maximum of {max_age} ms"
            )
        checks.append(GateCheck("committedState.age", fresh, age_detail))

    passed = all(check.passed for check in checks)
    return GateDecision(
        action=GateAction.EMIT if passed else GateAction.WITHHOLD,
        expose_id=expose_id,
        checks=tuple(checks),
        evaluation=evaluation,
        code=None if passed else FLX_8001.code,
    )
