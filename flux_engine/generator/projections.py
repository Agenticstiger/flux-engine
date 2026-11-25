# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's rows, taken from the history it projects (ADR 0027).

A projected emit declares no ``rows``. Its count is worked out at plan time from the history's
layouts alone, and each of its rows names the history row it is read from. One entry of
:data:`KINDS` stands for each kind of projection that has a generator, with two members:

* ``count``: the number of rows the history gives the projection;
* ``sources``: for a range of the projection's rows, the history row each is taken from.

Change events (section 4, :mod:`flux_engine.generator.events`) and periodic rows (section 5,
:mod:`flux_engine.generator.periods`) take their own members of :class:`ProjectionKind`, and
build their rows from the history's rows and the emit's own cells rather than copy one history
row a row.

A snapshot (section 3) is one row per entity, in entity order: each row is the entity's last
version, every column its final value, the rules included. ``ended: drop`` leaves out the entities
the history ended, so its count is the entities less the ended ones. It draws nothing and adds no
path.

:func:`project_plan` turns a planned emit into a projected one: it sets the emit's ``rows`` to the
derived count and attaches a :class:`ProjectionPlan`, which :func:`projection_rows` reads when a
slice of the emit is asked for. Every process that writes a slice works out what the slice needs
of the history once (:func:`sources_of`) and keeps it. A projection draws nothing, so the plan
document holds the count and the history's digest, and no seed path is added.

Every refusal is ``FLX-3403``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

import numpy as np

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.core.sizing import FLX_3405, MOST_ROWS
from flux_engine.generator import events as _events
from flux_engine.generator import periods as _periods
from flux_engine.generator.families import Family
from flux_engine.generator.history import HistoryPlan, plan_history
from flux_engine.generator.rows import (
    ColumnPlan,
    GeneratorPlan,
    generate_row_slice,
    is_binned,
    level_index,
    with_rules,
)
from flux_engine.generator.versionview import clear_views
from flux_engine.seam.projections import (
    EVENTS_KEY,
    GENERATED_PROJECTIONS,
    PERIODS_KEY,
    PROJECTION_KEYS,
    SNAPSHOTS_KEY,
    events_entry_of,
    periods_entry_of,
    snapshot_entry_of,
)

__all__ = [
    "KINDS",
    "ProjectionKind",
    "ProjectionPlan",
    "chain_of",
    "derived_rows",
    "fallback_notes",
    "held_givens",
    "implied_marginal",
    "refuse_rows",
    "projection_of",
    "projection_rows",
    "project_plan",
    "refuse",
    "sources_of",
]

#: How many sources a process keeps (each holds an array over the projection's rows).
_SOURCES_KEPT = 4
_SOURCES: Dict[Tuple[Any, ...], "np.ndarray"] = {}
#: The projected emits a process made whole for a window over siblings, newest last.
_WHOLE_KEPT = 4
_WHOLE: Dict[Tuple[Any, ...], Tuple[Tuple[Any, ...], ...]] = {}


def _code() -> Any:
    from flux_engine.generator import relations

    return relations.FLX_3403


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports the generator's rows)."""
    return FluxError(_code(), message)


refuse = _refuse


@dataclass(frozen=True)
class ProjectionPlan:
    """What a projected emit's plan carries: its kind, the history's plan and the entry's options.

    ``history`` is the plan of the emit the entry's ``of`` names, as the relational plan left it.
    ``ended`` is a snapshot's ``ended`` (``keep`` or ``drop``) and ``entry`` the entry of an
    events or a periods projection. ``root`` is the run root a kind's functions read; it is set
    for the moment of a call and is no part of the plan. ``held`` names the columns of the version
    the entry sets that a column of the emit is drawn given: their levels are the version's, held
    in the row before the emit's own cells are drawn (ADR 0027, the amendment of 2026-11-25).
    ``fallback`` names those of them the history may hold a value no level of the emit's column
    names in: a column drawn given such a column is drawn from its own distribution there.
    ``marginals`` holds that distribution for each such column that has one implied by the
    declarations (:func:`implied_marginal`): the column, then each of its levels with its exact
    share (``"n/d"``); a fallback column with none is drawn from its declared support, evenly.
    """

    kind: str
    of: str
    history: GeneratorPlan
    ended: str = "keep"
    entry: Any = None
    root: Optional[SeedTreeRoot] = field(default=None, compare=False, repr=False)
    window: Optional[Tuple[int, int]] = None
    held: Tuple[str, ...] = ()
    fallback: Tuple[str, ...] = ()
    marginals: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = ()

    def as_document(self) -> Dict[str, Any]:
        """The ``projection`` member of the plan's document: where the rows come from."""
        document: Dict[str, Any] = {
            "kind": self.kind,
            "of": self.of,
            "ended": self.ended,
            "history": self.history.digest().value,
        }
        optional = {
            "entry": None if self.entry is None else self.entry.as_member(),
            "heldGivens": list(self.held) or None,
            "fallbackGivens": list(self.fallback) or None,
            "fallbackMarginals": {name: dict(shares) for name, shares in self.marginals} or None,
        }
        document.update({key: value for key, value in optional.items() if value is not None})
        return document


@dataclass(frozen=True)
class ProjectionKind:
    """One kind of projection: how many rows it has and which history row each is read from.

    ``sources`` is, for a snapshot, an array of the history row each row copies. A kind that
    builds its rows itself gives ``rows``, which takes the plan, what ``sources`` returned, the
    range of rows and the run root.
    """

    count: Callable[[HistoryPlan, ProjectionPlan], int]
    sources: Callable[[HistoryPlan, ProjectionPlan], Any]
    rows: Optional[
        Callable[[GeneratorPlan, Any, int, int, SeedTreeRoot], Tuple[Tuple[Any, ...], ...]]
    ] = None


# -- the history's layout -----------------------------------------------------------------------


def chain_of(history: GeneratorPlan, root: SeedTreeRoot) -> HistoryPlan:
    """The version layout of *history*'s emit: its entities, versions per entity and ended entities.

    Worked out once per process (:func:`~flux_engine.generator.history.plan_history` keeps it).
    """
    from flux_engine.generator.relations import history_pick_of

    pick = history_pick_of(history)
    if pick is None:
        raise _refuse(
            f"projection of {history.expose.expose_id!r}: the emit has no history to project"
        )
    if pick.parent_lifetime is not None:
        from flux_engine.generator.relations import bound_history_of

        return bound_history_of(history, root)
    return plan_history(
        root,
        history.sim_id,
        history.expose.expose_id,
        pick.entry,
        kind=pick.kind,
        rows=pick.rows,
        entities=pick.entities,
        time_bounds=(pick.window_start,),
    )


# -- snapshots ----------------------------------------------------------------------------------


def _kept_entities(chain: HistoryPlan, projection: ProjectionPlan) -> "np.ndarray":
    """The entities a snapshot holds, in entity order: all of them, or the live ones under ``drop``."""
    if projection.ended == "drop" and chain.ended is not None:
        return np.flatnonzero(~chain.ended).astype(np.int64)
    return np.arange(chain.entities, dtype=np.int64)


def _snapshot_count(chain: HistoryPlan, projection: ProjectionPlan) -> int:
    """``E`` less the ended entities under ``ended: drop`` (ADR 0027 section 3), else ``E``."""
    if projection.ended == "drop" and chain.ended is not None:
        return chain.entities - int(chain.ended.sum())
    return chain.entities


def _snapshot_sources(chain: HistoryPlan, projection: ProjectionPlan) -> "np.ndarray":
    """The last version row of each kept entity, in entity order."""
    kept = _kept_entities(chain, projection)
    return (chain.versions.offsets[kept] + chain.versions.counts[kept] - 1).astype(np.int64)


KINDS: Dict[str, ProjectionKind] = {
    SNAPSHOTS_KEY: ProjectionKind(count=_snapshot_count, sources=_snapshot_sources),
    EVENTS_KEY: ProjectionKind(
        count=_events.count, sources=_events.sources, rows=_events.events_rows
    ),
    PERIODS_KEY: ProjectionKind(
        count=_periods.count, sources=_periods.sources, rows=_periods.periods_rows
    ),
}


# -- the entry of an emit -----------------------------------------------------------------------


def projection_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Optional[Tuple[str, str, Any]]:
    """The generated projection *emit*'s bound extension holds: its key, its ``of`` and its options.

    The options are a snapshot's ``ended`` and, for events and periods, the entry itself. ``None``
    for an emit with none, or with a kind this build does not generate (the plan refuses that
    one earlier, :func:`~flux_engine.seam.projections.refuse_projection`).
    """
    if extension is None:
        return None
    for key in PROJECTION_KEYS:
        if key not in extension or key not in GENERATED_PROJECTIONS:
            continue
        found = _entry_of_key(key, extension, emit)
        if found is not None:
            return found
    return None


def _entry_of_key(
    key: str, extension: Mapping[str, Any], emit: str
) -> Optional[Tuple[str, str, Any]]:
    """The key, ``of`` and options of *emit*'s entry under *key*, or ``None`` for none."""
    if key == SNAPSHOTS_KEY:
        snapshot = snapshot_entry_of(extension, emit, refusal=_code())
        return None if snapshot is None else (key, snapshot.of, snapshot.ended)
    entry: Any
    if key == EVENTS_KEY:
        entry = events_entry_of(extension, emit, refusal=_code())
    elif key == PERIODS_KEY:
        entry = periods_entry_of(extension, emit, refusal=_code())
    else:
        return None
    return None if entry is None else (key, entry.of, entry)


# -- the plan -----------------------------------------------------------------------------------


def _projection(
    key: str,
    of: str,
    options: Any,
    history: GeneratorPlan,
    plan: GeneratorPlan,
    after: bool = True,
) -> ProjectionPlan:
    """The projection of *plan*'s emit, after the checks that need the history's plan.

    With *after* (every new plan), a column drawn given a column of the version is drawn given the
    level the version holds; without it (a recorded plan without ``projectedGivens``) it is drawn as
    engine 0.11.1 drew it, given the level the column's own draw made.
    """
    if key not in KINDS:
        raise _refuse(f"{key} ({plan.expose.expose_id}) has no generator")
    if key == SNAPSHOTS_KEY:
        names = {column.column.name for column in history.columns}
        missing = [column.column.name for column in plan.columns if column.column.name not in names]
        if missing:
            raise _refuse(
                f"{key} ({plan.expose.expose_id}): {missing[0]!r} is not a column of {of!r}"
            )
        return ProjectionPlan(key, of, history, options)
    sets = projected_cells(key, options, history, plan)
    held, fallback, marginals = (
        held_givens(key, options, sets, history, plan) if after else ((), (), ())
    )
    if not after:
        _unheld_givens(key, options, sets, plan)
    _refuse_rules_on_projected_cells(key, sets, plan)
    return ProjectionPlan(
        key,
        of,
        history,
        entry=options,
        window=plan.time_bounds,
        held=held,
        fallback=fallback,
        marginals=marginals,
    )


def projected_cells(
    key: str, entry: Any, history: GeneratorPlan, plan: GeneratorPlan
) -> Dict[str, str]:
    """The columns an events or a periods entry sets on its emit's rows, each with the role it sets it in.

    Periods set the period column, each kind's overrides and every column that names a column of the
    history's emit; events set ``op``, ``at``, ``date``, the ``type`` column, the before and after
    images and every column that names a column of the history's emit (ADR 0027 sections 4 and 5).
    """
    return projected_cell_names(
        key,
        entry,
        [column.column.name for column in history.columns],
        [column.column.name for column in plan.columns],
    )


def projected_cell_names(
    key: str, entry: Any, history_columns: Iterable[str], emit_columns: Iterable[str]
) -> Dict[str, str]:
    """:func:`projected_cells` from the names of the history's columns and of the emit's own.

    Known before any plan is built, so a column the entry sets can be told from one the emit draws.
    """
    found, about = _period_cells(entry) if key == PERIODS_KEY else _event_cells(entry)
    version = set(history_columns)
    for name in emit_columns:
        if name in version:
            found.setdefault(name, about)
    return found


def _period_cells(entry: Any) -> Tuple[Dict[str, str], str]:
    """The columns a periods entry sets besides the history's own, and what the others are."""
    found = {entry.column: "its period column"}
    for index, kind in enumerate(entry.kinds):
        for name, _ in kind.overrides:
            found.setdefault(name, f"an override of kinds[{index}]")
    return found, "a column of the version the row carries"


def _event_cells(entry: Any) -> Tuple[Dict[str, str], str]:
    """The columns an events entry sets besides the history's own, and what the others are."""
    found: Dict[str, str] = {}
    for name, role in (
        (entry.op, "its op column"),
        (entry.at, "its at column"),
        (entry.date, "its date column"),
        (entry.type_column, "its type column"),
    ):
        if name is not None:
            found.setdefault(name, role)
    for name, _ in entry.before:
        found.setdefault(name, "a before image")
    for name, _ in entry.after:
        found.setdefault(name, "an after image")
    return found, "a column of the version the event is about"


def _rule_setters(rules: Any) -> Dict[str, str]:
    """The columns a plan's rules set, each with what sets it; none for a plan with no rules."""
    setters: Dict[str, str] = {}
    if rules is None:
        return setters
    for rule in rules.rules:
        for column in (*rule.determines, *rule.to_columns):
            setters.setdefault(column, f"the rule {rule.id!r}")
    for column in rules.determined:
        setters.setdefault(column, "a rule")
    for rate in rules.rates:
        setters.setdefault(rate.column, "its seasonal rates")
    return setters


def _refuse_rules_on_projected_cells(
    key: str, sets: Mapping[str, str], plan: GeneratorPlan
) -> None:
    """Refuse (``FLX-6203``) a rule of the emit that sets a column the projection sets.

    The emit's rules run over its rows once the projection has set its cells, so a rule that set
    one of them would be a second source for it.
    """
    rules = plan.rules
    if rules is None:
        return
    from flux_engine.expr2.errors import FLX_6203

    setters = _rule_setters(rules)
    for column, role in sets.items():
        if column in setters:
            raise FluxError(
                FLX_6203,
                f"column {column!r} has more than one source: the {key} entry "
                f"({plan.expose.expose_id}) sets it as {role}, and {setters[column]} sets it too",
            )
    _refuse_reads_through_projected_keys(key, sets, plan)


def _given_over_projected(
    sets: Mapping[str, str], plan: GeneratorPlan
) -> Iterable[Tuple[ColumnPlan, str]]:
    """Each column the emit draws given a column the projection sets, with that column.

    A column the entry sets itself is replaced as well and is no source of a disagreement.
    """
    for column_plan in plan.columns:
        if column_plan.column.name in sets:
            continue
        for parent in column_plan.statistics.parents:
            if parent in sets:
                yield column_plan, parent


def held_givens(
    key: str, entry: Any, sets: Mapping[str, str], history: GeneratorPlan, plan: GeneratorPlan
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]]:
    """The columns of the version a column of the emit is drawn given, those that may hold no level, and the draws there.

    Such a column's level is the version's: the entry's cells are set before the emit draws its
    own, so the column drawn given it reads the level the row holds (ADR 0027, the amendment of
    2026-11-25). Refused (``FLX-3403``), as a draw this cannot serve, is a column drawn given any
    other cell the entry sets (an op, at, date, type or period column, a kind's override, a before
    or after image), whose values are not drawn from the column's levels; and a column with
    ``allocation: exact``, whose classes are allotted by the draw of its parent.

    A column of the version the history may fill with a value that is none of the emit's levels
    (:func:`_unheld_reason`) is the second member. Where a row holds such a value, the column drawn
    given it is drawn from the marginal its parent's level weights and its own table imply
    (:func:`implied_marginal`, the third member, one entry for each such column), else from its
    own declared support, evenly, and is refused when it has neither.
    """
    own, _ = _period_cells(entry) if key == PERIODS_KEY else _event_cells(entry)
    held: Dict[str, None] = {}
    fallback: Dict[str, None] = {}
    marginals: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    for column_plan, parent in _given_over_projected(sets, plan):
        name = column_plan.column.name
        where = (
            f"{key} entry ({plan.expose.expose_id}): column {name!r} is drawn given {parent!r}, "
            f"which the entry sets as {sets[parent]}"
        )
        _refuse_unservable(where, column_plan, parent, parent in own)
        reason = _unheld_reason(history, plan, parent)
        if reason is not None:
            unnamed = f"{where}, and {reason}, which no level of {parent!r} names"
            shares = _fallback_draw(unnamed, column_plan, parent, history, plan)
            fallback[parent] = None
            if shares is not None:
                marginals[name] = tuple((level, str(share)) for level, share in shares.items())
        held[parent] = None
    return tuple(sorted(held)), tuple(sorted(fallback)), tuple(sorted(marginals.items()))


def _refuse_unservable(where: str, column_plan: ColumnPlan, parent: str, is_own: bool) -> None:
    """Refuse (``FLX-3403``) a column drawn given a cell of the entry's own, or allotted by its parent."""
    name = column_plan.column.name
    if is_own:
        raise _refuse(
            f"{where}; only a column of the version can be drawn given, since its level is the "
            "version's, and this one holds no level the history draws; draw "
            f"{name!r} given a column of the version, or on its own"
        )
    if column_plan.allocated_rows is not None:
        raise _refuse(
            f"{where}; {name!r} declares allocation: exact, whose classes are allotted by the "
            f"draw of {parent!r}, not by the level the row holds; drop the allocation, or "
            f"draw {name!r} on its own"
        )


def _fallback_draw(
    unnamed: str, column_plan: ColumnPlan, parent: str, history: GeneratorPlan, plan: GeneratorPlan
) -> Optional[Dict[str, Fraction]]:
    """The marginal a column draws where its parent holds no level, ``None`` for its support, or the refusal.

    *unnamed* is the refusal's opening: the column, its parent and what sets the value no level names.
    """
    found = implied_marginal(column_plan, parent, history, plan)
    if found is not None:
        return found[0]
    gap = _own_distribution_gap(column_plan)
    if gap is not None:
        name = column_plan.column.name
        raise _refuse(
            f"{unnamed}; {name!r} has no distribution of its own to draw from there ({gap}); "
            f"declare level weights for {parent!r} that name the levels the table of {name!r} is "
            f"keyed by, or declare a support for {name!r} that names the levels its table names, "
            "or draw it on its own"
        )
    return None


def _own_distribution_gap(column_plan: ColumnPlan) -> Optional[str]:
    """Why a column drawn given another has no support to draw from, or ``None`` when it has one.

    The support is its declared ``support``, equal weights over it, and it must name exactly the
    levels the column's table names, so no column given this one meets a level it has no row for.
    """
    statistics = column_plan.statistics
    if column_plan.family is not Family.CATEGORICAL or is_binned(column_plan):
        return "it is drawn from bins"
    if not statistics.support:
        return "it declares no support"
    table = statistics.level_weights_given
    named = set(next(iter(table.values()))) if table else set()
    if set(statistics.support) != named:
        return f"its support names {sorted(statistics.support)} and its table {sorted(named)}"
    return None


def implied_marginal(
    column_plan: ColumnPlan, parent: str, history: GeneratorPlan, plan: GeneratorPlan
) -> Optional[Tuple[Dict[str, Fraction], str]]:
    """The marginal of a column drawn given *parent*, and whose level weights gave it; ``None`` for none.

    ``P(D = d) = sum over c of w_c * P(D = d | C = c)``, from the level weights ``w`` that the
    declaration of *parent* gives the levels the column's table is keyed by, and the table's rows
    taken as shares. It needs no declaration of its own and fits nothing to data. The emit's column
    is read first, and the history's where the emit's declares no level weights (``"the emit's"``
    or ``"the history's"``, the second member). Exact (:class:`~fractions.Fraction`), summed over
    the levels in sorted order and normalised, so no run order or kernel path moves a digit.
    ``None`` when the column is drawn from bins, from several parents or by no table, or neither
    declaration gives a weight to every level of the table.
    """
    statistics = column_plan.statistics
    table = statistics.level_weights_given
    if (
        table is None
        or column_plan.family is not Family.CATEGORICAL
        or is_binned(column_plan)
        or tuple(statistics.parents) != (parent,)
    ):
        return None
    for source, label in ((plan, "the emit's"), (history, "the history's")):
        weights = _level_weights_of(source.column_plan(parent), table)
        if weights is not None:
            found = _mix(weights, table)
            if found is not None:
                return found, label
    return None


def _level_weights_of(
    column_plan: ColumnPlan, table: Mapping[str, Mapping[str, Any]]
) -> Optional[Dict[str, Fraction]]:
    """The declared level weights of *column_plan* at each level *table* is keyed by, or ``None``."""
    statistics = column_plan.statistics
    if (
        column_plan.family is not Family.CATEGORICAL
        or is_binned(column_plan)
        or statistics.levels is None
        or statistics.level_weights_given is not None
    ):
        return None
    if any(level not in statistics.levels for level in table):
        return None
    return {level: Fraction(statistics.levels[level]) for level in sorted(table)}


def _mix(
    weights: Mapping[str, Fraction], table: Mapping[str, Mapping[str, Any]]
) -> Optional[Dict[str, Fraction]]:
    """``sum over c of w_c * P(D | C = c)``, normalised, or ``None`` when no weight or no row is above zero."""
    mixed: Dict[str, Fraction] = {}
    for level in sorted(weights):
        row = table[level]
        total = sum((Fraction(weight) for weight in row.values()), Fraction(0))
        if total <= 0:
            if weights[level] > 0:
                return None
            continue
        for drawn in sorted(row):
            mixed[drawn] = (
                mixed.get(drawn, Fraction(0)) + weights[level] * Fraction(row[drawn]) / total
            )
    whole = sum(mixed.values(), Fraction(0))
    if whole <= 0:
        return None
    return {drawn: share / whole for drawn, share in sorted(mixed.items())}


def fallback_notes(plan: GeneratorPlan) -> Tuple[str, ...]:
    """One sentence for each column of *plan*'s emit drawn from its own distribution where its parent holds no level.

    The sentence names the column, its parent in the version, what sets the parent's unnamed
    values and which distribution is used. Advice only: nothing here changes how a value is drawn,
    and the plan document and its digests carry the columns (``fallbackGivens``) and their
    marginals (``fallbackMarginals``) rather than the sentence.
    """
    projection = plan.projection
    if projection is None or not projection.fallback:
        return ()
    expose_id = plan.expose.expose_id
    sets = projected_cells(projection.kind, projection.entry, projection.history, plan)
    notes = []
    for column_plan, parent in _given_over_projected(sets, plan):
        if parent not in projection.fallback:
            continue
        name = column_plan.column.name
        reason = _unheld_reason(projection.history, plan, parent)
        shares = implied_marginal(column_plan, parent, projection.history, plan)
        if shares is None:
            drawn = f"{name} is drawn from {name}'s support, evenly"
        elif shares[1] == "the emit's":
            drawn = f"{name} is drawn from the marginal {parent}'s level weights and {name}'s table imply"
        else:
            drawn = (
                f"{name} is drawn from the marginal the history's level weights for {parent} and "
                f"{name}'s table imply"
            )
        notes.append(
            f"note: {expose_id}.{name} is drawn given {parent}; where {parent} holds a value no "
            f"level names ({reason}), {drawn}"
        )
    return tuple(notes)


def _unheld_givens(key: str, entry: Any, sets: Mapping[str, str], plan: GeneratorPlan) -> None:
    """Refuse (``FLX-3403``), for a plan recorded before the draw after the projection, what it refuses.

    Such a plan drew a column given a column of the version from the level the column's own draw
    made, which is what it replays. A column drawn given any other cell the entry sets is refused
    as a new plan refuses it.
    """
    own, _ = _period_cells(entry) if key == PERIODS_KEY else _event_cells(entry)
    for column_plan, parent in _given_over_projected(sets, plan):
        if parent in own:
            raise _refuse(
                f"{key} entry ({plan.expose.expose_id}): column {column_plan.column.name!r} is "
                f"drawn given {parent!r}, which the entry sets as {sets[parent]}; only a column of "
                "the version can be drawn given"
            )


def _unheld_reason(history: GeneratorPlan, plan: GeneratorPlan, name: str) -> Optional[str]:
    """Why the history's column *name* may hold a value with no level of the emit's, or ``None``.

    The history fills it with a value none of the emit's levels names when it declares a
    ``nullFraction``, when a rule, a relationship or the history's ``end`` sets it, and when it is
    drawn from levels (or bins) the emit's column does not hold.
    """
    from flux_engine.generator.relations import history_pick_of

    source = history.column_plan(name)
    statistics = source.statistics
    if statistics.null_fraction is not None:
        return "the history may leave it NULL (it declares a nullFraction)"
    setter = _rule_setters(history.rules).get(name)
    if setter is not None:
        return f"{setter} of the history sets it"
    if history.relations is not None and history.relations.driver(name) is not None:
        return "a relationship of the history sets it"
    target = plan.column_plan(name)
    if is_binned(target):
        if source.column.logical_type.kind is not target.column.logical_type.kind:
            return f"the history's {name!r} is not of the type the emit's bins are"
        return None
    if source.family is not Family.CATEGORICAL:
        return "the history does not draw it from levels"
    index = level_index(target)
    for value, level in level_index(source).items():
        if value not in index:
            return f"the history draws {level!r} in it"
    pick = history_pick_of(history)
    end = None if pick is None else pick.entry.end
    if end is not None and end.column == name and end.value not in (None, *index.values()):
        return f"the history's end writes {end.value!r} in it"
    return None


def _refuse_reads_through_projected_keys(
    key: str, sets: Mapping[str, str], plan: GeneratorPlan
) -> None:
    """Refuse (``FLX-3403``) a rule that reads through a relationship whose key the projection sets.

    A path through a relationship (a derive's ``relationship.column``, a copy, an as-of path) and a
    conditional link are read from the row's drawn pick of that relationship. When the entry sets
    the relationship's foreign-key columns the cell holds the version's value, not the pick, so
    the rule would read another row's value.
    """
    rules = plan.rules
    if rules is None:
        return
    links = rules.model.exposes[rules.expose_id].links
    reached: Dict[str, str] = {}
    for rule in rules.rules:
        for path in rule.paths:
            reached.setdefault(path[0], f"the rule {rule.id!r}")
    for step in rules.links:
        reached.setdefault(step.relationship, "a conditional link")
    for relationship, reader in reached.items():
        link = links.get(relationship)
        if link is None:
            continue
        for column in link.from_columns:
            if column in sets:
                raise _refuse(
                    f"{key} entry ({plan.expose.expose_id}): {reader} reads through the "
                    f"relationship {relationship!r}, and the entry sets its foreign-key column "
                    f"{column!r} as {sets[column]}, so the rule would read the row the relationship "
                    "drew, not the row the cell names; read the column on the row itself, or leave "
                    "the foreign key to the relationship"
                )


def refuse_rows(label: str, rows: int) -> None:
    """Refuse (``FLX-3405``, as a declared count is) a derived count above ``MOST_ROWS``."""
    if rows > MOST_ROWS:
        raise FluxError(
            FLX_3405, f"{label} derives {rows} rows; a run holds at most {MOST_ROWS} rows"
        )


def _count(key: str, chain: Any, projection: ProjectionPlan, plan: GeneratorPlan) -> int:
    rows = KINDS[key].count(chain, projection)
    refuse_rows(f"{key} ({plan.expose.expose_id})", rows)
    return rows


def derived_rows(
    key: str,
    of: str,
    options: Any,
    history: GeneratorPlan,
    plan: GeneratorPlan,
    root: SeedTreeRoot,
    after: bool = True,
) -> int:
    """The number of rows *plan*'s emit takes from *history*, worked out from the plan alone."""
    projection = replace(_projection(key, of, options, history, plan, after), root=root)
    return _count(key, chain_of(history, root), projection, plan)


def project_plan(
    plan: GeneratorPlan,
    key: str,
    of: str,
    options: Any,
    history: GeneratorPlan,
    root: SeedTreeRoot,
    after: bool = True,
) -> GeneratorPlan:
    """*plan* as a projected emit's: its ``rows`` the derived count, and the projection attached.

    *after* is :func:`_projection`'s.
    """
    projection = _projection(key, of, options, history, plan, after)
    count = _count(key, chain_of(history, root), replace(projection, root=root), plan)
    return replace(plan, rows=count, projection=projection)


# -- the rows -----------------------------------------------------------------------------------


def sources_of(plan: GeneratorPlan, root: SeedTreeRoot) -> Any:
    """The history row each row of the projected *plan* is read from, worked out once per process."""
    projection = plan.projection
    assert projection is not None  # the caller routes a projected plan only
    held = (
        root.digest,
        root.seed_tree_version,
        plan.digest().value,
    )
    found = _SOURCES.get(held)
    if found is None:
        found = KINDS[projection.kind].sources(
            chain_of(projection.history, root), replace(projection, root=root)
        )
        if len(_SOURCES) >= _SOURCES_KEPT:
            _SOURCES.clear()
        _SOURCES[held] = found
    return found


def clear_sources() -> None:
    """Drop the sources, the whole emits, the event layouts and the version views this process keeps."""
    _SOURCES.clear()
    _WHOLE.clear()
    _events.clear_layouts()
    _periods.clear_layouts()
    clear_views()


def projection_rows(
    plan: GeneratorPlan, start: int, stop: int, root: Optional[SeedTreeRoot]
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of the projected *plan*, each its source row's columns.

    The history's rows come as its own pass writes them, over the range its sources span, so a
    column holds its final value with the rules applied. An events or a periods emit builds its
    rows from its own cells and the history's, and its own rules then run over them, so a rule
    reads the cells the projection set (ADR 0027, the amendment of 2026-11-24).
    """
    projection = plan.projection
    assert projection is not None  # the caller routes a projected plan only
    if root is None:
        raise _refuse(
            f"projection of {projection.of!r}: a projected emit is generated over the run root"
        )
    if start == stop:
        return ()
    made = KINDS[projection.kind].rows
    if made is not None:
        own = replace(plan, projection=None)
        if own.rules is not None and own.rules.windowed:
            return _whole(plan, own, made, root)[start:stop]
        return with_rules(
            own, made(plan, sources_of(plan, root), start, stop, root), start, root=root
        )
    sources = sources_of(plan, root)[start:stop]
    low, high = int(sources[0]), int(sources[-1]) + 1
    rows = generate_row_slice(projection.history, low, high, root=root)
    position = {
        column.column.name: index for index, column in enumerate(projection.history.columns)
    }
    taken = [position[column.column.name] for column in plan.columns]
    return tuple(
        tuple(rows[int(source) - low][index] for index in taken) for source in sources.tolist()
    )


def _whole(
    plan: GeneratorPlan,
    own: GeneratorPlan,
    made: Callable[[GeneratorPlan, Any, int, int, SeedTreeRoot], Tuple[Tuple[Any, ...], ...]],
    root: SeedTreeRoot,
) -> Tuple[Tuple[Any, ...], ...]:
    """Every row of a projected emit whose rules hold a window over siblings, kept per process.

    The window reads rows of every slice, so the emit is made whole and its rules applied over
    all of it, as :func:`~flux_engine.generator.rows.generate_row_slice` makes any such emit.
    """
    held = (root.digest, root.seed_tree_version, plan.digest().value)
    if held not in _WHOLE:
        if len(_WHOLE) >= _WHOLE_KEPT:
            _WHOLE.clear()
        sources = sources_of(plan, root)
        _WHOLE[held] = with_rules(own, made(plan, sources, 0, plan.rows, root), 0, root=root)
    return _WHOLE[held]
