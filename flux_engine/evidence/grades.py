# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The privacy grade of a run, read from the profile it pins (ADR 0020).

A run has one grade, and it comes from the pinned profile and from the generator plugins the bundle
declares (ADR 0032), and from nothing else. The bundle's own labels do not enter it: the parameter
register says where a bundle claims its numbers come from, and a bundle can claim anything, so a
grade read from the claims is a grade the bundle chooses. The profile is what the Simulation's digest
pins, so it cannot be relabelled. A plugin's grade is a statement the bundle makes about code it names
and pins, and it is the weaker pattern that counts: the run's grade is the weakest of the profile's
and every plugin's.

* **No profile pinned: P0.** Every parameter is declared by hand and the engine read no data. It
  cannot confirm that no data shaped the numbers a person typed.
* **A profile pinned: P3** only when the profile states an account of a release under differential
  privacy, declares that its levels, bin edges and ``sampleSize`` bands were chosen without the data
  (``publicStructure``), and **every** statistic the engine can read from it is noisy under that
  account: no row count, no column metric, no statistic without a ``noise`` block, no period, query
  or fact statistic, none of which can be released with noise in this format, and no column pair
  with a statistic that has none. A pair is noisy when every statistic in it carries a ``noise``
  block: a ``correlation/1`` never does, and a rank correlation can. A column statistic grouped by
  bins (``groupByBins``) or by a link (``groupByLink``) is noisy only when it carries a ``noise``
  block. A relationship is noisy when every statistic in it carries one (so it holds no ``mean/1``
  and no quantile) and it holds no ``dates`` and no ``exposure``; a relationship that holds no
  statistic, named only so that the unit can follow it, releases nothing. The unit's ``follows`` is
  part of the account: the evidence states the unit with what it follows and names the tables it
  treats as fixed. The engine checked the account's arithmetic and the cliques' agreement. It did
  not check that noise was applied, nor the declaration about the structure: both are the
  profiler's statement.

  The parts of the account are the ones the profile reader enumerates
  (:attr:`flux_stats.profile.Profile.parts`), once each, so no statistic can be missing from the
  spent figure for want of a place in a list kept here. A table's ``cliqueSelection`` is
  accounting, not a statistic: its ``selection`` and ``domain`` shares are parts, it never makes a
  profile not P3, and a clique of either definition carries its own ``noise``. A member the grade
  does not know, at the top level, in the ``privacy`` block, in a table, a column or a relationship,
  counts as released without noise.
* **Otherwise P1.** The profile's statistics were released under output checks and carry no formal
  guarantee, and the reasons are listed.
* **A generator plugin: P4 or P5.** ``P4`` is a differentially private model, with the account the
  bundle declares; ``P5`` is a learned model with no such account, the weakest pattern. A run that
  pins no profile and declares one ``P4`` plugin is ``P4``.

A column's own grade, which would follow the statistics that inform it, is a later refinement. Today
each column's minimum (:mod:`flux_engine.governance.privacy`) is compared with the run's grade.

Nothing here reads a record or a row: it reads the profile's document.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from flux_engine.core.grades import ORDER, Pattern, at_least, weakest
from flux_engine.evidence.provenance import quoted_name
from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_4_0, SCHEMA_URL_1_5_0, Profile, parse_profile

__all__ = [
    "Accounting",
    "ColumnFloor",
    "GeneratorGrade",
    "ORDER",
    "Pattern",
    "ProfileGrade",
    "RunGrade",
    "accounting_of",
    "at_least",
    "grade_profile",
    "weakest",
]

#: The sections of a profile whose statistics cannot carry a ``noise`` block.
_NOT_NOISY_SECTIONS = ("invariants", "references")

#: What the declaration about structure covers (``publicStructure``), for the reason a profile that
#: does not make it is not ``P3``. A profile that names format 1-4-0 declares more.
_STRUCTURE = "its levels, bin edges and sampleSize bands"
_STRUCTURE_1_4 = (
    "its levels and bin edges, the bins of every groupByBins, the group keys of grouped noisy "
    "statistics, its sampleSize bands, every rowFloor and every maxChildren"
)

#: The top-level members of a profile the grade reads, each by its own rule in :func:`grade_profile`.
GRADED_TOP_LEVEL_MEMBERS = frozenset(
    {"tables", "relationships", "queries", "invariants", "references", "privacy"}
)
#: The top-level members that carry no statistic: the producer, the format, the grid and the rules.
STRUCTURAL_TOP_LEVEL_MEMBERS = frozenset({"_producer", "_schemaURL", "encoding", "disclosureRules"})
#: The members of the ``privacy`` block, none of which carries a statistic.
STRUCTURAL_PRIVACY_MEMBERS = frozenset({"measure", "privacyLoss", "privacyUnit", "publicStructure"})

#: The calendar histograms of a table's ``calendar`` entry (format 1-3-0).
_CALENDAR_KEYS = ("month", "dayOfWeek", "hourOfWeek")

#: The members of a table the grade reads, each by its own rule in :func:`grade_profile`.
GRADED_TABLE_MEMBERS = frozenset(
    {
        "rowCount",
        "columns",
        "columnPairs",
        "periods",
        "cliques",
        "cliqueSelection",
        "datePairs",
        "calendar",
        "history",
    }
)
#: The members of a column the grade reads.
GRADED_COLUMN_MEMBERS = frozenset({"metrics", "statistics"})
#: The members of a relationship the grade reads. The relationship section as a whole is exact.
GRADED_RELATIONSHIP_MEMBERS = frozenset({"statistics", "dates", "exposure"})
#: Members that carry no statistic: a column's domain, and a relationship's keys and structure.
STRUCTURAL_COLUMN_MEMBERS = frozenset({"domain"})
STRUCTURAL_RELATIONSHIP_MEMBERS = frozenset({"parent", "child", "structural"})
#: ``structural`` (the classes of parents a constraint leaves empty) is structure only in format
#: 1-4-0 and later; in an older format the member is not one the grade knows.
_SINCE_1_4_RELATIONSHIP_MEMBERS = frozenset({"structural"})


def _structural_relationship_members(accounting: Optional["Accounting"]) -> FrozenSet[str]:
    """The relationship members that carry no statistic in the format *accounting* belongs to."""
    if accounting is not None and accounting.from_1_4:
        return STRUCTURAL_RELATIONSHIP_MEMBERS
    return STRUCTURAL_RELATIONSHIP_MEMBERS - _SINCE_1_4_RELATIONSHIP_MEMBERS


@dataclass(frozen=True)
class Accounting:
    """The release-level account of the pinned profile, in NIST SP 800-226's terms."""

    measure: str
    unit: str
    total: Mapping[str, str]
    #: The parts: how many noisy statistics and cliques spend the budget, and what they add up to.
    parts: int
    spent: Mapping[str, str]
    #: The profiler's declaration that levels, bin edges and ``sampleSize`` bands were chosen
    #: without the data. The engine cannot check it.
    public_structure: bool = False
    #: The profile names format 1-4-0. Its declaration covers more (the group keys of grouped
    #: noisy statistics, every ``rowFloor`` and every ``maxChildren``), and a labelled column of a
    #: table its unit does not reach counts as ``P1`` for the minimum grade.
    from_1_4: bool = False
    #: The profile names format 1-5-0, which may hold a noisy history and a noisy query.
    from_1_5: bool = False
    #: A table's ``history`` holds a member that carries noise (format 1-5-0).
    noisy_history: bool = False
    #: A query's statistic carries noise (format 1-5-0).
    noisy_query: bool = False
    #: The relationships the unit follows, as ``(parent table, child table, maxChildren)``.
    follows: Tuple[Tuple[str, str, int], ...] = ()
    #: ``m_T`` of every table the unit reaches, the expose included, by table name. Empty when the
    #: reader refuses the profile.
    reached: Tuple[Tuple[str, int], ...] = ()
    #: The profile's tables the unit does not reach, whose rows the guarantee treats as fixed.
    fixed: Tuple[str, ...] = ()

    def unreached(self, exposes: Iterable[str]) -> FrozenSet[str]:
        """The *exposes* the unit does not reach, for a profile of format 1-4-0; none otherwise.

        A table that is not in the profile at all is not reached either. The minimum-grade check
        counts a labelled column of such a table as ``P1`` (ADR 0037 decision 1). A profile of an
        earlier format keeps the check it always had.
        """
        if not self.from_1_4:
            return frozenset()
        held = {table for table, _ in self.reached}
        return frozenset(name for name in exposes if name not in held)

    @property
    def definition(self) -> str:
        return (
            "zero-concentrated differential privacy"
            if self.measure == "zeroConcentratedDP"
            else "approximate differential privacy"
        )

    @property
    def composition(self) -> str:
        return (
            "the privacy loss rho of the parts adds"
            if self.measure == "zeroConcentratedDP"
            else "the privacy loss epsilon of the parts adds, and so does delta (basic composition)"
        )


def _named(name: Any) -> str:
    """A name from the profile as a reason prints it: quoted, or withheld when printing it would harm the report."""
    return quoted_name(str(name), "(a name withheld)")


@dataclass(frozen=True)
class ProfileGrade:
    """The grade of a run's pinned profile, with the reasons it is not P3 when it is not."""

    pattern: Pattern
    #: Fixed phrases, never names from the profile, saying why the profile is not P3.
    reasons: Tuple[str, ...] = ()
    accounting: Optional[Accounting] = None
    pinned: bool = False


@dataclass(frozen=True)
class ColumnFloor:
    """One column of the run, the minimum its sensitivity sets, and whether the run's grade meets it."""

    expose_id: str
    column: str
    sensitivity: Optional[str]
    minimum: Optional[Pattern]
    met: bool


@dataclass(frozen=True)
class GeneratorGrade:
    """One generator plugin a bundle declares, with the grade it states (ADR 0032).

    ``unit``, ``loss`` and ``accounting`` are the account of a ``P4`` plugin as the bundle declares
    it, and ``None`` for ``P5``. The engine checks the account's form, not how the model was trained.
    """

    expose_id: str
    plugin: str
    distribution: str
    version: str
    pattern: Pattern
    unit: Optional[str] = None
    loss: Optional[str] = None
    accounting: Optional[str] = None


@dataclass(frozen=True)
class RunGrade:
    """The run's grade, and each column's minimum beside it.

    The run's grade is the weakest of its profile's and every declared plugin's.
    """

    profile: ProfileGrade
    columns: Tuple[ColumnFloor, ...] = ()
    generators: Tuple[GeneratorGrade, ...] = ()

    @property
    def pattern(self) -> Pattern:
        return weakest([self.profile.pattern, *(item.pattern for item in self.generators)])


def _decimal(value: Fraction) -> str:
    denominator = value.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    if denominator != 1:
        return f"{value.numerator}/{value.denominator}"
    from decimal import Decimal

    text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _read(profile: Mapping[str, Any]) -> Optional[Profile]:
    """The profile as its reader reads it, or ``None`` when the reader refuses it."""
    try:
        return parse_profile(profile, allow_emptied_entries=True)
    except FluxError:
        return None


def _account(profile: Optional[Mapping[str, Any]]) -> Tuple[Optional[Accounting], bool]:
    """The account a profile states and whether the reader could read the profile to count its parts."""
    block = profile.get("privacy") if isinstance(profile, Mapping) else None
    if not isinstance(block, Mapping):
        return None, True
    assert profile is not None
    total = dict(block.get("privacyLoss") or {})
    read = _read(profile)
    parts = read.parts if read is not None else None
    spent = _spent(total, parts or ())
    unit = block.get("privacyUnit")
    follows, reach, fixed = _unit_reach(read)
    accounting = Accounting(
        measure=str(block.get("measure")),
        unit=str(unit.get("expose")) if isinstance(unit, Mapping) else "",
        total={key: str(value) for key, value in total.items()},
        parts=len(parts or ()),
        spent={key: _decimal(value) for key, value in spent.items()},
        public_structure=block.get("publicStructure") is True,
        from_1_4=profile.get("_schemaURL") in (SCHEMA_URL_1_4_0, SCHEMA_URL_1_5_0),
        from_1_5=profile.get("_schemaURL") == SCHEMA_URL_1_5_0,
        noisy_history=_holds_noisy_history(read),
        noisy_query=_holds_noisy_query(read),
        follows=follows,
        reached=reach,
        fixed=fixed,
    )
    return accounting, parts is not None


def _holds_noisy_history(read: Optional[Profile]) -> bool:
    """Whether a table of the profile has a ``history`` member that carries noise."""
    if read is None:
        return False
    for table in read.tables.values():
        history = table.history
        if history is None:
            continue
        members = [
            history.versions_per_entity,
            history.first_start,
            history.version_length,
            history.chain,
            history.last_version,
            history.reasons,
        ]
        for column in history.columns.values():
            members += [column.initial, column.transitions]
        if any(member is not None and member.noise is not None for member in members):
            return True
    return False


def _holds_noisy_query(read: Optional[Profile]) -> bool:
    """Whether a query of the profile has a statistic that carries noise."""
    return read is not None and any(
        statistic.noise is not None for query in read.queries for statistic in query.statistics
    )


def _spent(total: Mapping[str, Any], parts: Iterable[Any]) -> Dict[str, Fraction]:
    """What the parts add up to, for each share of the stated total."""
    spent: Dict[str, Fraction] = {key: Fraction(0) for key in total}
    for _, noise in parts:
        for key in spent:
            share = getattr(noise.loss, key, None)
            if share is not None:
                spent[key] += share
    return spent


def _unit_reach(
    read: Optional[Profile],
) -> Tuple[Tuple[Tuple[str, str, int], ...], Tuple[Tuple[str, int], ...], Tuple[str, ...]]:
    """The relationships the unit follows, the tables it reaches, and the tables it treats as fixed."""
    if read is None:
        return (), (), ()
    stated = read.accounting
    reach: Tuple[Tuple[str, int], ...] = ()
    follows: Tuple[Tuple[str, str, int], ...] = ()
    if stated is not None:
        reach = tuple(sorted({stated.unit: 1, **stated.reached}.items()))
        follows = tuple((f.parent_table, f.child_table, f.max_children) for f in stated.follows)
    fixed = tuple(sorted(name for name in read.tables if name not in dict(reach)))
    return follows, reach, fixed


def accounting_of(profile: Optional[Mapping[str, Any]]) -> Optional[Accounting]:
    """The account a profile states, with the parts added up, or ``None`` for a profile with none.

    The parts are the reader's own (:attr:`flux_stats.profile.Profile.parts`): one enumeration of
    the noisy statistics and cliques, shared with the check that the shares fit the total. A profile
    the reader refuses has no parts to count, so its account lists none.
    """
    return _account(profile)[0]


def _pair_name(pair: Any, index: int) -> str:
    """How a reason names a column pair: by its two columns, or by its place when it names none."""
    columns = pair.get("columns") if isinstance(pair, Mapping) else None
    if isinstance(columns, list) and len(columns) == 2 and all(isinstance(c, str) for c in columns):
        return f"{_named(columns[0])}, {_named(columns[1])}"
    return str(index)


def _pair_is_noisy(pair: Any) -> bool:
    """A ``columnPairs`` entry is noisy when it holds a statistic and every one carries ``noise``.

    A ``correlation/1`` never does, so a pair that holds one is not noisy, and an entry with no
    statistic releases nothing the grade can show to be noisy.
    """
    statistics = pair.get("statistics") if isinstance(pair, Mapping) else None
    return (
        isinstance(statistics, list)
        and bool(statistics)
        and all(isinstance(s, Mapping) and "noise" in s for s in statistics)
    )


#: Fixed phrases for the cases where a noisy statistic still states a data value or an exact size.
_DATA_GROUP_REASON = (
    "the profile holds a statistic grouped by a column whose keys are data values (a number or "
    "timestamp column, or one the profile does not describe), which are released without noise"
)
_LINK_GROUP_REASON = (
    "the profile holds a statistic grouped through a link to a column of another table, whose "
    "keys are values of that table's data and are released without noise"
)
_LINK_COLUMN_REASON = (
    "the profile holds a statistic grouped through a link to a column that is not a category "
    "column of a table it describes, whose keys are data values released without noise"
)
_EXACT_BAND_REASON = (
    "the profile holds a statistic with a sampleSize band whose min equals its max, an exact "
    "size released without noise"
)


def _structure_reasons(
    statistics: Any,
    columns: Mapping[str, Any],
    found: Set[str],
    *,
    links: bool,
    tables: Any = None,
) -> None:
    """Add to *found* the reasons a noisy statistic of a table still states data (ADR 0020).

    A group key of a ``groupBy`` on a column that is not categorical is a data value, and so is the
    key of a ``groupByLink``. A ``sampleSize`` band with ``min`` equal to ``max`` is an exact size.
    Under ``publicStructure`` a categorical level and a band are declared chosen without the data;
    these are not, whatever the declaration says. Anything the check cannot read counts as data.

    *links* is false for a profile of format ``1-4-0``, whose declaration covers the values of a
    category column of the parent table of a ``groupByLink`` as group keys of a noisy statistic; a
    link to any other column (a number or timestamp column, or one the profile does not describe,
    looked up in *tables*, the profile's tables) is data in every format, as are a non-categorical
    ``groupBy`` and an exact band.
    """
    for statistic in statistics if isinstance(statistics, (list, tuple)) else ():
        if isinstance(statistic, Mapping):
            found.update(_statistic_reasons(statistic, columns, links=links, tables=tables))


def _link_column_is_category(link: Any, tables: Any) -> bool:
    """Whether a ``groupByLink`` names a category column of a table the profile describes."""
    parent = link.get("parent") if isinstance(link, Mapping) else None
    table = (
        tables.get(parent.get("table"))
        if isinstance(tables, Mapping)
        and isinstance(parent, Mapping)
        and isinstance(parent.get("table"), str)
        else None
    )
    described = table.get("columns") if isinstance(table, Mapping) else None
    name = link.get("column") if isinstance(link, Mapping) else None
    target = (
        described.get(name) if isinstance(described, Mapping) and isinstance(name, str) else None
    )
    return isinstance(target, Mapping) and target.get("domain") == "category"


def _statistic_reasons(
    statistic: Mapping[str, Any], columns: Mapping[str, Any], *, links: bool, tables: Any = None
) -> Set[str]:
    """The reasons one noisy statistic still states data (see :func:`_structure_reasons`)."""
    found: Set[str] = set()
    if "groupByLink" in statistic:
        if links:
            found.add(_LINK_GROUP_REASON)
        elif not _link_column_is_category(statistic["groupByLink"], tables):
            found.add(_LINK_COLUMN_REASON)
    if "groupBy" in statistic:
        target = (
            columns.get(statistic["groupBy"]) if isinstance(statistic["groupBy"], str) else None
        )
        if not (isinstance(target, Mapping) and target.get("domain") == "category"):
            found.add(_DATA_GROUP_REASON)
    band = statistic.get("sampleSize")
    if isinstance(band, Mapping) and "min" in band and band.get("min") == band.get("max"):
        found.add(_EXACT_BAND_REASON)
    return found


def _table_structure_reasons(
    table: Mapping[str, Any], found: Set[str], *, links: bool, tables: Any = None
) -> None:
    """Add to *found* the reasons the statistics of a table's columns, date pairs and column pairs
    state data."""
    columns = table.get("columns")
    table_columns = columns if isinstance(columns, Mapping) else {}
    for key in ("datePairs", "columnPairs"):
        for pair in table.get(key) or ():
            if isinstance(pair, Mapping):
                _structure_reasons(
                    pair.get("statistics"), table_columns, found, links=links, tables=tables
                )
    for column in (table.get("columns") or {}).values():
        if isinstance(column, Mapping):
            _structure_reasons(
                column.get("statistics"), table_columns, found, links=links, tables=tables
            )
    history = table.get("history")
    if isinstance(history, Mapping):
        _structure_reasons(
            _history_members(history), table_columns, found, links=links, tables=tables
        )


def _query_structure_reasons(queries: Any, tables: Any, found: Set[str], *, links: bool) -> None:
    """Add to *found* the reasons the statistics of the ``queries`` state data (format 1-5-0).

    A query's ``groupBy`` names a column of its unit's table. A query that states no unit, or one
    whose table the profile does not hold, has no column to look it up in.
    """
    for query in queries if isinstance(queries, list) else ():
        if not isinstance(query, Mapping):
            continue
        unit = query.get("unit")
        table = (
            tables.get(unit.get("table"))
            if isinstance(tables, Mapping)
            and isinstance(unit, Mapping)
            and isinstance(unit.get("table"), str)
            else None
        )
        columns = table.get("columns") if isinstance(table, Mapping) else None
        _structure_reasons(
            query.get("statistics"),
            columns if isinstance(columns, Mapping) else {},
            found,
            links=links,
            tables=tables,
        )


def _history_members(history: Mapping[str, Any]) -> List[Any]:
    """The members of a table's ``history``: its statistics, and each tracked column's two.

    A tracked column that is not an object counts as a member that is absent.
    """
    members: List[Any] = [
        history.get(key)
        for key in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion")
    ]
    if "reasons" in history:
        members.append(history["reasons"])
    columns = history.get("columns")
    for column in columns.values() if isinstance(columns, Mapping) else ():
        if isinstance(column, Mapping):
            members += [column.get("initial"), column.get("transitions")]
        else:
            members.append(None)
    return members


def _history_released_exact(history: Any) -> bool:
    """Whether a table's ``history`` holds a member released without noise (format 1-5-0 allows it).

    A history with no member that carries a ``noise`` block is exact, as is one that has any member
    without one; an empty or absent history holds nothing. A member the grade does not recognise
    counts as released without noise.
    """
    if not history:
        return False
    if not isinstance(history, Mapping):
        return True
    members = _history_members(history)
    return any(not (isinstance(member, Mapping) and "noise" in member) for member in members)


def _queries_released_exact(queries: Any) -> bool:
    """Whether the ``queries`` section holds a statistic released without noise (format 1-5-0 allows it)."""
    if not queries:
        return False
    if not isinstance(queries, list):
        return True
    return any(
        not isinstance(query, Mapping)
        or not query.get("statistics")
        or any(
            not (isinstance(statistic, Mapping) and "noise" in statistic)
            for statistic in query["statistics"]
        )
        for query in queries
    )


def grade_profile(profile: Optional[Mapping[str, Any]], *, pinned: bool) -> ProfileGrade:
    """Grade a run from the document of the profile it pins (``None`` when it pins none).

    *pinned* says the Simulation pins a profile. A run that pins one and whose document is not at
    hand cannot be shown to be anything better than ``P1``.
    """
    if not pinned:
        return ProfileGrade(Pattern.P0)
    if profile is None:
        return ProfileGrade(
            Pattern.P1,
            ("the pinned profile was not at hand, so nothing shows its statistics to be noisy",),
            None,
            True,
        )
    accounting, readable = _account(profile)
    reasons: List[str] = []
    if accounting is None:
        reasons.append("the profile states no account of a release under differential privacy")
    elif not accounting.public_structure:
        reasons.append(
            "the profile does not declare that "
            + (_STRUCTURE_1_4 if accounting.from_1_4 else _STRUCTURE)
            + " were chosen without the data"
        )
    exact = {
        "row count": False,
        "metric": False,
        "statistic": False,
        "other": False,
        "history": False,
    }
    sections: List[str] = []
    unknown: List[str] = [
        _named(key)
        for key in profile
        if key not in GRADED_TOP_LEVEL_MEMBERS | STRUCTURAL_TOP_LEVEL_MEMBERS
    ]
    privacy = profile.get("privacy")
    unknown += [
        f"privacy.{_named(key)}"
        for key in (privacy if isinstance(privacy, Mapping) else ())
        if key not in STRUCTURAL_PRIVACY_MEMBERS
    ]
    data_stated: Set[str] = set()
    tables = profile.get("tables")
    for table_id, table in tables.items() if isinstance(tables, Mapping) else ():
        if not isinstance(table, Mapping):
            continue
        at = f"tables.{_named(table_id)}"
        unknown += [f"{at}.{_named(key)}" for key in table if key not in GRADED_TABLE_MEMBERS]
        exact["row count"] |= "rowCount" in table
        exact["other"] |= bool(table.get("periods"))
        sections += [
            f"{at}.columnPairs[{_pair_name(pair, index)}]"
            for index, pair in enumerate(table.get("columnPairs") or ())
            if not _pair_is_noisy(pair)
        ]
        exact["history"] |= _history_released_exact(table.get("history"))
        if any(
            isinstance(pair, Mapping)
            and (
                any(
                    isinstance(s, Mapping) and "noise" not in s
                    for s in [*(pair.get("statistics") or ()), pair.get("order")]
                )
                or not isinstance(pair.get("order"), Mapping)
            )
            for pair in table.get("datePairs") or ()
        ):
            sections.append(f"{at}.datePairs")
        if any(
            isinstance(entry, Mapping)
            and any(
                key in entry and not (isinstance(entry[key], Mapping) and "noise" in entry[key])
                for key in _CALENDAR_KEYS
            )
            for entry in table.get("calendar") or ()
        ):
            sections.append(f"{at}.calendar")
        _table_structure_reasons(
            table,
            data_stated,
            links=accounting is None or not accounting.from_1_4,
            tables=tables,
        )
        for name, column in (table.get("columns") or {}).items():
            if not isinstance(column, Mapping):
                continue
            unknown += [
                f"{at}.columns.{_named(name)}.{_named(key)}"
                for key in column
                if key not in GRADED_COLUMN_MEMBERS | STRUCTURAL_COLUMN_MEMBERS
            ]
            exact["metric"] |= bool(column.get("metrics"))
            exact["statistic"] |= any(
                isinstance(s, Mapping) and "noise" not in s for s in column.get("statistics") or ()
            )
    exact["other"] |= any(profile.get(section) for section in _NOT_NOISY_SECTIONS)
    exact["other"] |= _queries_released_exact(profile.get("queries"))
    _query_structure_reasons(
        profile.get("queries"),
        tables,
        data_stated,
        links=accounting is None or not accounting.from_1_4,
    )
    relationships = profile.get("relationships")
    for index, relationship in enumerate(relationships if isinstance(relationships, list) else ()):
        if not isinstance(relationship, Mapping):
            continue
        at = f"relationships[{index}]"
        sections += [f"{at}.{key}" for key in ("dates", "exposure") if relationship.get(key)]
        if any(
            not (isinstance(s, Mapping) and "noise" in s)
            for s in relationship.get("statistics") or ()
        ):
            sections.append(at)
        parent = relationship.get("parent")
        parent_table = (
            tables.get(parent.get("table"))
            if isinstance(tables, Mapping)
            and isinstance(parent, Mapping)
            and isinstance(parent.get("table"), str)
            else None
        )
        parent_columns = parent_table.get("columns") if isinstance(parent_table, Mapping) else None
        _structure_reasons(
            relationship.get("statistics"),
            parent_columns if isinstance(parent_columns, Mapping) else {},
            data_stated,
            links=accounting is None or not accounting.from_1_4,
            tables=tables,
        )
        unknown += [
            f"{at}.{_named(key)}"
            for key in relationship
            if key not in GRADED_RELATIONSHIP_MEMBERS | _structural_relationship_members(accounting)
        ]
    if exact["row count"]:
        reasons.append("the profile holds a row count released without noise")
    if exact["metric"]:
        reasons.append(
            "the profile holds column metrics (counts, sums, minima, maxima, quantiles) released without noise"
        )
    if exact["statistic"]:
        reasons.append("the profile holds a statistic released without noise")
    if exact["history"]:
        reasons.append(
            "the profile holds a history of versions with a member released without noise"
        )
    if exact["other"]:
        reasons.append(
            "the profile holds period statistics, fact statistics or reference rows, which cannot "
            "be released with noise, or a query statistic released without noise"
        )
    reasons.extend(sorted(data_stated))
    for path in sections:
        reasons.append(f"the profile holds a statistic released without noise in {path}")
    for path in unknown:
        reasons.append(
            f"the profile holds {path}, which the grade does not know, so it counts as released "
            "without noise"
        )
    if accounting is not None and not readable:
        reasons.append(
            "the profile reader refuses the profile, so the parts of its account cannot be counted"
        )
    elif accounting is not None and accounting.parts == 0:
        reasons.append("the profile's account is spent by no noisy statistic or clique")
    return ProfileGrade(Pattern.P3 if not reasons else Pattern.P1, tuple(reasons), accounting, True)
