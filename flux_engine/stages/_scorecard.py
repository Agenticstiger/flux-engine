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

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.errors import FluxError
from flux_engine.seam.overlay import Overlay, overlay_from_plan
from flux_engine.seam.profile import PinnedProfile, require_profile
from flux_engine.seam.resolve import FLX_3105
from flux_engine.seam.scorecard import ScorecardDeclaration, declaration_from_plan
from flux_engine.stages.plan import OVERLAY_PLAN_KEY, PROFILE_PIN_KEY, SCORECARD_PLAN_KEY

if TYPE_CHECKING:
    from flux_engine.concordance.calendar_lines import CalendarChecks
    from flux_engine.concordance.compiler import CompiledFidelity
    from flux_engine.concordance.exposure_lines import ExposureChecks
    from flux_engine.concordance.queries import QueriesDimension
    from flux_engine.concordance.scorecard import (
        ContractShape,
        PropertyScore,
        Scorecard,
        TableInput,
    )
    from flux_engine.concordance.verifier import FidelityEvaluation, Row
    from flux_engine.stages._module_checks import ModuleChecks
    from flux_stats.profile import LinkGrouping

__all__ = [
    "LineFailure",
    "build_run_scorecard",
    "children_failures",
    "contract_shape",
    "copula_alphas",
    "line_failures",
    "refused_evaluation",
    "evaluate_against_profile",
    "min_credibility",
    "pair_run_profile",
    "plan_declaration",
    "plan_overlay",
    "plan_pin",
    "NOT_SCORED_DETAIL",
    "profile_check_detail",
    "unscored_note",
    "unused_statistics",
    "unpaired_detail",
    "with_line_failures",
]


@dataclass(frozen=True)
class LineFailure:
    """One line of a run that fails it, beyond the scorecard's own thresholds.

    ``kind`` is ``rules`` (a rule off its band or a link not whole), ``calendar`` or ``exposure``;
    ``expose_id`` is the emit the line is about; ``line`` is the line ``verify`` prints for it.
    """

    kind: str
    expose_id: str
    line: str


def line_failures(
    module: Optional["ModuleChecks"],
    calendar: Optional["CalendarChecks"],
    exposure: Optional["ExposureChecks"],
) -> List[LineFailure]:
    """The module, calendar and exposure lines that fail a run, in the order ``verify`` lists them.

    The one reading of what fails: ``verify`` and ``ship`` both ask here, so a run one of them fails
    the other fails too. A calendar line on a column no ``rates`` entry shapes is not among them.
    """
    found = [LineFailure("rules", expose_id, line) for expose_id, line in _failed(module)]
    if calendar is not None:
        found.extend(
            LineFailure("calendar", each.expose_id, each.line()) for each in calendar.failed()
        )
    if exposure is not None:
        found.extend(
            LineFailure("exposure", each.expose_id, each.line()) for each in exposure.failed()
        )
    return found


def children_failures(
    module: Optional["ModuleChecks"], scorecard: Optional["Scorecard"]
) -> List[LineFailure]:
    """The children-per-parent rows of *scorecard* that are outside their tolerance, as lines that fail a run.

    A ``counts`` entry that lays a relationship out *given* a parent column declares the tolerance of
    the row's classes, so a row of such a relationship that is outside it (in a class, or in the
    whole) is a failed check, as the assertion for the row (``success: false``) says. Any other row,
    a relationship with no entry or one with no ``given``, informs and fails nothing. A group is
    named by its ordinal, never by its key.
    """
    from flux_engine.concordance.module_scores import CHILDREN_PER_PARENT
    from flux_engine.concordance.verifier import OUTSIDE

    if module is None or scorecard is None or not any(entry.given for entry in module.counts):
        return []
    declared = {(entry.emit, entry.relationship) for entry in module.counts if entry.given}
    found: List[LineFailure] = []
    for table in scorecard.tables:
        for row in table.properties:
            document = row.document or {}
            if (
                row.kind != CHILDREN_PER_PARENT
                or row.outcome != OUTSIDE
                or (table.expose_id, document.get("relationship")) not in declared
            ):
                continue
            found.append(LineFailure("children", table.expose_id, _children_line(row)))
    return found


def _children_line(row: "PropertyScore") -> str:
    """The line for one children-per-parent row outside its tolerance: the groups, distances and tolerance."""
    document = row.document or {}
    name = f"{document.get('child')}.{document.get('relationship')}"
    tolerance = document.get("tolerance")
    grouping = document.get("groupBy") or (document.get("groupByBins") or {}).get("column")
    if "groups" not in document:
        return (
            f"children {name}: the parents' children per parent are outside the tolerance "
            f"{tolerance} of the profile's (distance {document.get('distance')})"
        )
    outside = [group for group in document["groups"] if group.get("outcome") == "outside"]
    parts = [f"group #{group['ordinal']} (distance {group.get('distance')})" for group in outside]
    return (
        f"children {name}, given {grouping}: {', '.join(parts)} outside the tolerance {tolerance} "
        "of the profile's"
    )


def _failed(module: Optional["ModuleChecks"]) -> List[Any]:
    return module.failed() if module is not None else []


def with_line_failures(scorecard: "Scorecard", failures: Sequence[LineFailure]) -> "Scorecard":
    """*scorecard* with the lines that fail the run: its ``met`` is then false (SCORECARD-SPEC.md section 6)."""
    return replace(scorecard, line_failures=tuple(each.line for each in failures))


def plan_pin(plan_document: Mapping[str, Any]) -> Optional[str]:
    """The digest of the profile the run's plan records as pinned, or ``None``."""
    value = plan_document.get(PROFILE_PIN_KEY)
    return None if value is None else str(value)


def plan_overlay(plan_document: Mapping[str, Any]) -> Optional[Overlay]:
    """The profile overlay the run's plan records (ADR 0030 section 7), or ``None``."""
    return overlay_from_plan(plan_document.get(OVERLAY_PLAN_KEY), code=FLX_3105)


def copula_alphas(plan_document: Mapping[str, Any]) -> Dict[str, str]:
    """The shrinkage ``alpha`` of each emit whose plan holds a copula, by ``exposeId``.

    The plan states ``copula: {alpha, factor}`` for such an emit only (ADR 0035 decision 5); an
    entry that is not that shape is left out.
    """
    emits = plan_document.get("emits")
    found: Dict[str, str] = {}
    for expose_id, entry in (emits.items() if isinstance(emits, Mapping) else ()):
        copula = entry.get("copula") if isinstance(entry, Mapping) else None
        alpha = copula.get("alpha") if isinstance(copula, Mapping) else None
        if isinstance(alpha, str):
            found[str(expose_id)] = alpha
    return found


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

    When the plan records an overlay, the profile returned is scored with it: its ``profile`` is
    the overlaid profile and its ``document`` and ``digest`` stay the pinned profile's, so the
    grade and the pin read the profile the Simulation pins (ADR 0030 section 7).
    """
    pinned = require_profile(
        plan_pin(plan_document), Path(profile) if profile is not None else None, where=where
    )
    overlay = plan_overlay(plan_document)
    if pinned is not None and overlay is not None:
        return pinned.with_overlay(overlay, code=FLX_3105)
    return pinned


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
    compiled: "CompiledFidelity",
    pinned: PinnedProfile,
    synthetic_rows: Sequence["Row"],
    link_groupings: Optional[Mapping[str, "LinkGrouping"]] = None,
) -> "FidelityEvaluation":
    """Compare one emit's declared properties with the pinned profile (PROFILE-SPEC.md §7).

    *link_groupings* name the groupings of the properties grouped by a link, whose linked
    column the rows then carry (:func:`flux_engine.concordance.linked.join_linked_columns`).
    """
    from flux_engine.concordance.verifier import ClearProfile

    return ClearProfile(
        compiled=compiled,
        profile=pinned.profile,
        synthetic_rows=synthetic_rows,
        link_groupings=link_groupings or {},
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


#: What a table whose fidelity block lists no property is reported as, where a verdict would stand.
NOT_SCORED_DETAIL = "the fidelity block declares no property, so nothing is verified"


def unused_statistics(expose_id: str, pinned: PinnedProfile) -> Tuple[str, ...]:
    """The statistics the pinned profile publishes over a table's columns, each as ``id of column``.

    A statistic counts when one of its groups is published, so a property could be scored
    against it. For a table whose fidelity block lists no property, these are the checks the
    profile supports that nothing declares.
    """
    table = pinned.profile.tables.get(expose_id)
    if table is None:
        return ()
    found: List[str] = []
    for name in sorted(table.columns):
        for statistic in table.columns[name].statistics:
            text = f"{statistic.identifier} of {name}"
            if any(group.published for group in statistic.groups) and text not in found:
                found.append(text)
    return tuple(found)


def unscored_note(statistics: Sequence[str]) -> str:
    """The note that says a table with no declared property could be checked against the profile."""
    shown = ", ".join(statistics[:3]) + (
        f" and {len(statistics) - 3} more" if len(statistics) > 3 else ""
    )
    return (
        f"the pinned profile publishes {shown}, which a property of this table could be scored "
        "against; declare one under the fidelity block's properties to check it"
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
    if evaluation.unscored:
        return NOT_SCORED_DETAIL
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
    module: Optional["ModuleChecks"] = None,
    calendar: Optional["CalendarChecks"] = None,
    queries: Optional["QueriesDimension"] = None,
) -> "Scorecard":
    """The run's scorecard, from one :class:`TableInput` per emit.

    *module* holds the rules and relationships checked on the run's rows
    (:mod:`flux_engine.stages._module_checks`): they enter the scorecard as the fact and
    relationship rows of format 1-3-0 (SCORECARD-SPEC.md §3), scored against the pinned profile.
    *calendar* holds the calendar lines of the run (:mod:`flux_engine.concordance.calendar_lines`):
    each histogram they scored enters as a ``temporal`` row of format 1-4-0, read from the
    comparison the lines already made.
    *queries* holds the consumers' queries as counted on the run: they score the ``queries``
    dimension (format 1-4-0, SCORECARD-SPEC.md §3a).
    """
    from flux_engine.concordance.calendar_lines import calendar_rows
    from flux_engine.concordance.scorecard import build_scorecard

    rows: Dict[str, List["PropertyScore"]] = {}
    for source in (
        module.scorecard_rows(pinned.profile) if module is not None else {},
        calendar_rows(calendar),
    ):
        for expose_id, found in source.items():
            rows.setdefault(expose_id, []).extend(found)

    return build_scorecard(
        copula_alphas=copula_alphas(plan_document),
        run_id=run_id,
        plan_digest=str(plan_document.get("planDigest")),
        run_digest=run_digest,
        profile_digest=pinned.digest.value,
        tables=tables,
        declaration=plan_declaration(plan_document),
        overlay=(
            (pinned.overlay.name, pinned.overlay.digest.value)
            if pinned.overlay is not None
            else None
        ),
        profile=pinned.profile,
        module_rows=rows or None,
        queries=queries,
    )
