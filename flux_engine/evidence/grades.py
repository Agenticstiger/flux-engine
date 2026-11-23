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
  account: no row count, no column metric, no statistic without a ``noise`` block, no period,
  relationship, query or fact statistic, none of which can be released with noise in this format,
  and no column pair with a statistic that has none. A pair is noisy when every statistic in it
  carries a ``noise`` block: a ``correlation/1`` never does, and a rank correlation can. A column
  statistic grouped by bins (``groupByBins``) is noisy only when it carries a ``noise`` block. The
  engine checked the account's arithmetic and the cliques' agreement. It did not check that noise
  was applied, nor the declaration about the structure: both are the profiler's statement.

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
from typing import Any, Dict, List, Mapping, Optional, Tuple

from flux_engine.core.grades import ORDER, Pattern, at_least, weakest
from flux_engine.evidence.provenance import quoted_name
from flux_stats.errors import FluxError
from flux_stats.noise import Spent
from flux_stats.profile import parse_profile

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
_NOT_NOISY_SECTIONS = ("relationships", "queries", "invariants", "references")

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


def _read_parts(profile: Mapping[str, Any]) -> Optional[Tuple[Tuple[str, Spent], ...]]:
    """The noisy parts of a profile, as its reader enumerates them, or ``None`` when it cannot read it."""
    try:
        return parse_profile(profile).parts
    except FluxError:
        return None


def _account(profile: Optional[Mapping[str, Any]]) -> Tuple[Optional[Accounting], bool]:
    """The account a profile states and whether the reader could read the profile to count its parts."""
    block = profile.get("privacy") if isinstance(profile, Mapping) else None
    if not isinstance(block, Mapping):
        return None, True
    assert profile is not None
    total = dict(block.get("privacyLoss") or {})
    spent: Dict[str, Fraction] = {key: Fraction(0) for key in total}
    parts = _read_parts(profile)
    for _, noise in parts or ():
        for key in spent:
            share = getattr(noise.loss, key, None)
            if share is not None:
                spent[key] += share
    unit = block.get("privacyUnit")
    accounting = Accounting(
        measure=str(block.get("measure")),
        unit=str(unit.get("expose")) if isinstance(unit, Mapping) else "",
        total={key: str(value) for key, value in total.items()},
        parts=len(parts or ()),
        spent={key: _decimal(value) for key, value in spent.items()},
        public_structure=block.get("publicStructure") is True,
    )
    return accounting, parts is not None


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
            "the profile does not declare that its levels, bin edges and sampleSize bands were "
            "chosen without the data"
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
        exact["history"] |= bool(table.get("history"))
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
    relationships = profile.get("relationships")
    for index, relationship in enumerate(relationships if isinstance(relationships, list) else ()):
        if not isinstance(relationship, Mapping):
            continue
        at = f"relationships[{index}]"
        sections += [f"{at}.{key}" for key in ("dates", "exposure") if relationship.get(key)]
        unknown += [
            f"{at}.{_named(key)}"
            for key in relationship
            if key not in GRADED_RELATIONSHIP_MEMBERS | STRUCTURAL_RELATIONSHIP_MEMBERS
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
            "the profile holds a history of versions, whose statistics are released without noise "
            "because one row can move an entity between cells"
        )
    if exact["other"]:
        reasons.append(
            "the profile holds period, relationship, query or fact statistics, or reference "
            "rows, which cannot be released with noise in this format"
        )
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
