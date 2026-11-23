# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The privacy grade of a run, and the minimum its columns set, from what a stage holds (ADR 0020).

:mod:`flux_engine.evidence.grades` grades a run from the document of the profile it pins, and
:mod:`flux_engine.governance.privacy` turns a column's sensitivity into a floor. This module joins
them for the stages: ``plan`` has the profile and records the grade in the plan, ``apply`` reads the
recorded grade (it is handed no profile for ``--run-id``), and ``ship`` is handed the profile and
grades it again. A plan that pins a profile and records no grade was made by an engine that did not
grade, and is read as ``P1``: nothing shows it to be better.

A run's grade is the weakest of its profile's and the grade each generator plugin the bundle
declares states (ADR 0032). The declaration is in the bundle, which ``inputDigest`` covers, so every
stage reads the plugins' grades from the bundle and never from a plan.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern, weakest
from flux_engine.evidence.grades import (
    ColumnFloor,
    GeneratorGrade,
    ProfileGrade,
    RunGrade,
    grade_profile,
)
from flux_engine.governance.privacy import (
    FLX_4005,
    effective_floors,
    enforce_minimum_grades,
    sensitivities_of,
)
from flux_engine.seam.resolve import (
    ResolvedEmit,
    declared_generators,
    privacy_floors,
    profile_reference,
    resolve_all_emits,
)

__all__ = [
    "GRADE_KEY",
    "column_floors",
    "generator_grades",
    "recorded_grade",
    "require_declared_grade",
    "require_minimum_grade",
    "require_recorded_minimum",
    "run_grade",
    "with_generators",
]

#: ``plan.json``'s key for the grade of the run. Written only when the plan pins a profile or the
#: bundle declares a generator plugin, so a bundle that does neither plans exactly as before.
GRADE_KEY = "privacyGrade"


def generator_grades(simulation: Mapping[str, Any], *, where: str) -> Tuple[Pattern, ...]:
    """The grade each generator plugin *simulation* declares states, in ``exposeId`` order."""
    return tuple(item.grade for item in declared_generators(simulation, where=where))


def with_generators(grade: Pattern, simulation: Mapping[str, Any], *, where: str) -> Pattern:
    """*grade*, or the weaker grade of a generator plugin the bundle declares (ADR 0032).

    The plugins are read from the bundle, which ``inputDigest`` covers, and never from a plan.
    """
    return weakest([grade, *generator_grades(simulation, where=where)])


def recorded_grade(plan_document: Mapping[str, Any], *, pinned: bool) -> Pattern:
    """The grade a recorded plan names: ``P0`` when the Simulation pins no profile, ``P1`` when it
    pins one and the plan names no grade (a plan from an engine that did not grade).

    ``pinned`` is read from the Simulation (``profile_reference``), never from the plan's own
    ``profileDigest``, which a plan written by hand could leave out. The plugins a bundle declares
    are not read here: :func:`with_generators` takes the weaker of this and theirs.
    """
    if not pinned:
        return Pattern.P0
    recorded = plan_document.get(GRADE_KEY)
    try:
        return Pattern(recorded)
    except ValueError:
        return Pattern.P1


def require_recorded_minimum(
    loaded: Any, plan_document: Mapping[str, Any], *, simulation_id: Optional[str]
) -> None:
    """Refuse (``FLX-4005``) a recorded plan whose grade does not meet the minimum its bundle sets.

    For a stage that commits a run without generating one (``apply --run-id`` of a staged plan): it
    reads the grade the plan records, and whether the Simulation pins a profile, from the bundle.
    """
    from flux_engine.runtime import resolve_cast

    cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
    emits = resolve_all_emits(loaded, simulation_id=cast.simulation_id)
    pinned = profile_reference(cast.simulation, where=cast.simulation_id) is not None
    require_minimum_grade(
        with_generators(
            recorded_grade(plan_document, pinned=pinned),
            cast.simulation,
            where=cast.simulation_id,
        ),
        emits,
        cast.simulation,
        where=cast.simulation_id,
    )


def _labels(
    emits: Sequence[ResolvedEmit],
) -> Tuple[Mapping[Tuple[str, str], str], List[Tuple[str, str]]]:
    columns = [
        (resolved.expose_id, name) for resolved in emits for name in resolved.compile().column_names
    ]
    labels = sensitivities_of([(resolved.expose_id, resolved.expose) for resolved in emits])
    return labels, columns


def require_minimum_grade(
    grade: Pattern,
    emits: Sequence[ResolvedEmit],
    simulation: Mapping[str, Any],
    *,
    where: str,
    grade_of: str = "the run's grade",
) -> None:
    """Refuse (``FLX-4005``) a run of *grade* with a labelled column whose floor it does not meet.

    *grade_of* names the profile whose grade this is, when it is not the pinned one.

    A bundle that labels no column with a sensitivity that has a floor is not checked at all.
    """
    floors = effective_floors(privacy_floors(simulation, where=where))
    labels, columns = _labels(emits)
    labels = {key: label for key, label in labels.items() if label in floors}
    _refuse_labelled_reference_columns(emits, labels)
    if labels:
        enforce_minimum_grades(grade, labels, floors, columns, grade_of=grade_of)


def _refuse_labelled_reference_columns(
    emits: Sequence[ResolvedEmit], labels: Mapping[Tuple[str, str], str]
) -> None:
    """Refuse (``FLX-4005``) a reference expose with a column whose sensitivity sets a minimum grade.

    A reference expose is released whole (ADR 0023 section 2): its rows are the profile's
    published rows, with no noise on them, so no grade covers a column a sensitivity floor
    applies to. The refusal does not depend on the run's grade.
    """
    reference = {resolved.expose_id for resolved in emits if resolved.is_reference}
    held = sorted(
        (expose_id, name, label)
        for (expose_id, name), label in labels.items()
        if expose_id in reference
    )
    if held:
        shown = "; ".join(
            f"{expose_id}.{name} is labelled {label}" for expose_id, name, label in held[:10]
        )
        raise FluxError(
            FLX_4005,
            f"{shown}; the expose is a reference expose, which is released whole with the rows "
            "its profile publishes and no noise, so no grade covers a column with a minimum; "
            "drop the label if the column holds no personal data, or generate the expose from "
            "statistics with a fidelity block instead",
        )


def require_declared_grade(
    emits: Sequence[ResolvedEmit], simulation: Mapping[str, Any], *, where: str
) -> None:
    """Refuse (``FLX-4005``) a run whose declared plugins alone put it below a labelled column's floor.

    The run's grade is at most the weakest grade a plugin states, whatever its profile is, so a
    refusal from the plugins' grades alone is a refusal the full grade would make. It reads the
    bundle only, so a command that goes on to run a plugin refuses first, before any plugin code
    is found or imported. A bundle that declares no plugin is not checked here.
    """
    declared = generator_grades(simulation, where=where)
    if declared:
        require_minimum_grade(weakest(declared), emits, simulation, where=where)


def column_floors(
    grade: Pattern, emits: Sequence[ResolvedEmit], simulation: Mapping[str, Any], *, where: str
) -> Tuple[ColumnFloor, ...]:
    """Each column of the run with its sensitivity, its minimum and whether *grade* meets it."""
    from flux_engine.core.grades import at_least

    floors = effective_floors(privacy_floors(simulation, where=where))
    labels, columns = _labels(emits)
    rows: List[ColumnFloor] = []
    for expose_id, name in columns:
        label = labels.get((expose_id, name))
        floor: Optional[Pattern] = floors.get(label) if label is not None else None
        rows.append(
            ColumnFloor(expose_id, name, label, floor, floor is None or at_least(grade, floor))
        )
    return tuple(rows)


def run_grade(
    emits: Sequence[ResolvedEmit],
    simulation: Mapping[str, Any],
    profile: Optional[Mapping[str, Any]],
    *,
    pinned: bool,
    recorded: Optional[Pattern] = None,
    where: str,
) -> RunGrade:
    """The run's grade: the weakest of the pinned profile's, any *recorded* one and every plugin's.

    The profile is graded from its document. The plugins are the ones the bundle declares
    (ADR 0032), each with the grade and, for ``P4``, the account it declares.
    """
    graded = grade_profile(profile, pinned=pinned)
    pattern = weakest([graded.pattern, recorded]) if recorded is not None else graded.pattern
    if pattern is not graded.pattern:
        graded = ProfileGrade(
            pattern,
            graded.reasons + ("the plan records a lower grade",),
            graded.accounting,
            graded.pinned,
        )
    generators = tuple(
        GeneratorGrade(
            expose_id=item.expose_id,
            plugin=item.plugin,
            distribution=item.distribution,
            version=item.version,
            pattern=item.grade,
            unit=item.privacy.unit if item.privacy is not None else None,
            loss=item.privacy.loss if item.privacy is not None else None,
            accounting=item.privacy.accounting if item.privacy is not None else None,
        )
        for item in declared_generators(simulation, where=where)
    )
    result = RunGrade(graded, (), generators)
    return RunGrade(
        graded, column_floors(result.pattern, emits, simulation, where=where), generators
    )
