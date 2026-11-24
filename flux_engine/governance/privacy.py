# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column's sensitivity sets the minimum privacy grade of the run that generates it (ADR 0020).

FLUID 0.7.6 lets a contract label a column with a sensitivity,
``exposes[].contract.schema[].sensitivity``, one of twelve names
(``$defs/sensitivityLevel`` in the FLUID schema). This module turns the label into a floor on the
**grade** of the run (:mod:`flux_engine.evidence.grades`), and refuses a run below it. A run has one
grade, the weakest of the one read from the profile its Simulation pins and the grade each
generator plugin it declares states (ADR 0032), so each labelled column's floor is compared with
that one grade. A grade for each column is a later refinement.

**The built-in floors.**

* ``pii``, ``phi``, ``restricted``, ``pseudonymized`` and ``tokenized`` require ``P3`` or stronger:
  a profile that states a release under differential privacy and holds only noisy statistics, or a
  model from a plugin (``P4``). ``P0``, a run that pins no profile and so reads no data, meets it
  too: nothing in the engine read any data, though it cannot confirm that none shaped the numbers a
  person typed. ``P5``, a learned model with no privacy account, is below every floor.
* ``confidential`` and ``internal`` require ``P1`` or stronger, which every graded run meets.
* Every other label, ``none``, ``cleartext``, ``treated``, ``anonymized`` and ``encrypted``,
  allows any pattern, and so does a column with no label.

**A bundle may only tighten them.** A Simulation can raise the floor of a label under
``extensions['com.agentics.flux/1'].privacy.minimumGrade``, as ``{confidential: P3}``. A value below the
built-in floor of its label is refused (``FLX-3105``, the namespace's own refusal): a bundle that
lowered the floor could clear a refusal the engine means to make, and the declaration is read
by the same people who would be refused.

**Which gate.** Governance refuses a run in this engine today at ``plan``, before any row is drawn
and before the plan is written: sovereignty (``FLX-4001``, the zone a run executes in) and the
skill bindings against ``agentPolicy`` are both checked there. This is the same gate, one more
check beside them, and it raises ``FLX-4005``. A plan that pins a profile records its grade, and
``apply`` and ``ship`` enforce the minimum again, so a plan from an engine that did not check, an
``apply --run-id`` and a ``replay`` meet it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.core.grades import Pattern, at_least, weakest

__all__ = [
    "BUILT_IN_FLOORS",
    "FLX_4005",
    "GradeBelowMinimum",
    "SENSITIVITY_LABELS",
    "Shortfall",
    "effective_floors",
    "enforce_minimum_grades",
    "read_floors",
    "sensitivities_of",
]

FLX_4005 = register(
    "FLX-4005",
    "the run's grade is below the minimum a column's sensitivity sets",
    "A column the contract labels with a sensitivity (exposes[].contract.schema[].sensitivity) "
    "needs a run graded at least as strong as the label sets: pii, phi, restricted, pseudonymized "
    "and tokenized need P3 or stronger (P0, a run that pins no profile and reads no data, meets it "
    "too), confidential and internal need P1 or stronger, and every other label allows any "
    "pattern. A Simulation may raise a floor under "
    "extensions['com.agentics.flux/1'].privacy.minimumGrade and never lower one. The run's grade is "
    "read from the profile it pins and from nothing the bundle says about itself (ADR 0020): P3 only "
    "when the profile states a release under differential privacy, declares its structure public and "
    "holds only noisy statistics, and P1 otherwise. A generator plugin the bundle declares "
    "(extensions['com.agentics.flux/1'].generators, ADR 0032) states P4 or P5, and the run's grade is "
    "the weakest of the profile's and every plugin's: P5, a learned model with no privacy account, is "
    "below every floor. The message names the labelled columns below "
    'their minimum, up to ten and then "and N more", each with the run\'s grade and the minimum. '
    "Nothing was written. A labelled column of a reference expose is refused at any grade: "
    "a reference expose is released whole, with the rows its profile publishes and no noise, so "
    "no grade covers it. Releasing the profile under noise does not help that case; drop the "
    "label if the column holds no personal data, or generate the expose from statistics with a "
    "fidelity block instead of reference: true. For the other cases, leaving the profile off "
    "the command does not help: a bundle that "
    "`init --profile` wrote pins it, and `plan` without it refuses with FLX-3109. To clear a grade "
    "shortfall, release every "
    "statistic the profile publishes under noise (a profile of format 1-2-0 with a privacy block, "
    "PROFILE-SPEC.md section 3a) and run `fluxsim init` again with that profile, or write the "
    "bundle by hand without a profile (a run that pins none grades P0, which every floor admits), "
    "or correct the label if it is wrong.",
)

#: FLUID 0.7.6's ``$defs/sensitivityLevel``, in the schema's order.
SENSITIVITY_LABELS: Tuple[str, ...] = (
    "none",
    "internal",
    "confidential",
    "restricted",
    "pii",
    "phi",
    "cleartext",
    "treated",
    "anonymized",
    "pseudonymized",
    "tokenized",
    "encrypted",
)

#: The floor of each label that has one. A label absent here allows any pattern.
BUILT_IN_FLOORS: Mapping[str, Pattern] = {
    "pii": Pattern.P3,
    "phi": Pattern.P3,
    "restricted": Pattern.P3,
    "pseudonymized": Pattern.P3,
    "tokenized": Pattern.P3,
    "confidential": Pattern.P1,
    "internal": Pattern.P1,
}

_KEY = "minimumGrade"


def read_floors(block: Any, *, where: str, code: ErrorCode) -> Mapping[str, Pattern]:
    """The floors a Simulation's ``privacy`` block raises, or refuse it (*code*).

    The block is ``{minimumGrade: {<sensitivity>: <pattern>}}`` and nothing else. A label that is not
    one of FLUID's, a pattern that is not ``P0``, ``P1``, ``P3``, ``P4`` or ``P5`` (P2 is unassigned), and a
    pattern weaker than the label's built-in floor are refused. Equal to the built-in floor is
    accepted and changes nothing.
    """
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise FluxError(code, f"{where} is a mapping holding {_KEY}")
    unknown = sorted(str(key) for key in block if key != _KEY)
    if unknown:
        raise FluxError(code, f"{where} declares {unknown}; it carries only {_KEY}")
    floors = block.get(_KEY)
    if floors is None:
        return {}
    if not isinstance(floors, Mapping):
        raise FluxError(code, f"{where}.{_KEY} maps a sensitivity to a pattern")
    raised: Dict[str, Pattern] = {}
    for label, value in floors.items():
        label = str(label)
        if label not in SENSITIVITY_LABELS:
            raise FluxError(
                code,
                f"{where}.{_KEY} names {label!r}, which is not one of {list(SENSITIVITY_LABELS)}",
            )
        try:
            pattern = Pattern(value)
        except ValueError:
            raise FluxError(
                code,
                f"{where}.{_KEY}.{label} is a pattern, one of "
                f"{[pattern.value for pattern in Pattern]} (P2 is not assigned)",
            ) from None
        built_in = BUILT_IN_FLOORS.get(label)
        if built_in is not None and not at_least(pattern, built_in):
            raise FluxError(
                code,
                f"{where}.{_KEY}.{label} is {pattern.value}, below the {built_in.value} the "
                f"engine requires of {label}: a Simulation may raise a floor and never lower one",
            )
        raised[label] = pattern
    return raised


def effective_floors(raised: Optional[Mapping[str, Pattern]] = None) -> Mapping[str, Pattern]:
    """Each label's floor: the built-in one, or the Simulation's when it is stronger."""
    floors = dict(BUILT_IN_FLOORS)
    for label, pattern in (raised or {}).items():
        current = floors.get(label)
        if current is None or at_least(pattern, current):
            floors[label] = pattern
    return floors


def sensitivities_of(
    exposes: Sequence[Tuple[str, Mapping[str, Any]]],
) -> Mapping[Tuple[str, str], str]:
    """The sensitivity of each labelled column, by ``(exposeId, column)``.

    *exposes* pairs an expose id with its ``exposes[]`` entry of the contract. A column with no
    label, or a label FLUID does not define, is not listed: the contract's own validation refuses
    the latter, and this reads only what it can name.
    """
    found: Dict[Tuple[str, str], str] = {}
    for expose_id, expose in exposes:
        contract = expose.get("contract") if isinstance(expose, Mapping) else None
        schema = contract.get("schema") if isinstance(contract, Mapping) else None
        for column in schema if isinstance(schema, list) else ():
            if not isinstance(column, Mapping):
                continue
            label, name = column.get("sensitivity"), column.get("name")
            if isinstance(label, str) and label in SENSITIVITY_LABELS and isinstance(name, str):
                found[(expose_id, name)] = label
    return found


@dataclass(frozen=True)
class Shortfall:
    """One labelled column the run's grade does not meet."""

    expose_id: str
    column: str
    sensitivity: str
    grade: Pattern
    minimum: Pattern
    #: The column lies in a table the unit of privacy does not reach, so it counts as ``P1``.
    fixed: bool = False


#: What the message of ``FLX-4005`` tells the caller to do, so a person who never opens the page can.
_REMEDY = (
    "release every statistic of the profile under noise (PROFILE-SPEC.md section 3a) and run "
    "`fluxsim init` again with that profile; or write the bundle by hand without a profile (a run "
    "that pins none grades P0, which every floor admits; dropping --profile from `plan` does not do "
    "it, the bundle pins the profile); where a generator plugin is declared P5, use a model trained "
    "under differential privacy and declare its account (P4); or correct the label if it is wrong"
)


class GradeBelowMinimum(FluxError):
    """Raised by :func:`enforce_minimum_grades` (``FLX-4005``), carrying every shortfall."""

    def __init__(
        self, shortfalls: Sequence[Shortfall], *, grade_of: str = "the run's grade"
    ) -> None:
        shown = "; ".join(
            f"{_name(item.expose_id)}.{_name(item.column)} is {item.sensitivity}, which needs "
            f"{item.minimum.value} or stronger, and "
            + (
                f"the profile's unit of privacy does not reach {_name(item.expose_id)}, whose rows "
                f"the guarantee treats as fixed, so its columns count as {item.grade.value}"
                if item.fixed
                else f"{grade_of} is {item.grade.value}"
            )
            for item in shortfalls[:10]
        )
        more = f"; and {len(shortfalls) - 10} more" if len(shortfalls) > 10 else ""
        super().__init__(FLX_4005, f"{shown}{more}; {_REMEDY}")
        self.shortfalls = tuple(shortfalls)


def _name(text: str) -> str:
    from flux_engine.seam.coverage import printable

    return printable(text)


def enforce_minimum_grades(
    grade: Pattern,
    sensitivities: Mapping[Tuple[str, str], str],
    floors: Mapping[str, Pattern],
    columns: Sequence[Tuple[str, str]],
    *,
    grade_of: str = "the run's grade",
    fixed: Collection[str] = (),
) -> None:
    """Refuse (``FLX-4005``) a run whose grade is below the floor of a labelled column.

    *grade_of* names the profile the grade belongs to, for the message, when it is not the run's own.

    *fixed* are the exposes whose rows the profile's unit of privacy does not reach (ADR 0037
    decision 1): the guarantee treats their rows as the same in every neighbouring data set, so it
    says nothing about them, and a labelled column of such an expose counts as ``P1`` here whatever
    the run's grade, and whether or not a noisy statistic touches it.

    *grade* is the run's one grade, *sensitivities* the labels by ``(exposeId, column)``, *floors*
    each label's floor and *columns* every column the run emits. A labelled column that is not one
    of them is refused as well: a label the gate cannot place is a label it would otherwise skip.
    """
    known = set(columns)
    short: List[Shortfall] = []
    for (expose_id, name), label in sorted(sensitivities.items()):
        if (expose_id, name) not in known:
            raise FluxError(
                FLX_4005,
                f"{_name(expose_id)}.{_name(name)} is labelled {label} and is not a column the run "
                "emits, so its minimum grade cannot be checked",
            )
        floor = floors.get(label)
        held = weakest([grade, Pattern.P1]) if expose_id in fixed else grade
        if floor is not None and not at_least(held, floor):
            short.append(Shortfall(expose_id, name, label, held, floor, expose_id in fixed))
    if short:
        raise GradeBelowMinimum(short, grade_of=grade_of)
