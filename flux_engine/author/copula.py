# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One copula per table: published rank correlations draw the number columns together (ADR 0035).

A profile of format 1-4-0 can publish ``kendallTau/1`` or ``spearmanRho/1`` for a pair of number
or timestamp columns. ``fluxsim init`` reads them into one ``fidelity.copula`` over the columns of
the table that it draws from ``bins`` and ``binWeights``, so the draw keeps each column's histogram
and the pairs' order.

**Which columns.** A column joins the copula when the generator can draw it through one and
nothing else decides it: a number or timestamp column that init declares from bins and weights,
with no ``given`` and no ``allocation``, that no other column is drawn given, that is in no clique,
and that no key, relationship, rule, ``lifetimes``, ``within``, ``rates``, history or copy sets. A
column has one joint (ADR 0035 decision 9), so what the Chow-Liu forest and the cliques draw stays
theirs. A column that no published pair of the other columns names is left out as well, since the
copula would hold it at zero with every other.

**Tie-heavy columns.** The statistics count a tied pair as neither concordant nor discordant
and carry no tie correction (ADR 0035 decision 4), so a column that ties often draws a weaker
statistic than the one the profile published. A column's *tie share* is the probability that two
independent draws of it are equal, ``sum(w_i ** 2 / p_i)`` over its bins, ``w_i`` the share of a
bin and ``p_i`` the number of grid values it holds. A column whose tie share is at least
:data:`TIE_SHARE` is left to the cliques. The rule reads the declared bins and the grid, which are
public, and no record.

**The values.** An exact statistic's value is ``S / C(N, 2)`` or ``3 A4 / (N (N^2 - 1))``, a noisy
one is the released number clamped to ``[-1, 1]``, both read by the profile reader, and written as
a decimal rounded half to even at 12 places. A copula has one measure, Kendall's when the profile
publishes any pair of its columns in it, and Spearman's otherwise. A pair the profile does not
publish in that measure is held at 0, which the bundle says as an assumption. The plan may shrink
the matrix towards the identity to make it a correlation matrix (ADR 0035 decision 5); the
evidence states by how much.

Nothing here reads a record: it reads numbers a profiler released.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.basis import Label, assumed
from flux_engine.author.columns import TOLERANCE_REASON, ColumnPlan, PropertyPlan
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import domain_value
from flux_engine.seam.contract import LogicalKind
from flux_stats.fidelity import DEFAULT_RANK_TOLERANCE
from flux_stats.rank_correlation import KENDALL_TAU_ID, RANK_IDENTIFIERS, SPEARMAN_RHO_ID

__all__ = [
    "MOST_COPULA_COLUMNS",
    "TIE_SHARE",
    "CopulaPlan",
    "declare_copula",
    "has_rank_statistics",
    "tie_share",
]

#: A column whose tie share reaches this is left to the cliques: two independent draws of it are
#: equal at least one time in twenty, which weakens a rank correlation by about as much.
TIE_SHARE = Fraction(1, 20)

#: The most columns a copula holds (ADR 0035 decision 4). Beyond it the columns with the fewest
#: published pairs are left out, name order breaking ties.
MOST_COPULA_COLUMNS = 64

_NUMBER_KINDS = (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)
_GIVEN_KEYS = ("given", "levelWeightsGiven", "binWeightsGiven")
_MEASURES = {KENDALL_TAU_ID: "kendall", SPEARMAN_RHO_ID: "spearman"}
_NAMES = {"kendall": "Kendall's tau", "spearman": "Spearman's rho"}
_FIRST_PLACES = 12
_SHOWN = 5

#: Why the copula is not written for a run on kernel 0.1.0, which has no copula path.
_NO_KERNEL = "a new plan runs kernel 0.1.0, which has no copula path, so no copula is written"


@dataclass
class CopulaPlan:
    """What ``init`` made of one table's rank correlations: the declaration, and what was said of it."""

    #: The ``fidelity.copula`` value, or ``None`` when no copula is written.
    declaration: Optional[Dict[str, Any]] = None
    columns: Tuple[str, ...] = ()
    measure: str = ""
    #: A ``rankCorrelation`` property for each pair the copula holds.
    properties: List[PropertyPlan] = field(default_factory=list)
    #: Statistics of the profile the copula's values rest on, each with how.
    reads: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)
    #: Statistics looked at and passed over, each with why.
    passed: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)
    #: The sentences a person reads under the table: what the copula holds, and each column left
    #: out with why. Choices and reasons, not assumptions.
    notes: List[str] = field(default_factory=list)
    #: The assumptions the copula makes: a pair held at 0 where the profile published none.
    assumptions: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Pair:
    """One rank correlation of a pair of columns: where it is, its measure, its value if published."""

    tokens: Tuple[str, ...]
    measure: str
    value: Optional[Fraction]
    noisy: bool = False
    #: A noisy value as released, before it was clamped to ``[-1, 1]``; ``None`` for an exact one.
    released: Optional[Fraction] = None


def has_rank_statistics(table: Any) -> bool:
    """Whether the profile's *table* publishes a rank correlation of any pair."""
    return any(
        statistic.identifier in RANK_IDENTIFIERS
        for pair in (table.pairs if table is not None else ())
        for statistic in pair.statistics
    )


def _read_pairs(table: Any, expose_id: str) -> Dict[FrozenSet[str], List[_Pair]]:
    """Every rank correlation of the table, by the set of its two columns, in document order."""
    found: Dict[FrozenSet[str], List[_Pair]] = {}
    for index, pair in enumerate(table.pairs):
        for position, statistic in enumerate(pair.statistics):
            measure = _MEASURES.get(statistic.identifier)
            if measure is None:
                continue
            tokens = (
                "tables",
                expose_id,
                "columnPairs",
                str(index),
                "statistics",
                str(position),
            )
            group = statistic.groups[0] if statistic.groups else None
            value = group.value if group is not None and group.published else None
            released = (
                group.noisy[0] if group is not None and group.published and group.noisy else None
            )
            found.setdefault(frozenset(pair.columns), []).append(
                _Pair(tokens, measure, value, statistic.noise is not None, released)
            )
    return found


def _value_text(value: Fraction) -> str:
    """*value* as a decimal rounded half to even at 12 places, with no trailing zero."""
    units = round(value * 10**_FIRST_PLACES)
    if units == 0:
        return "0"
    sign = "-" if units < 0 else ""
    digits = str(abs(units)).rjust(_FIRST_PLACES + 1, "0")
    whole, fraction = digits[:-_FIRST_PLACES], digits[-_FIRST_PLACES:].rstrip("0")
    return f"{sign}{whole}.{fraction}" if fraction else f"{sign}{whole}"


def _grid_values(low: Decimal, high: Decimal, step: Decimal, *, last: bool) -> int:
    """How many grid values the bin ``[low, high)`` holds; the last bin is closed above."""
    span = Fraction(high) - Fraction(low)
    count = int(span // Fraction(step))
    return count + 1 if last else count


def tie_share(column: Any, plan: ColumnPlan) -> Fraction:
    """The probability that two independent draws of the column's declared histogram are equal.

    ``sum(w_i ** 2 / p_i)`` over the bins, ``w_i`` the share of bin ``i`` and ``p_i`` the number of
    grid values it holds (the generator draws uniformly over them). Raises ``ValueError`` when a
    bin of positive weight holds no grid value, which the generator refuses, and ``FluxError`` when
    an edge is not one the column reads.
    """
    declaration = plan.declaration
    step = Decimal(1).scaleb(-(column.logical_type.scale or 0))
    edges = [domain_value(edge, column, what="a bin edge") for edge in declaration.get("bins", ())]
    weights = [Fraction(Decimal(str(weight))) for weight in declaration.get("binWeights", ())]
    total = sum(weights, Fraction(0))
    if len(edges) != len(weights) + 1 or total <= 0:
        raise ValueError("its bins and weights do not fit")
    share = Fraction(0)
    for index, weight in enumerate(weights):
        if weight == 0:
            continue
        points = _grid_values(edges[index], edges[index + 1], step, last=index == len(weights) - 1)
        if points < 1:
            raise ValueError("a bin that holds weight holds no grid value")
        share += (weight / total) ** 2 / points
    return share


class _Why:
    """The reasons columns are not in the copula, by column name, in the order they were found."""

    def __init__(self) -> None:
        self.by_column: Dict[str, str] = {}

    def add(self, name: str, reason: str) -> None:
        self.by_column.setdefault(name, reason)

    def __contains__(self, name: str) -> bool:
        return name in self.by_column


def _given_parents(columns: Sequence[ColumnPlan]) -> Set[str]:
    """The columns another column of the expose is drawn given."""
    found: Set[str] = set()
    for plan in columns:
        parent = plan.declaration.get("given")
        for name in (
            [parent] if isinstance(parent, str) else parent if isinstance(parent, list) else ()
        ):
            found.add(str(name))
    return found


def _why_not(
    expose: Any,
    plan: ColumnPlan,
    parents: Set[str],
    by_dates: Set[str],
) -> Optional[str]:
    """Why *plan*'s column cannot join the copula, or ``None`` when the generator could draw it."""
    name = plan.name
    declaration = plan.declaration
    context = expose.contexts.get(name)
    if plan.fixed is not None:
        return f"it is {plan.fixed}"
    if name in expose.derives or name in expose.dependents or name in expose.copies:
        return "a rule of the contract sets it"
    if name in expose.aggregated or name in expose.guarded:
        return "a rule of the contract sets it"
    if context is not None and context.column.hidden:
        return "it is a hidden column"
    if name in by_dates:
        return "a lifetimes, within or rates entry names it"
    if name in expose.drawn_by_cliques:
        return "a clique draws it, and a column has one joint"
    if any(key in declaration for key in _GIVEN_KEYS):
        return "it is drawn given another column, and a column has one joint"
    if name in parents:
        return "another column is drawn given it, and a column has one joint"
    if "allocation" in declaration:
        return "it is allocated exactly, which a copula cannot read"
    if "bins" not in declaration or "binWeights" not in declaration:
        return "it is not drawn from a histogram"
    return None


def _tie_reason(expose: Any, plan: ColumnPlan) -> Optional[str]:
    """Why the column's histogram leaves it to the cliques or to the generator's refusal, else ``None``."""
    context = expose.contexts.get(plan.name)
    if context is None:
        return None
    try:
        share = tie_share(context.column, plan)
    except ValueError:
        return "a bin of its histogram holds weight and no grid value, which a copula cannot draw"
    except FluxError:
        return "its bins are not read onto its grid"
    if share >= TIE_SHARE:
        return (
            "its histogram puts so much of its mass on few grid values that two draws of it are "
            f"equal with probability {float(share):.3f}, at least {float(TIE_SHARE):.3f}, and a "
            "rank correlation carries no tie correction, so a clique draws it"
        )
    return None


def _spelled(names: Sequence[str]) -> str:
    from flux_engine.seam.coverage import printable

    return ", ".join(printable(name) for name in names)


def declare_copula(
    expose: Any,
    table: Any,
    *,
    by_dates: Set[str],
    whole: Optional[str],
    counter_based: bool,
) -> Optional[CopulaPlan]:
    """The copula of *expose* from the rank correlations its profile *table* publishes, if any.

    *by_dates* are the columns a ``lifetimes``, ``within`` or ``rates`` entry names, *whole* the
    reason no column of the expose is drawn from its own histogram (a history or a projection
    sets them) or ``None``, and *counter_based* whether the kernel a new plan takes has a copula
    path. ``None`` when the profile publishes no rank correlation of the table, so a profile
    written before them gives the bundle it gave.
    """
    if table is None or not has_rank_statistics(table):
        return None
    from flux_engine.seam.coverage import printable

    published = _read_pairs(table, expose.expose_id)
    result = CopulaPlan()
    if not counter_based or whole is not None:
        why = _NO_KERNEL if not counter_based else f"no copula is written: {whole}"
        result.passed += [(item.tokens, why) for pairs in published.values() for item in pairs]
        result.notes.append(why)
        return result
    out = _Why()
    candidates = _candidates(expose, published, by_dates, out)
    names = _settle(candidates, published, out)
    if len(names) < 2:
        _pass_all(result, published, out, None, names)
        result.notes.append(
            "no copula is written: fewer than two columns have a published rank correlation "
            "with another column that can join one"
        )
    else:
        _declare(result, published, names, out)
    result.notes += [
        f"{printable(name)} is not in the copula: {reason}"
        for name, reason in out.by_column.items()
    ]
    return result


def _candidates(
    expose: Any,
    published: Mapping[FrozenSet[str], List[_Pair]],
    by_dates: Set[str],
    out: _Why,
) -> List[ColumnPlan]:
    """The columns that could join: named by a published pair and not decided elsewhere, tied too often."""
    parents = _given_parents(expose.columns)
    named = {name for pair in published for name in pair}
    candidates: List[ColumnPlan] = []
    for plan in expose.columns:
        if expose.kinds.get(plan.name) not in _NUMBER_KINDS or plan.name not in named:
            continue
        reason = _why_not(expose, plan, parents, by_dates) or _tie_reason(expose, plan)
        if reason is not None:
            out.add(plan.name, reason)
        else:
            candidates.append(plan)
    return candidates


def _settle(
    candidates: Sequence[ColumnPlan],
    published: Mapping[FrozenSet[str], List[_Pair]],
    out: _Why,
) -> List[str]:
    """The names that join: those with a published pair to another, and at most 64, in column order."""
    names = [plan.name for plan in candidates]
    # A column with no published pair to another candidate would sit at zero with every other.
    while True:
        counts = {
            name: sum(
                1
                for other in names
                if other != name
                and any(
                    item.value is not None for item in published.get(frozenset((name, other)), ())
                )
            )
            for name in names
        }
        alone = [name for name in names if counts[name] == 0]
        if not alone:
            break
        for name in alone:
            out.add(
                name,
                "the profile publishes no rank correlation of it with another column that can "
                "join the copula",
            )
        names = [name for name in names if name not in alone]
    if len(names) > MOST_COPULA_COLUMNS:
        keep = sorted(names, key=lambda name: (-counts[name], name))[:MOST_COPULA_COLUMNS]
        for name in names:
            if name not in keep:
                out.add(
                    name,
                    f"a copula holds at most {MOST_COPULA_COLUMNS} columns, and it has the fewest "
                    "published pairs",
                )
        names = [name for name in names if name in keep]
    return names


def _measure_of(
    names: Sequence[str], published: Mapping[FrozenSet[str], List[_Pair]]
) -> Optional[str]:
    """Kendall's when the profile publishes any pair of *names* in it, else Spearman's, else ``None``."""
    for measure in ("kendall", "spearman"):
        for first, second in combinations(names, 2):
            for item in published.get(frozenset((first, second)), ()):
                if item.measure == measure and item.value is not None:
                    return measure
    return None


def _declare(
    result: CopulaPlan,
    published: Mapping[FrozenSet[str], List[_Pair]],
    names: List[str],
    out: _Why,
) -> None:
    measure = _measure_of(names, published) or "kendall"
    pairs: List[Dict[str, Any]] = []
    held: List[Tuple[str, str]] = []
    clamped: List[str] = []
    used: Set[Tuple[str, ...]] = set()
    for first in range(len(names)):
        for second in range(first + 1, len(names)):
            pair = (names[first], names[second])
            chosen = next(
                (
                    item
                    for item in published.get(frozenset(pair), ())
                    if item.measure == measure and item.value is not None
                ),
                None,
            )
            if chosen is None or chosen.value is None:
                held.append(pair)
                continue
            used.add(chosen.tokens)
            pairs.append({"columns": list(pair), "value": _value_text(chosen.value)})
            how = (
                f"its value is the pair's {_NAMES[measure]} in the table's copula, a noisy "
                "value clamped to [-1, 1]"
                if chosen.noisy
                else f"its value is the pair's {_NAMES[measure]} in the table's copula"
            )
            result.reads.append((chosen.tokens, how))
            result.properties.append(_property(pair, measure, chosen.tokens))
            if chosen.released is not None and abs(chosen.released) > 1:
                clamped.append(
                    f"{_spelled(pair)}: the noisy {_NAMES[measure]} released as "
                    f"{_value_text(chosen.released)} is held at {'-1' if chosen.released < 0 else '1'}"
                )
    result.declaration = {
        "columns": list(names),
        "rankCorrelations": {"measure": measure, "pairs": pairs},
    }
    result.columns = tuple(names)
    result.measure = measure
    _pass_all(result, published, out, measure, names, used)
    _notes(result, names, measure, held)
    # A noisy release past the range of a correlation is clamped, and the text says which pair.
    result.notes.extend(clamped)


def _property(pair: Tuple[str, str], measure: str, tokens: Tuple[str, ...]) -> PropertyPlan:
    """The ``rankCorrelation`` property that scores the run's statistic against the profile's."""
    tolerance = {"type": "absolute", "max": DEFAULT_RANK_TOLERANCE["max"]}
    return PropertyPlan(
        {
            "property": "rankCorrelation",
            "columns": list(pair),
            "measure": measure,
            "tolerance": tolerance,
        },
        [Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
        scorable=True,
        reads=[(tokens, "the property is scored against the published rank correlation")],
    )


def _pass_all(
    result: CopulaPlan,
    published: Mapping[FrozenSet[str], List[_Pair]],
    out: _Why,
    measure: Optional[str],
    names: Sequence[str],
    used: Optional[Set[Tuple[str, ...]]] = None,
) -> None:
    """Say why each rank correlation that is not a value of the copula is not read."""
    taken = used or set()
    inside = set(names)
    for pair, items in published.items():
        for item in items:
            if item.tokens in taken or item.value is None:
                continue
            left = [name for name in sorted(pair) if name in out]
            if left:
                why = f"{_spelled(left[:1])} is left out of the table's copula: {out.by_column[left[0]]}"
            elif measure is not None and item.measure != measure and pair <= inside:
                why = (
                    f"the table's copula has one measure, {_NAMES[measure]}, and the profile "
                    f"publishes this pair in {_NAMES[item.measure]} as well"
                )
            elif pair <= inside:
                why = "the profile publishes this pair twice, and the copula reads the first"
            else:
                why = "a column of it is not one of the table's copula"
            result.passed.append((item.tokens, why))


def _notes(
    result: CopulaPlan, names: Sequence[str], measure: str, held: Sequence[Tuple[str, str]]
) -> None:
    result.notes.append(
        f"{_spelled(names)} are drawn together through one Gaussian copula of their "
        f"{_NAMES[measure]}. Each pair's value is the profile's, rounded to {_FIRST_PLACES} "
        "places. The plan may shrink the matrix towards the identity, so that it is a "
        "correlation matrix. That multiplies every correlation by 1 - alpha on the latent "
        "scale. The evidence states alpha."
    )
    if held:
        shown = "; ".join(_spelled(pair) for pair in held[:_SHOWN])
        more = f"; and {len(held) - _SHOWN} more" if len(held) > _SHOWN else ""
        result.assumptions.append(
            f"The profile publishes no {_NAMES[measure]} for {len(held)} pair"
            f"{'s' if len(held) != 1 else ''} of the copula's columns ({shown}{more}), so the "
            "copula holds each at 0, as if the two were independent."
        )
