# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit's copula: its declaration, and what a plan checks of it (ADR 0035 decision 4).

An emit's fidelity block may hold one ``copula``:

.. code-block:: yaml

    fidelity:
      copula:
        columns: [amount, balance, opened_at]
        rankCorrelations:
          measure: kendall
          pairs:
            - { columns: [amount, balance], value: "0.42" }

:func:`read_copula` reads the declaration with nothing but the declaration in hand: its shape, the
distinct columns, the measure and the pairs. :func:`check_copula` checks it against the emit's
planned columns. Everything refused is ``FLX-3402``, naming where and never quoting a value, except
a run on kernel 0.1.0, which has no copula path: that is ``FLX-3105``, naming the kernel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from flux_engine.core import copula as core_copula
from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.statistics import FLX_3402
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.copula import COPULA_KEY

if TYPE_CHECKING:
    from flux_engine.generator.rows import ColumnPlan

__all__ = [
    "LEAST_COLUMNS",
    "MEASURES",
    "MOST_COLUMNS",
    "CopulaDeclaration",
    "CopulaDraw",
    "check_copula",
    "declares_copula",
    "plan_copula_draw",
    "read_copula",
]

#: How many columns a copula holds (ADR 0035 decision 4).
LEAST_COLUMNS = 2
MOST_COLUMNS = 64

#: The measures a rank correlation is published in.
MEASURES = ("kendall", "spearman")

_VALUE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_KINDS = (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)


@dataclass(frozen=True)
class CopulaDeclaration:
    """What a ``copula`` key says: its columns in draw order, its measure and its pairs.

    Each pair is ``(first, second, value)`` in the order written, with *value* an exact decimal in
    ``[-1, 1]``. A pair of columns that is not listed has the value 0.
    """

    columns: Tuple[str, ...]
    measure: str
    pairs: Tuple[Tuple[str, str, Decimal], ...]

    def pair_values(self) -> Mapping[Tuple[str, str], Decimal]:
        """The pairs as :func:`flux_engine.core.copula.plan_copula` takes them."""
        return {(first, second): value for first, second, value in self.pairs}


def _refuse(message: str) -> FluxError:
    return FluxError(FLX_3402, f"fidelity.copula: {message}")


def _mapping(value: Any, keys: Sequence[str], where: str) -> Mapping[str, Any]:
    """*value* as a mapping that holds exactly the keys of *keys*."""
    if not isinstance(value, Mapping):
        raise _refuse(f"{where} is a mapping")
    found = {nfc(key) if isinstance(key, str) else key for key in value}
    unknown = [key for key in found if key not in keys]
    if unknown:
        raise _refuse(f"{where} holds a key that is not one of {list(keys)}")
    missing = [key for key in keys if key not in found]
    if missing:
        raise _refuse(f"{where} has no {missing[0]}")
    return {nfc(key): item for key, item in value.items()}


def _names(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _refuse(f"{where} is a list of column names")
    names: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise _refuse(f"{where} is a list of column names")
        names.append(nfc(item))
    return tuple(names)


def _value(raw: Any) -> Decimal:
    """A pair's ``value``: a decimal in ``[-1, 1]``, as text, an integer or a ``Decimal``."""
    if isinstance(raw, bool) or isinstance(raw, float):
        raise _refuse("a pair's value is a decimal written as text, never a float")
    if isinstance(raw, Decimal):
        number = raw
    elif isinstance(raw, int):
        number = Decimal(raw)
    elif isinstance(raw, str) and _VALUE.fullmatch(raw):
        number = Decimal(raw)
    else:
        raise _refuse("a pair's value is a decimal in [-1, 1]")
    if not number.is_finite() or not -1 <= number <= 1:
        raise _refuse("a pair's value is a decimal in [-1, 1]")
    return number


def declares_copula(fidelity: Any) -> bool:
    """Whether the fidelity block *fidelity* holds a ``copula`` key, whatever it says."""
    return isinstance(fidelity, Mapping) and any(
        isinstance(key, str) and nfc(key) == COPULA_KEY for key in fidelity
    )


def read_copula(fidelity: Any) -> Optional[CopulaDeclaration]:
    """The copula *fidelity* declares, or ``None`` when its block holds no ``copula`` key.

    Refuses (``FLX-3402``) a key that is not a mapping of exactly ``columns`` and
    ``rankCorrelations``; ``columns`` that are not 2 to 64 distinct names;
    ``rankCorrelations`` that are not exactly a ``measure`` of ``kendall`` or ``spearman`` and a
    list of ``pairs``; a pair that is not exactly two distinct columns of the copula and a
    ``value`` in ``[-1, 1]``; and a pair of columns listed twice, in either order.
    """
    if not isinstance(fidelity, Mapping):
        return None
    keyed = [key for key in fidelity if isinstance(key, str) and nfc(key) == COPULA_KEY]
    if not keyed:
        return None
    block = _mapping(fidelity[keyed[0]], ("columns", "rankCorrelations"), "the copula")
    columns = _names(block["columns"], "columns")
    if not LEAST_COLUMNS <= len(columns) <= MOST_COLUMNS:
        raise _refuse(f"columns hold {LEAST_COLUMNS} to {MOST_COLUMNS} columns")
    if len(set(columns)) != len(columns):
        raise _refuse("columns name each column once")
    ranks = _mapping(block["rankCorrelations"], ("measure", "pairs"), "rankCorrelations")
    measure = ranks["measure"]
    if not isinstance(measure, str) or nfc(measure) not in MEASURES:
        raise _refuse("rankCorrelations.measure is kendall or spearman")
    listed = ranks["pairs"]
    if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
        raise _refuse("rankCorrelations.pairs is a list")
    pairs: List[Tuple[str, str, Decimal]] = []
    seen: Set[FrozenSet[str]] = set()
    for position, item in enumerate(listed):
        where = f"rankCorrelations.pairs[{position}]"
        entry = _mapping(item, ("columns", "value"), where)
        named = _names(entry["columns"], f"{where}.columns")
        if len(named) != 2 or named[0] == named[1]:
            raise _refuse(f"{where}.columns names two different columns")
        if any(name not in columns for name in named):
            raise _refuse(f"{where}.columns names a column the copula does not hold")
        key = frozenset(named)
        if key in seen:
            raise _refuse(f"{where} lists a pair of columns that an earlier pair lists")
        seen.add(key)
        pairs.append((named[0], named[1], _value(entry["value"])))
    return CopulaDeclaration(columns=columns, measure=nfc(measure), pairs=tuple(pairs))


def check_copula(
    declaration: CopulaDeclaration,
    plans: Sequence["ColumnPlan"],
    *,
    set_elsewhere: Mapping[str, str],
) -> None:
    """Refuse (``FLX-3402``) a copula column that the emit cannot draw through the copula.

    Each column is one of the emit's, a number or timestamp column drawn from ``bins`` and
    ``binWeights``, given nothing and allocated by nothing, that no other column is given, and
    that nothing else sets: *set_elsewhere* maps each column a key, a rule, a ``lifetimes``,
    ``within`` or ``rates`` entry, a copy or a hidden declaration sets to what sets it. A column
    has one joint (ADR 0035 decision 9), so a column that is a ``given`` parent or child is not
    a copula column. A bin of positive weight that holds no grid value is refused where the
    laws are built.
    """
    by_name = {nfc(plan.column.name): plan for plan in plans}
    parents = {nfc(parent) for plan in plans for parent in plan.statistics.parents}
    for name in declaration.columns:
        plan = by_name.get(name)
        if plan is None:
            raise _refuse(f"columns names {name!r}, which is not a column of the emit")
        reason = _why_not(plan, name, parents, set_elsewhere)
        if reason is not None:
            raise _refuse(f"{name!r} is {reason}")


def _why_not(
    plan: "ColumnPlan", name: str, parents: Set[str], set_elsewhere: Mapping[str, str]
) -> Optional[str]:
    """Why *plan*'s column cannot be a copula column, or ``None`` when it can."""
    statistics = plan.statistics
    if name in set_elsewhere:
        return f"{set_elsewhere[name]}, and a copula column is set by nothing else"
    if plan.column_type is ColumnType.IDENTIFIER or plan.column.logical_type.kind not in _KINDS:
        return "not a number or timestamp column"
    if statistics.parents or statistics.given_link is not None:
        return "drawn given another column, and a column has one joint"
    if name in parents:
        return "a column another is drawn given, and a column has one joint"
    if statistics.allocation is not None:
        return "allocated exactly, which a copula cannot read"
    if (
        plan.family is not Family.UNIFORM_EMPIRICAL
        or statistics.bins is None
        or statistics.bin_weights is None
    ):
        return "not drawn from bins and binWeights"
    return None


class CopulaDraw:
    """What the plan decides of an emit's copula: the matrix's shrinkage and its factor.

    ``alpha`` is the shrinkage as canonical decimal text and :attr:`digest` the sha256 of the
    factor's binary64 words (:func:`flux_engine.core.copula.plan_copula`). The emit's plan document
    gains ``copula: {alpha, factor}`` only when the emit declares one, so it enters ``planDigest``
    there and nowhere else.
    """

    def __init__(
        self,
        declaration: CopulaDeclaration,
        plan: core_copula.CopulaPlan,
        *,
        sim_id: str,
        expose_id: str,
    ) -> None:
        self.columns = declaration.columns
        self.measure = declaration.measure
        self.pairs = tuple((a, b, str(value)) for a, b, value in declaration.pairs)
        self.alpha = plan.alpha
        self.digest = plan.digest
        self.factor = plan.factor
        self.sim_id = sim_id
        self.expose_id = expose_id

    def _identity(self) -> Tuple[Any, ...]:
        return (
            self.columns,
            self.measure,
            self.pairs,
            self.alpha,
            self.digest,
            self.sim_id,
            self.expose_id,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CopulaDraw) and self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    @property
    def shrunk(self) -> bool:
        """Whether the declared matrix was shrunk, so every latent correlation was scaled."""
        return Decimal(self.alpha) > 0

    def as_document(self) -> Dict[str, str]:
        """The plan's ``copula`` member: ``{alpha, factor}``."""
        return {"alpha": self.alpha, "factor": self.digest}


def plan_copula_draw(declaration: CopulaDeclaration, *, sim_id: str, expose_id: str) -> CopulaDraw:
    """Run the plan of ADR 0035 decision 5 for *declaration*."""
    plan = core_copula.plan_copula(
        declaration.columns, declaration.measure, declaration.pair_values()  # type: ignore[arg-type]
    )
    return CopulaDraw(declaration, plan, sim_id=sim_id, expose_id=expose_id)
