# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``verify`` and ``ship`` share to score a run against its pinned profile.

docs/spec/SCORECARD-SPEC.md. The run's plan records the pin and the
thresholds (:data:`~flux_engine.stages.plan.PROFILE_PIN_KEY`,
:data:`~flux_engine.stages.plan.SCORECARD_PLAN_KEY`), so both stages read
them through the plan's digest, never from a bundle directory that nothing
binds to the run. The profile itself is never copied into the run directory
or the evidence pack: it is handed to the command, as it is handed to
``plan``, and paired with the pin before anything is read from it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

from flux_engine.core.errors import FluxError
from flux_engine.seam.profile import PinnedProfile, require_profile
from flux_engine.seam.resolve import FLX_3105
from flux_engine.seam.scorecard import ScorecardDeclaration, declaration_from_plan
from flux_engine.stages.plan import PROFILE_PIN_KEY, SCORECARD_PLAN_KEY

if TYPE_CHECKING:
    from flux_engine.concordance.compiler import CompiledFidelity
    from flux_engine.concordance.scorecard import ContractShape, Scorecard, TableInput
    from flux_engine.concordance.verifier import FidelityEvaluation, Row

__all__ = [
    "build_run_scorecard",
    "contract_shape",
    "refused_evaluation",
    "evaluate_against_profile",
    "min_credibility",
    "pair_run_profile",
    "plan_declaration",
    "plan_pin",
    "profile_check_detail",
    "unpaired_detail",
]


def plan_pin(plan_document: Mapping[str, Any]) -> Optional[str]:
    """The digest of the profile the run's plan records as pinned, or ``None``."""
    value = plan_document.get(PROFILE_PIN_KEY)
    return None if value is None else str(value)


def plan_declaration(plan_document: Mapping[str, Any]) -> Optional[ScorecardDeclaration]:
    """The scorecard thresholds the run's plan records, or ``None``."""
    return declaration_from_plan(plan_document.get(SCORECARD_PLAN_KEY), code=FLX_3105)


def pair_run_profile(
    plan_document: Mapping[str, Any],
    profile: Union[str, Path, None],
    *,
    where: str,
) -> Optional[PinnedProfile]:
    """Pair the profile a command was handed with the pin the run's plan records.

    The rules are ``plan``'s (:func:`~flux_engine.seam.profile.require_profile`):
    no pin and no profile is ``None``; a profile with no pin, or a pin with
    no profile, is ``FLX-3109``; a profile whose digest is not the pinned one
    is ``FLX-3110``; a document that is not a profile is ``FLX-5036``. A
    caller that reports an unpaired pin rather than refusing it checks
    :func:`plan_pin` first.
    """
    return require_profile(
        plan_pin(plan_document), Path(profile) if profile is not None else None, where=where
    )


def unpaired_detail(pinned: str) -> str:
    """What to do when the run pins a profile and none was handed over."""
    return (
        f"this run's plan pins profile {pinned}; pass that profile with --profile to score the run"
    )


def min_credibility(fidelity_input: Mapping[str, Any]) -> Optional[str]:
    """The emit's declared ``minCredibility``, as the decimal string the run recorded, or ``None``.

    The FLUX schema keeps it a number between 0 and 1 and the run records it
    as an exact decimal string; anything else in the run's record is refused
    (``FLX-3105``) rather than read as some other floor.
    """
    emit = fidelity_input.get("emit")
    value = emit.get("minCredibility") if isinstance(emit, Mapping) else None
    if value is None:
        return None
    text = str(value)
    exact: Optional[Fraction] = None
    try:
        number = Decimal(text)
        exponent = number.as_tuple().exponent
        # Range and length first: Fraction(Decimal("1e-99999999")) would
        # spell out a hundred million digits before anything was refused.
        if (
            number.is_finite()
            and isinstance(exponent, int)
            and -1000 <= exponent <= 0
            and 0 <= number <= 1
        ):
            exact = Fraction(number)
    except (InvalidOperation, ValueError, OverflowError):
        exact = None
    if isinstance(value, (bool, float)) or exact is None:
        raise FluxError(
            FLX_3105,
            "the run records an emit minCredibility that is not a decimal between 0 and 1",
        )
    return text


def contract_shape(contract_document: Any, expose_id: str) -> Optional["ContractShape"]:
    """One emit's contract columns and declared relationships, for the scorecard's coverage.

    The columns are the expose's contract schema; the relationships are the
    ones the expose declares as the child (``semantics.relationships``,
    FLUID 0.7.7). ``None`` for a contract this engine cannot read, which
    coverage then leaves out rather than guess at: the plan refused such a
    contract before the run existed.
    """
    from flux_engine.concordance.scorecard import ContractShape, DeclaredLink
    from flux_engine.core.canonical import nfc
    from flux_engine.seam.keys import compile_keys
    from flux_stats.fidelity import expose_from_contract

    try:
        expose = expose_from_contract(contract_document, expose_id)
        keys = compile_keys(contract_document)
    except FluxError:
        return None
    declared = keys.expose(expose_id).relationships if keys is not None else ()
    return ContractShape(
        columns=tuple(expose.column_names),
        relationships=tuple(
            DeclaredLink(
                parent=link.target,
                parent_columns=tuple(nfc(column) for column in link.to_columns),
                child=link.source,
                child_columns=tuple(nfc(column) for column in link.from_columns),
            )
            for link in declared
        ),
    )


def evaluate_against_profile(
    compiled: "CompiledFidelity", pinned: PinnedProfile, synthetic_rows: Sequence["Row"]
) -> "FidelityEvaluation":
    """Compare one emit's declared properties with the pinned profile (PROFILE-SPEC.md §7)."""
    from flux_engine.concordance.verifier import ClearProfile

    return ClearProfile(
        compiled=compiled, profile=pinned.profile, synthetic_rows=synthetic_rows
    ).evaluate()


def refused_evaluation(expose_id: str, code: str, reason: str) -> "FidelityEvaluation":
    """An evaluation that compared no property, refused for *reason*.

    For an emit stopped before its comparison (no recorded block, a stream
    that cannot be read): its table is then refused on the scorecard, and
    says why, rather than listed with nothing in it.
    """
    from flux_engine.concordance.verifier import FidelityEvaluation, Refusal

    return FidelityEvaluation(
        expose_id=expose_id, outcomes=(), refusals=(Refusal(code=code, reason=reason),)
    )


def profile_check_detail(evaluation: "FidelityEvaluation", *, name_failures: bool = False) -> str:
    """One line saying what an emit's comparison with the profile found, naming no value.

    With *name_failures* the line also names each property outside its margin
    or refused, by id and by what it measured: a gate check is read on its
    own, with no property list beside it.
    """
    total = len(evaluation.outcomes)
    noun = "property" if total == 1 else "properties"
    if evaluation.refused:
        count = len(evaluation.refusals)
        return (
            f"{count} fail-closed condition{'' if count == 1 else 's'} met against the "
            "pinned profile"
        )
    censored = len(evaluation.censored)
    if evaluation.within:
        detail = f"{total - censored} of {total} declared {noun} within the margin against the pinned profile"
        return detail + (f"; {censored} censored, not scored" if censored else "")
    if total and censored == total:
        return (
            "the pinned profile withheld every group of every declared property, so "
            "nothing was scored"
        )
    outside = len(evaluation.outside)
    detail = f"{outside} of {total} declared {noun} outside the margin against the pinned profile"
    if name_failures:
        detail += ": " + ", ".join(
            f"{outcome.property_id} ({outcome.kind.value} of {', '.join(outcome.columns)})"
            for outcome in evaluation.outside
        )
    return detail


def build_run_scorecard(
    *,
    plan_document: Mapping[str, Any],
    run_id: str,
    run_digest: str,
    pinned: PinnedProfile,
    tables: Sequence["TableInput"],
) -> "Scorecard":
    """The run's scorecard, from one :class:`TableInput` per emit."""
    from flux_engine.concordance.scorecard import build_scorecard

    return build_scorecard(
        run_id=run_id,
        plan_digest=str(plan_document.get("planDigest")),
        run_digest=run_digest,
        profile_digest=pinned.digest.value,
        tables=tables,
        declaration=plan_declaration(plan_document),
        profile=pinned.profile,
    )
