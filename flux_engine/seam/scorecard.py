# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a Simulation declares about its scorecard: the dimensions and the thresholds.

A run whose Simulation pins a profile (docs/spec/PROFILE-SPEC.md) is scored
against it by ``verify`` and ``ship``, per property, per table, per dimension
and overall (docs/spec/SCORECARD-SPEC.md). The Simulation can declare
thresholds on those scores under the namespace's top-level ``scorecard`` key,
and ``ship`` withholds a run that misses one:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        profile: { digest: "sha256:..." }
        scorecard:
          thresholds:
            overall: { mustBeGreaterOrEqualTo: "0.9" }
            dimensions:
              marginals: { mustBeGreaterOrEqualTo: "0.95" }
            tables:
              payment_recovery_moment: { mustBeGreaterThan: "0.8" }
          coverage:
            columns: "0.8"
            relationships: "0.5"

``coverage`` floors the share of the contract the score speaks for
(docs/spec/SCORECARD-SPEC.md §5), not how well the run scored. This module holds what the declaration and the scorecard share, so the two
cannot disagree: the eight dimensions in their fixed order, the dimension each
property kind is scored in, and the thresholds. It reads the block, never a
record or a profile.

The operators are two of the Open Data Contract Standard's quality operators
(bitol-io ODCS 3.2.0, Apache-2.0): ``mustBeGreaterOrEqualTo`` and
``mustBeGreaterThan``. A threshold is a floor on a score, and the other six
operators bound a score from above or exclude a band, which no scorecard
threshold means, so they are refused rather than accepted and read as
something else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError

__all__ = [
    "COVERAGE_KINDS",
    "DIMENSIONS",
    "SCORED_DIMENSIONS",
    "THRESHOLD_OPERATORS",
    "DeclaredProperty",
    "ScorecardDeclaration",
    "Threshold",
    "declaration_from_plan",
    "dimension_of",
    "read_scorecard_block",
    "thresholds_document",
]

#: The eight fidelity dimensions of the scorecard, in the order every scorecard lists
#: them. Structure is whether the values are ones the data has; marginals one
#: column at a time; joint two columns of one table together; then across
#: tables, over time, the declared invariants, the consumer queries and the
#: defect rates.
DIMENSIONS: Tuple[str, ...] = (
    "structure",
    "marginals",
    "joint",
    "crossTable",
    "temporal",
    "invariants",
    "queries",
    "defects",
)

#: The dimensions some property kind of this engine is scored in. The other
#: five have no property kind yet, so a scorecard reports them as not measured
#: and a threshold on one is refused: it could only ever withhold.
SCORED_DIMENSIONS: FrozenSet[str] = frozenset({"structure", "marginals", "joint"})

#: What coverage counts: the contract's columns that carry a scored property, and
#: its declared relationships the profile constrains.
COVERAGE_KINDS: Tuple[str, ...] = ("columns", "relationships")

#: A threshold's operator: ODCS's two floors.
THRESHOLD_OPERATORS: Tuple[str, ...] = ("mustBeGreaterOrEqualTo", "mustBeGreaterThan")

#: ODCS's other six operators, named so a refusal can say why they do not apply.
_OTHER_ODCS_OPERATORS: FrozenSet[str] = frozenset(
    {
        "mustBe",
        "mustNotBe",
        "mustBeLessThan",
        "mustBeLessOrEqualTo",
        "mustBeBetween",
        "mustNotBeBetween",
    }
)

_KINDS_BY_COLUMN: FrozenSet[str] = frozenset(
    {"mean", "quantile", "marginalHistogram", "levelHistogram"}
)

#: A decimal operand: a whole part with no leading zeros and an optional
#: fraction, with no sign and no exponent, and at most 1000 characters (the
#: fidelity block's own bound on a number's length).
_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_LONGEST_OPERAND = 1000


def dimension_of(kind: str, grouped: bool) -> str:
    """The dimension a property of *kind* is scored in.

    ``support`` asks whether every synthetic value is one the reference
    holds, which is the structure of the column rather than its shape, so it
    is scored in ``structure`` with or without ``groupBy``. A ``correlation``
    relates two columns, and so does any property grouped by a second column
    of its table: both are ``joint``. Every other kind describes one column
    and is a marginal.
    """
    if kind == "support":
        return "structure"
    if kind == "correlation" or (grouped and kind in _KINDS_BY_COLUMN):
        return "joint"
    if kind in _KINDS_BY_COLUMN:
        return "marginals"
    raise ValueError(f"no dimension scores a {kind!r} property")


@dataclass(frozen=True)
class DeclaredProperty:
    """What the threshold rules need of one declared property: its kind and grouping."""

    kind: str
    grouped: bool


@dataclass(frozen=True)
class Threshold:
    """One declared floor on one score.

    ``scope`` is ``overall``, ``dimension``, ``table`` or ``coverage``, and
    ``name`` is the dimension, the ``exposeId`` or the coverage kind (``None``
    for ``overall``). A ``coverage`` floor reads a share of the contract, not a
    score, and its operator is ``mustBeGreaterOrEqualTo``. ``declared`` is the
    operand as written, and ``value`` its exact reading.
    """

    scope: str
    name: Optional[str]
    operator: str
    value: Fraction
    declared: str

    @property
    def target(self) -> str:
        """Where the threshold sits in the block: ``overall``, ``dimensions.<name>``..."""
        if self.scope == "overall":
            return "overall"
        prefix = {"dimension": "dimensions", "table": "tables"}.get(self.scope, self.scope)
        return f"{prefix}.{self.name}"

    def met_by(self, score: Optional[Fraction]) -> bool:
        """Whether *score* clears the floor. A score that was never formed does not."""
        if score is None:
            return False
        if self.operator == "mustBeGreaterThan":
            return score > self.value
        return score >= self.value


@dataclass(frozen=True)
class ScorecardDeclaration:
    """The validated ``scorecard`` block: its thresholds, overall first, coverage floors last."""

    thresholds: Tuple[Threshold, ...]


def _refuse(code: ErrorCode, message: str) -> FluxError:
    return FluxError(code, message)


def _mapping(value: Any, where: str, code: ErrorCode) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse(code, f"{where} is a mapping")
    return value


def _keys(value: Mapping[str, Any], allowed: Sequence[str], where: str, code: ErrorCode) -> None:
    unknown = sorted(str(key) for key in value if nfc(str(key)) not in allowed)
    if unknown:
        raise _refuse(code, f"{where} declares {unknown}; it carries {sorted(allowed)}")


def _operand(value: Any, where: str, code: ErrorCode) -> Tuple[Fraction, str]:
    """Read a threshold's operand: an exact decimal in ``[0, 1]``, never a float."""
    if isinstance(value, bool) or isinstance(value, float):
        raise _refuse(code, f"{where} is an exact decimal, never a float or a bool")
    if isinstance(value, int):
        # Checked before it is spelled: an int of thousands of digits cannot
        # be turned into a string at all, and nothing above 1 is a score.
        if value not in (0, 1):
            raise _refuse(code, f"{where} is a score between 0 and 1")
        text = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise _refuse(code, f"{where} is a finite decimal")
        # Range and length before spelling: 1E-9999999 would be written out
        # in ten million digits first.
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)
        if not 0 <= value <= 1 or -exponent > _LONGEST_OPERAND:
            raise _refuse(
                code,
                f"{where} is a score between 0 and 1, written in at most "
                f"{_LONGEST_OPERAND} digits",
            )
        text = format(value, "f")
    elif isinstance(value, str):
        text = value
    else:
        raise _refuse(code, f"{where} is an exact decimal, written as a string")
    if len(text) > _LONGEST_OPERAND or not _DECIMAL.fullmatch(text):
        raise _refuse(
            code,
            f'{where} is a decimal with no sign and no exponent, such as "0.9"',
        )
    exact = Fraction(text)
    if exact > 1:
        raise _refuse(code, f"{where} is a score between 0 and 1")
    return exact, text


def _threshold(
    value: Any, *, scope: str, name: Optional[str], where: str, code: ErrorCode
) -> Threshold:
    rule = _mapping(value, where, code)
    operators = [nfc(str(key)) for key in rule]
    if len(operators) != 1:
        raise _refuse(
            code,
            f"{where} holds exactly one operator: " + " or ".join(THRESHOLD_OPERATORS),
        )
    operator = operators[0]
    if operator in _OTHER_ODCS_OPERATORS:
        raise _refuse(
            code,
            f"{where} uses {operator}; a threshold is a floor on a score, so it is "
            + " or ".join(THRESHOLD_OPERATORS),
        )
    if operator not in THRESHOLD_OPERATORS:
        raise _refuse(
            code,
            f"{where} uses {operator!r}; the operator is " + " or ".join(THRESHOLD_OPERATORS),
        )
    exact, declared = _operand(next(iter(rule.values())), f"{where}.{operator}", code)
    if operator == "mustBeGreaterThan" and exact == 1:
        raise _refuse(
            code,
            f"{where}: no score is above 1, so mustBeGreaterThan 1 could only withhold; "
            "use mustBeGreaterOrEqualTo 1 to ask for a perfect score",
        )
    return Threshold(scope=scope, name=name, operator=operator, value=exact, declared=declared)


def _normalised(mapping: Mapping[str, Any], where: str, code: ErrorCode) -> Dict[str, Any]:
    """*mapping* keyed by NFC name, refusing two spellings of one name.

    Two keys that differ only in their Unicode normal form name one dimension
    or one table; keeping either silently would drop the other's floor.
    """
    found: Dict[str, Any] = {}
    for key, raw in mapping.items():
        name = nfc(str(key))
        if name in found:
            raise _refuse(code, f"{where} names {name!r} twice, in two spellings of one name")
        found[name] = raw
    return found


def _scored_dimensions(emits: Mapping[str, Optional[Sequence[DeclaredProperty]]]) -> FrozenSet[str]:
    """The dimensions some declared fidelity property is scored in."""
    found = set()
    for properties in emits.values():
        for entry in properties or ():
            try:
                found.add(dimension_of(entry.kind, entry.grouped))
            except ValueError:
                # Not a kind this engine scores; the fidelity block's own
                # reader refuses it with its own code.
                continue
    return frozenset(found)


def _dimension_thresholds(
    value: Any,
    emits: Mapping[str, Optional[Sequence[DeclaredProperty]]],
    *,
    where: str,
    code: ErrorCode,
) -> List[Threshold]:
    """``thresholds.dimensions``: one floor per named dimension, in the order of :data:`DIMENSIONS`."""
    by_dimension = _mapping(value, where, code)
    if not by_dimension:
        raise _refuse(code, f"{where} names at least one dimension")
    _keys(by_dimension, DIMENSIONS, where, code)
    normalised = _normalised(by_dimension, where, code)
    scored = _scored_dimensions(emits)
    found: List[Threshold] = []
    for dimension in (name for name in DIMENSIONS if name in normalised):
        here = f"{where}.{dimension}"
        if dimension not in SCORED_DIMENSIONS:
            raise _refuse(
                code,
                f"{here}: no property kind of this engine is scored in {dimension} yet, so "
                "the threshold could only withhold; remove it",
            )
        if dimension not in scored:
            raise _refuse(
                code,
                f"{here}: no declared fidelity property is scored in {dimension}, so the "
                "threshold could only withhold; declare one, or remove the threshold",
            )
        found.append(
            _threshold(
                normalised[dimension], scope="dimension", name=dimension, where=here, code=code
            )
        )
    return found


def _table_thresholds(
    value: Any,
    emits: Mapping[str, Optional[Sequence[DeclaredProperty]]],
    *,
    where: str,
    code: ErrorCode,
) -> List[Threshold]:
    """``thresholds.tables``: one floor per named table, sorted by ``exposeId``."""
    by_table = _mapping(value, where, code)
    if not by_table:
        raise _refuse(code, f"{where} names at least one table")
    normalised = _normalised(by_table, where, code)
    found: List[Threshold] = []
    for expose_id in sorted(normalised):
        here = f"{where}.{expose_id}"
        if expose_id not in emits:
            raise _refuse(
                code,
                f"{here}: this Simulation does not emit {expose_id!r} (it emits {sorted(emits)})",
            )
        if emits[expose_id] is None:
            raise _refuse(
                code,
                f"{here}: {expose_id!r} declares no fidelity block, so its table has no "
                "score; declare one under emits, or remove the threshold",
            )
        found.append(
            _threshold(normalised[expose_id], scope="table", name=expose_id, where=here, code=code)
        )
    return found


def _coverage_thresholds(
    value: Any,
    emits: Mapping[str, Optional[Sequence[DeclaredProperty]]],
    *,
    where: str,
    code: ErrorCode,
) -> List[Threshold]:
    """``coverage``: one floor per named kind, in the order of :data:`COVERAGE_KINDS`."""
    declared = _mapping(value, where, code)
    if not declared:
        raise _refuse(code, f"{where} names at least one of {list(COVERAGE_KINDS)}")
    _keys(declared, COVERAGE_KINDS, where, code)
    normalised = _normalised(declared, where, code)
    if not _scored_dimensions(emits):
        raise _refuse(
            code,
            f"{where}: no emit declares a fidelity property this engine scores, so no column "
            "carries a scored property and the floor could only withhold; declare one under "
            "emits, or remove the floor",
        )
    found: List[Threshold] = []
    for kind in (name for name in COVERAGE_KINDS if name in normalised):
        exact, text = _operand(normalised[kind], f"{where}.{kind}", code)
        found.append(
            Threshold(
                scope="coverage",
                name=kind,
                operator="mustBeGreaterOrEqualTo",
                value=exact,
                declared=text,
            )
        )
    return found


def read_scorecard_block(
    block: Any,
    *,
    pinned: bool,
    emits: Mapping[str, Optional[Sequence[DeclaredProperty]]],
    where: str,
    code: ErrorCode,
) -> ScorecardDeclaration:
    """Validate the ``scorecard`` block of one Simulation, or refuse it with *code*.

    *pinned* says whether the Simulation pins a profile: a scorecard is
    formed against one, so a block without a pin is refused. *emits* maps
    every ``exposeId`` the Simulation emits to its declared fidelity
    properties, or to ``None`` when the emit declares no fidelity block.

    Refused, each with a message that says what to change:

    * a key other than ``thresholds``, and under it other than ``overall``,
      ``dimensions`` and ``tables``; a block that declares no threshold;
    * a dimension that is not one of :data:`DIMENSIONS`, one no property kind
      of this engine is scored in, and one no declared property is scored in:
      each could only withhold;
    * a table the Simulation does not emit, and one that declares no fidelity
      block, whose score is never formed;
    * an operator other than :data:`THRESHOLD_OPERATORS`, and an operand that
      is not an exact decimal between 0 and 1;
    * a ``coverage`` key other than :data:`COVERAGE_KINDS`, and a coverage
      floor that is not an exact decimal between 0 and 1.
    """
    root = _mapping(block, where, code)
    if not pinned:
        raise _refuse(
            code,
            f"{where} declares thresholds on a scorecard, and a scorecard is formed "
            "against a pinned profile; pin one under profile, or remove the block",
        )
    _keys(root, ("thresholds", "coverage"), where, code)
    if "thresholds" not in root and "coverage" not in root:
        raise _refuse(code, f"{where} holds thresholds or coverage")
    thresholds_where = f"{where}.thresholds"
    declared = _mapping(root["thresholds"], thresholds_where, code) if "thresholds" in root else {}
    _keys(declared, ("overall", "dimensions", "tables"), thresholds_where, code)

    found: List[Threshold] = []
    if "overall" in declared:
        overall = _threshold(
            declared["overall"],
            scope="overall",
            name=None,
            where=f"{thresholds_where}.overall",
            code=code,
        )
        if not _scored_dimensions(emits):
            raise _refuse(
                code,
                f"{thresholds_where}.overall: no emit declares a fidelity property this engine "
                "scores, so the overall score is never formed and the threshold could only "
                "withhold; declare one under emits, or remove the threshold",
            )
        found.append(overall)

    if "dimensions" in declared:
        found.extend(
            _dimension_thresholds(
                declared["dimensions"], emits, where=f"{thresholds_where}.dimensions", code=code
            )
        )
    if "tables" in declared:
        found.extend(
            _table_thresholds(
                declared["tables"], emits, where=f"{thresholds_where}.tables", code=code
            )
        )

    if "thresholds" in root and not found:
        raise _refuse(code, f"{thresholds_where} declares at least one threshold")
    if "coverage" in root:
        found.extend(
            _coverage_thresholds(root["coverage"], emits, where=f"{where}.coverage", code=code)
        )
    return ScorecardDeclaration(thresholds=tuple(found))


def thresholds_document(declaration: ScorecardDeclaration) -> Dict[str, Any]:
    """The thresholds as ``plan.json`` records them, in declaration order (overall first).

    The plan holds what the run is gated on, so ``verify`` and ``ship`` read
    the thresholds through the plan's digest rather than from a bundle
    directory nothing binds to the run. A coverage floor is a row of the same
    list, targeted ``coverage.columns`` or ``coverage.relationships``.
    """
    return {
        "thresholds": [
            {
                "target": threshold.target,
                "operator": threshold.operator,
                "value": threshold.declared,
            }
            for threshold in declaration.thresholds
        ]
    }


def declaration_from_plan(document: Any, *, code: ErrorCode) -> Optional[ScorecardDeclaration]:
    """Read back what :func:`thresholds_document` wrote, or ``None`` when the plan has none.

    The plan is bound by its own digest before this is read, so a document
    this does not recognise is refused with *code*, never guessed at.
    """
    if document is None:
        return None
    root = _mapping(document, "plan.json scorecard", code)
    rows = root.get("thresholds")
    if not isinstance(rows, list) or not rows:
        raise _refuse(code, "plan.json scorecard holds a list of thresholds")
    found: List[Threshold] = []
    for index, row in enumerate(rows):
        where = f"plan.json scorecard.thresholds[{index}]"
        entry = _mapping(row, where, code)
        target = entry.get("target")
        operator = entry.get("operator")
        if not isinstance(target, str) or operator not in THRESHOLD_OPERATORS:
            raise _refuse(code, f"{where} names a target and an operator")
        scope, _, name = target.partition(".")
        if target == "overall":
            scope_name: Tuple[str, Optional[str]] = ("overall", None)
        elif scope == "dimensions" and name in DIMENSIONS:
            scope_name = ("dimension", name)
        elif scope == "tables" and name:
            scope_name = ("table", name)
        elif scope == "coverage" and name in COVERAGE_KINDS:
            scope_name = ("coverage", name)
        else:
            raise _refuse(
                code,
                f"{where}.target is overall, dimensions.<name>, tables.<exposeId> or "
                "coverage.<columns|relationships>",
            )
        exact, declared = _operand(entry.get("value"), f"{where}.value", code)
        found.append(
            Threshold(
                scope=scope_name[0],
                name=scope_name[1],
                operator=str(operator),
                value=exact,
                declared=declared,
            )
        )
    return ScorecardDeclaration(thresholds=tuple(found))
