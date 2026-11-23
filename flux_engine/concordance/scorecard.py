# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The scorecard: a run scored against the profile its Simulation pins.

docs/spec/SCORECARD-SPEC.md. ``verify`` and ``ship`` form
it from the profile path's evaluations (:class:`~flux_engine.concordance.verifier.ClearProfile`),
one per emit, and ``ship`` writes it into the evidence pack as
``run/scorecard.json``. It is a tree of exact scores:

* a **property** scores the mean similarity of its scored groups
  (:mod:`flux_engine.concordance.similarity`), beside its outcome, which is
  still the gate: ``within``, ``outside``, ``censored`` (the profile withheld
  every group, so nothing was scored) or ``refused`` (a fail-closed condition
  was met, so the check declined to answer);
* a **table** and a **dimension** score the weighted mean of their scored
  properties;
* the run scores the mean of its measured dimensions, each weighted equally,
  so that many marginal properties do not drown a few joint ones.

The tree is SDMetrics' report shape (sdv-dev/SDMetrics 0.33.0, MIT; used as
a pattern only, since it depends on the BSL-licensed ``copulas``): a score
per item, an unweighted mean per property group, and an unweighted mean of
those for the overall score, with an item that could not be scored shown and
left out of every mean. mostlyai-qa (1.10.11, Apache-2.0) averages its
univariate and bivariate accuracies into one overall the same way. Here the
weights are written out as exact fractions, every weight is 1 today, and a
score that was never formed is ``None``, never a default number.

The score does not say how much of the contract it speaks for, so the record
also carries **coverage**: how many of the contract's columns carry a scored
property, and how many of its declared relationships the profile constrains
(:class:`Coverage`). Coverage is counted, never scored; a relationship's children per
parent are scored apart, as a ``childrenPerParent`` row beside it.

A dimension, a table or the run with nothing scored is ``not measured``. A
threshold on it is not met, and nothing reports a number in its place.

Nothing here reads a record, and no value of the data appears in the
document: a score is a function of the profile's published statistics and
the synthetic rows, and groups are named by ordinal, never by key
(KERNEL-SPEC §9).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty, render_exact
from flux_engine.concordance.queries import QueriesDimension
from flux_engine.concordance.verifier import (
    CENSORED,
    OUTSIDE,
    WITHIN,
    FidelityEvaluation,
    PropertyOutcome,
    Refusal,
)
from flux_engine.core.canonical import nfc
from flux_engine.lineage.emitter import OPENLINEAGE_PRODUCER
from flux_engine.seam.scorecard import (
    DIMENSIONS,
    ScorecardDeclaration,
    Threshold,
    dimension_of,
)
from flux_stats import definition_for_property
from flux_stats.profile import Profile

if TYPE_CHECKING:
    from flux_engine.concordance.shape import ShapeReport

__all__ = [
    "ASSERTIONS_SCHEMA_URL",
    "REFUSED",
    "SCHEMA_URL",
    "SCHEMA_URL_1_3_0",
    "SCHEMA_URL_1_4_0",
    "SCHEMA_URL_1_5_0",
    "SCORECARD_FILE",
    "Aggregate",
    "ContractShape",
    "Coverage",
    "CredibilityResult",
    "DeclaredLink",
    "PropertyScore",
    "RunCoverage",
    "Scorecard",
    "Share",
    "TableInput",
    "TableScore",
    "ThresholdResult",
    "build_scorecard",
    "display",
]

#: The scorecard format this engine writes, named by its schema's ``$id``.
SCHEMA_URL = "https://agenticstiger.github.io/flux/engine/spec/scorecard/1-2-0/FluxScorecard.json"

#: Format ``1-3-0``, written only by a scorecard that holds a fact or a relationship row
#: (SCORECARD-SPEC.md §3): every other scorecard is format ``1-2-0`` and keeps its bytes.
SCHEMA_URL_1_3_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/scorecard/1-3-0/FluxScorecard.json"
)

#: Format ``1-4-0``, written only by a scorecard that holds a children-per-parent row, a calendar
#: row, the ``sampling`` member of a property row's widening, the ``queries`` member of the
#: ``queries`` dimension, or the top-level ``overlay`` member (SCORECARD-SPEC.md §1, §3, §3a and
#: §9a): every other scorecard keeps the format it wrote before.
SCHEMA_URL_1_4_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/scorecard/1-4-0/FluxScorecard.json"
)

#: Format ``1-5-0``, written only by a scorecard that holds a ``rankCorrelation`` row, a row
#: grouped by the bins of a column, or a children-per-parent row grouped by a parent column
#: (SCORECARD-SPEC.md §3, docs/spec/drafts/correlations-0.11.0.md section 5): every other scorecard
#: keeps the format it wrote before.
SCHEMA_URL_1_5_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/scorecard/1-5-0/FluxScorecard.json"
)

#: The ``property`` of the rows that make a scorecard format ``1-4-0``.
_FORMAT_1_4_0_ROWS = frozenset({"childrenPerParent", "calendar"})

#: The dimension the consumers' queries score (ADR 0028 section 7). A scorecard whose ``queries``
#: dimension holds the ``queries`` member, which only a bundle that declares ``consumers`` has,
#: names format ``1-4-0`` too.
_QUERIES = "queries"

#: OpenLineage's data-quality assertions facet, 1-1-0: one entry per scored property.
ASSERTIONS_SCHEMA_URL = (
    "https://openlineage.io/spec/facets/1-1-0/DataQualityAssertionsDatasetFacet.json"
    "#/$defs/DataQualityAssertionsDatasetFacet"
)

#: Where ``ship`` writes the scorecard, beside the run's other records.
SCORECARD_FILE = "scorecard.json"

#: A property the check declined to answer for (a fail-closed condition).
REFUSED = "refused"

MEASURED = "measured"
NOT_MEASURED = "not measured"

_ONE = Fraction(1)

#: What the shape slot says of a scorecard formed with no shape report (format
#: 1-1-0 adds the shape flags beside the status ``checked``), and what the
#: interval slot says until a sampled ``ship`` fills it (SAMPLE-SPEC.md §7).
_SHAPE_FLAGS = {"status": "not checked"}
_INTERVALS = {"status": "not computed"}


def display(value: Optional[Fraction]) -> str:
    """A score for a person: exact when it has at most four places, else rounded down to four."""
    if value is None:
        return NOT_MEASURED
    scaled = math.floor(value * 10_000)
    if Fraction(scaled, 10_000) == value:
        return render_exact(value)
    return f"{scaled // 10_000}.{scaled % 10_000:04d}"


def _exact(value: Optional[Fraction]) -> Optional[str]:
    return None if value is None else render_exact(value)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyScore:
    """One declared property: its outcome, its score and where it is counted.

    ``groups_outside`` names the groups that landed outside by ordinal in
    the NFC-sorted group order, never by key: a key is a value of the data.
    """

    expose_id: str
    property_id: str
    kind: str
    statistic: str
    columns: Tuple[str, ...]
    group_by: Optional[str]
    dimension: str
    outcome: str
    score: Optional[Fraction]
    groups_scored: int
    groups_censored: int
    groups_outside: Tuple[int, ...]
    tolerance: Mapping[str, str]
    margin: Optional[str]
    refusals: Tuple[Refusal, ...] = ()
    weight: Fraction = _ONE
    #: A row of a kind other than a profile statistic (a fact, a relationship; format 1-3-0): its
    #: document as the scorecard writes it, and what it measured, for a person.
    document: Optional[Mapping[str, Any]] = None
    label: str = ""
    #: The noise of the profile statistic the property was compared with (format 1-3-0): the
    #: variance of each cell and the largest widening a group's margin took. ``None`` for an
    #: exact reference.
    noise: Optional[Mapping[str, str]] = None
    #: The widening the run's sampling gave the margin of a distance tolerance that declares
    #: ``sampling: true`` (format 1-4-0): the largest any group took. ``None`` without the key.
    sampling: Optional[str] = None
    #: A rank correlation's comparison, as the row writes it (format 1-5-0): the measure, both
    #: values, the distance, the sampling widening and what the two may differ by, and the copula's
    #: ``alpha`` when the run's plan has one. ``None`` for every other kind.
    rank: Optional[Mapping[str, Any]] = None

    @property
    def subject(self) -> str:
        """``mean of amount``, ``correlation of a, b, grouped by market``: what it measured."""
        if self.document is not None:
            return self.label
        text = f"{self.kind} of {', '.join(self.columns)}"
        if self.group_by is not None:
            text += f", grouped by {self.group_by}"
        return text

    def to_document(self) -> Dict[str, Any]:
        """The property's row in the scorecard."""
        if self.document is not None:
            return dict(self.document)
        document: Dict[str, Any] = {
            "propertyId": self.property_id,
            "property": self.kind,
            "statistic": self.statistic,
            "columns": list(self.columns),
            "dimension": self.dimension,
            "outcome": self.outcome,
            "score": _exact(self.score),
            "weight": render_exact(self.weight),
            "groups": {
                "scored": self.groups_scored,
                "censored": self.groups_censored,
                "outside": list(self.groups_outside),
            },
            "tolerance": dict(sorted(self.tolerance.items())),
            "margin": self.margin,
            "refusals": [
                {"code": refusal.code, "reason": refusal.reason} for refusal in self.refusals
            ],
        }
        if self.group_by is not None:
            document["groupBy"] = self.group_by
        if self.noise is not None:
            document["noise"] = dict(self.noise)
        if self.sampling is not None:
            document["widening"] = {"sampling": self.sampling}
        if self.rank is not None:
            document["rank"] = dict(self.rank)
        return document

    def assertion(self) -> Optional[Dict[str, Any]]:
        """This property as one OpenLineage data-quality assertion, or ``None`` when censored.

        An assertion records a test that ran. A censored property ran no
        comparison, so it has none; a refused one ran and declined, which
        is not a success.
        """
        if self.outcome == CENSORED:
            return None
        if self.document is not None:
            return {
                "assertion": self.kind,
                "success": self.outcome == WITHIN,
                "severity": "error",
                "name": self.property_id,
                "params": {
                    "dimension": self.dimension,
                    "outcome": self.outcome,
                    "score": render_exact(self.score) if self.score is not None else None,
                },
            }
        params: Dict[str, Any] = {
            "dimension": self.dimension,
            "outcome": self.outcome,
            "tolerance": dict(sorted(self.tolerance.items())),
        }
        if self.score is not None:
            params["score"] = render_exact(self.score)
        if self.group_by is not None:
            params["groupBy"] = self.group_by
        entry: Dict[str, Any] = {
            "assertion": self.statistic,
            "success": self.outcome == WITHIN,
            "severity": "error",
            "name": self.property_id,
            "params": params,
        }
        if len(self.columns) == 1:
            entry["column"] = self.columns[0]
        else:
            # A pair is neither column alone, and an empty column means the
            # whole dataset to OpenLineage; the pair travels in params.
            params["columns"] = list(self.columns)
        return entry


def _holds_1_5_0(prop: PropertyScore) -> bool:
    """Whether a property row holds what format 1-5-0 adds: a rank correlation, or a grouped
    children-per-parent row (the ``groupBy`` or ``groupByBins`` member its document then has)."""
    if prop.rank is not None:
        return True
    document = prop.document
    return (
        document is not None
        and document.get("property") == "childrenPerParent"
        and ("groupBy" in document or "groupByBins" in document)
    )


def _holds_1_4_0(prop: PropertyScore) -> bool:
    """Whether a property row holds what format 1-4-0 adds to 1-3-0 (the ``sampling`` member)."""
    return prop.sampling is not None


@dataclass(frozen=True)
class Aggregate:
    """A score over a set of properties, and how many of them it could use."""

    score: Optional[Fraction]
    properties: int
    scored: int
    censored: int
    refused: int

    @property
    def status(self) -> str:
        """``measured`` when some property was scored, else ``not measured``."""
        return MEASURED if self.score is not None else NOT_MEASURED

    def counts(self) -> Dict[str, int]:
        """The counts behind the score."""
        return {
            "properties": self.properties,
            "scored": self.scored,
            "censored": self.censored,
            "refused": self.refused,
        }


def _aggregate(properties: Sequence[PropertyScore]) -> Aggregate:
    pairs = [(entry.weight, entry.score) for entry in properties if entry.score is not None]
    score: Optional[Fraction] = None
    if pairs:
        total = sum((weight for weight, _ in pairs), Fraction(0))
        score = sum((weight * value for weight, value in pairs), Fraction(0)) / total
    return Aggregate(
        score=score,
        properties=len(properties),
        scored=len(pairs),
        censored=sum(1 for entry in properties if entry.outcome == CENSORED),
        refused=sum(1 for entry in properties if entry.outcome == REFUSED),
    )


@dataclass(frozen=True)
class DeclaredLink:
    """One relationship the contract declares: a child's key columns and its parent's.

    The child expose declares it (``semantics.relationships``), so ``child`` is
    that expose and ``parent`` the one it names with ``to``. The columns pair up
    by position.
    """

    parent: str
    parent_columns: Tuple[str, ...]
    child: str
    child_columns: Tuple[str, ...]


@dataclass(frozen=True)
class ContractShape:
    """What coverage counts of one emit's contract: its columns and its declared relationships."""

    columns: Tuple[str, ...]
    relationships: Tuple[DeclaredLink, ...] = ()


@dataclass(frozen=True)
class Share:
    """``covered`` of ``of``, with the exact share when there is something to cover."""

    covered: int = 0
    of: int = 0

    @property
    def share(self) -> Optional[Fraction]:
        """``covered / of``, or ``None`` when nothing was declared: there is no share to state."""
        return Fraction(self.covered, self.of) if self.of else None

    def to_document(self) -> Dict[str, Any]:
        """The share's row in the scorecard."""
        return {"covered": self.covered, "of": self.of, "share": _exact(self.share)}

    def __add__(self, other: "Share") -> "Share":
        return Share(self.covered + other.covered, self.of + other.of)


@dataclass(frozen=True)
class Coverage:
    """How much of a contract a score speaks for.

    ``columns`` counts the contract's columns that carry at least one scored
    property. ``relationships`` counts the declared relationships the profile
    constrains: it holds an entry for that parent and child, over those key
    columns, with at least one published statistic. That is counted, not
    scored.
    """

    columns: Share = Share()
    relationships: Share = Share()

    def of_kind(self, kind: str) -> Share:
        """``columns`` or ``relationships``."""
        return self.columns if kind == "columns" else self.relationships

    def to_document(self) -> Dict[str, Any]:
        """The coverage's row in the scorecard."""
        return {
            "columns": self.columns.to_document(),
            "relationships": self.relationships.to_document(),
        }

    def text(self) -> str:
        """``covers 12 of 20 columns, 0 of 3 relationships``."""
        return (
            f"covers {self.columns.covered} of {self.columns.of} "
            f"{'column' if self.columns.of == 1 else 'columns'}, "
            f"{self.relationships.covered} of {self.relationships.of} "
            f"{'relationship' if self.relationships.of == 1 else 'relationships'}"
        )


@dataclass(frozen=True)
class RunCoverage:
    """The run's coverage: the sum over the tables whose contract the run records.

    ``tables_counted`` of ``tables_total`` tables are in the sum. A table whose
    contract the run does not record (one with no fidelity block) is left out
    rather than guessed at.
    """

    coverage: Coverage = Coverage()
    tables_counted: int = 0
    tables_total: int = 0

    def text(self) -> str:
        """The coverage as a sentence fragment, naming the tables it leaves out."""
        if self.tables_counted == 0:
            return "coverage not known: no table's contract is recorded"
        text = self.coverage.text()
        left_out = self.tables_total - self.tables_counted
        if left_out:
            text += (
                f"; {left_out} of {self.tables_total} "
                f"{'table' if self.tables_total == 1 else 'tables'} not counted, "
                "no contract recorded"
            )
        return text

    def to_document(self) -> Dict[str, Any]:
        """The run's ``coverage`` entry."""
        return {
            **self.coverage.to_document(),
            "tables": {"counted": self.tables_counted, "total": self.tables_total},
        }


def _published(profile: Profile, link: DeclaredLink) -> bool:
    """Whether *profile* holds a published statistic for *link*'s parent and child."""
    return any(
        entry.parent_table == link.parent
        and entry.child_table == link.child
        and tuple(nfc(column) for column in entry.parent_columns) == link.parent_columns
        and tuple(nfc(column) for column in entry.child_columns) == link.child_columns
        and any(group.published for statistic in entry.statistics for group in statistic.groups)
        for entry in profile.relationships
    )


def _coverage_of(
    contract: ContractShape, properties: Sequence["PropertyScore"], profile: Optional[Profile]
) -> Coverage:
    scored = {
        nfc(column) for entry in properties if entry.score is not None for column in entry.columns
    }
    names = {nfc(column) for column in contract.columns}
    links = list(contract.relationships)
    return Coverage(
        columns=Share(len(names & scored), len(names)),
        relationships=Share(
            sum(1 for link in links if profile is not None and _published(profile, link)),
            len(links),
        ),
    )


@dataclass(frozen=True)
class TableScore:
    """One emit's table: its properties and their aggregate.

    ``refusals`` are the table's own: a fail-closed condition that stopped
    every property of it (the profile holds no table for the expose, a row
    count outside ``records``), which leaves each property ``refused``.
    """

    expose_id: str
    declares_fidelity: bool
    properties: Tuple[PropertyScore, ...]
    refusals: Tuple[Refusal, ...]
    aggregate: Aggregate
    #: What the score speaks for; ``None`` when the run records no contract for the emit.
    coverage: Optional[Coverage] = None

    @property
    def status(self) -> str:
        """``refused``, ``measured`` or ``not measured``."""
        return REFUSED if self.refusals else self.aggregate.status


def _table_state(table: TableScore) -> str:
    """One table's line: its score and what it was formed from, or why there is none."""
    if table.status == REFUSED:
        return "refused: " + "; ".join(
            f"{refusal.code} {refusal.reason}" for refusal in table.refusals
        )
    if not table.declares_fidelity and not table.properties:
        return "not measured (no fidelity block)"
    counts = table.aggregate
    if not counts.properties:
        return "not scored (the fidelity block declares no property, so nothing is verified)"
    noun = "property" if counts.properties == 1 else "properties"
    detail = f"{counts.scored} of {counts.properties} {noun} scored"
    if counts.censored:
        detail += f", {counts.censored} censored"
    if counts.refused:
        detail += f", {counts.refused} refused"
    return f"{display(counts.score)} ({detail})"


@dataclass(frozen=True)
class ThresholdResult:
    """One declared threshold, the score it reads and whether that score clears it."""

    threshold: Threshold
    score: Optional[Fraction]
    met: bool

    def to_document(self) -> Dict[str, Any]:
        """The threshold's row in the scorecard."""
        return {
            "target": self.threshold.target,
            "operator": self.threshold.operator,
            "value": self.threshold.declared,
            "score": _exact(self.score),
            "met": self.met,
        }

    def line(self) -> str:
        """``threshold overall mustBeGreaterOrEqualTo 0.9: met (0.9871)``."""
        state = "met" if self.met else "not met"
        found = display(self.score)
        return (
            f"threshold {self.threshold.target} {self.threshold.operator} "
            f"{self.threshold.declared}: {state} ({found})"
        )


@dataclass(frozen=True)
class CredibilityResult:
    """An emit's ``minCredibility``, read against its table's computed score.

    The computed score: wherever a property measures something, the computed score
    replaces the declared credibility. ``basis`` is ``measured`` when the
    table has a score, and ``met`` says whether the score reaches the floor.
    Otherwise ``basis`` is ``declared`` and ``met`` is ``None``: the
    Playback's declared credibility, which ``fluxsim validate`` checks, is
    the only basis there is.
    """

    expose_id: str
    declared: str
    value: Fraction
    score: Optional[Fraction]

    @property
    def basis(self) -> str:
        """``measured`` or ``declared``."""
        return "measured" if self.score is not None else "declared"

    @property
    def met(self) -> Optional[bool]:
        """Whether the measured score reaches ``minCredibility``; ``None`` when unmeasured."""
        return None if self.score is None else self.score >= self.value

    def to_document(self) -> Dict[str, Any]:
        """The table's ``credibility`` entry."""
        return {
            "minCredibility": self.declared,
            "basis": self.basis,
            "score": _exact(self.score),
            "met": self.met,
        }

    def line(self) -> str:
        """``credibility x: measured 0.9871 meets minCredibility 0.7``."""
        if self.score is None:
            return (
                f"credibility {self.expose_id}: no property of this table was scored, so "
                f"minCredibility {self.declared} stays with the Playback's declared credibility"
            )
        verdict = "meets" if self.met else "is below"
        return (
            f"credibility {self.expose_id}: measured {display(self.score)} {verdict} "
            f"minCredibility {self.declared}"
        )


@dataclass(frozen=True)
class Scorecard:
    """A run's scores against its pinned profile, from properties up to the run."""

    run_id: str
    plan_digest: str
    run_digest: str
    profile_digest: str
    tables: Tuple[TableScore, ...]
    dimensions: Tuple[Tuple[str, Aggregate], ...]
    overall: Aggregate
    thresholds: Tuple[ThresholdResult, ...]
    credibility: Tuple[CredibilityResult, ...]
    #: The shape flags; ``None`` when none were checked. They inform: :attr:`met` does not
    #: read them and nothing withholds on them, unless the Simulation declares a limit
    #: (:attr:`shape_limit`, SCORECARD-SPEC.md §6).
    shape: Optional["ShapeReport"] = None
    #: The most shape flags the Simulation allows (``shape.maxFlags``); ``None`` declares none.
    shape_limit: Optional[int] = None
    #: How much of the contract the score speaks for, over every table that records one.
    coverage: RunCoverage = field(default_factory=RunCoverage)
    #: The profile overlay the run was scored with, its name and digest (format 1-4-0); ``None``
    #: when the run was scored against the pinned profile as published.
    overlay: Optional[Tuple[str, str]] = None
    #: The consumers' queries as counted on the run (ADR 0028 section 7), which score the ``queries``
    #: dimension; ``None`` for a bundle that declares none. A scorecard that holds it is format 1-4-0.
    queries: Optional[QueriesDimension] = None
    #: The rule, calendar and exposure lines that fail the run, as ``verify`` prints them. They are
    #: not scores, so no threshold reads them, but a run with one is not met.
    line_failures: Tuple[str, ...] = ()

    @property
    def met(self) -> bool:
        """Every declared threshold met, every measured ``minCredibility`` reached, the shape limit kept, and no line failed."""
        return (
            all(result.met for result in self.thresholds)
            and all(result.met is not False for result in self.credibility)
            and self.shape_met
            and not self.line_failures
        )

    @property
    def shape_flags(self) -> int:
        """How many shape flags the run shows, over every column of every emit."""
        if self.shape is None:
            return 0
        return sum(len(column.flags) for _, column in self.shape.flagged())

    @property
    def shape_met(self) -> bool:
        """Whether the run keeps its declared shape limit; true when it declares none."""
        return self.shape_limit is None or self.shape_flags <= self.shape_limit

    def shape_flagged_tables(self) -> Tuple[str, ...]:
        """The emits with a flagged column, in ``exposeId`` order."""
        if self.shape is None:
            return ()
        return tuple(sorted({expose_id for expose_id, _ in self.shape.flagged()}))

    def shape_failure_line(self) -> Optional[str]:
        """The line saying a run shows more shape flags than it may, or ``None`` when it does not."""
        if self.shape is None or self.shape_met:
            return None
        named = "; ".join(
            f"{expose_id}.{column.summary.column} ({', '.join(flag.alert for flag in column.flags)})"
            for expose_id, column in self.shape.flagged()
        )
        count = self.shape_flags
        return (
            f"shape: {count} flag{'' if count == 1 else 's'} against the {self.shape_limit} "
            f"the scorecard allows (maxFlags): {named}"
        )

    def table(self, expose_id: str) -> Optional[TableScore]:
        """One table's scores, or ``None``."""
        return next((table for table in self.tables if table.expose_id == expose_id), None)

    def contributors(self, threshold: Threshold) -> Tuple[str, ...]:
        """The emits the score *threshold* reads is about.

        ``ship`` withholds these when the threshold is not met. A table
        threshold names its table. A dimension or overall threshold names the
        tables whose scored properties fed it; when none was scored, the
        tables with a property under it (censored or refused); then every
        table that declares a fidelity block, and failing that every table,
        so a missed threshold always withholds something.
        """
        if threshold.scope == "table" and self.table(str(threshold.name)) is not None:
            return (str(threshold.name),)
        if threshold.scope == "coverage":
            counted = [table for table in self.tables if table.coverage is not None]
            short = tuple(
                table.expose_id
                for table in counted
                if table.coverage is not None
                and not threshold.met_by(table.coverage.of_kind(str(threshold.name)).share)
            )
            if short or counted:
                return short or tuple(table.expose_id for table in counted)

        def under(entry: PropertyScore) -> bool:
            return threshold.scope == "overall" or entry.dimension == threshold.name

        if threshold.scope in ("overall", "dimension"):
            for wanted in (
                lambda entry: under(entry) and entry.score is not None,
                under,
            ):
                found = tuple(
                    table.expose_id
                    for table in self.tables
                    if any(wanted(entry) for entry in table.properties)
                )
                if found:
                    return found
        declaring = tuple(table.expose_id for table in self.tables if table.declares_fidelity)
        return declaring or tuple(table.expose_id for table in self.tables)

    def lowest(self, threshold: Threshold) -> Optional[PropertyScore]:
        """The scored property with the lowest score under *threshold*'s target.

        Where to look first when the threshold is missed. Ties go to the
        first in table and declaration order.
        """
        if threshold.scope == "coverage":
            return None
        candidates = [
            entry
            for table in self.tables
            if threshold.scope != "table" or table.expose_id == threshold.name
            for entry in table.properties
            if entry.score is not None
            and (threshold.scope != "dimension" or entry.dimension == threshold.name)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry.score or Fraction(0))

    def threshold_line(self, result: ThresholdResult) -> str:
        """*result*'s line, and for a missed threshold the property to look at first."""
        line = result.line()
        if result.met:
            return line
        if result.threshold.scope == "coverage":
            return f"{line}; {self.coverage.text()}"
        weakest = self.lowest(result.threshold)
        if weakest is not None:
            return (
                f"{line}; lowest property {weakest.property_id} ({weakest.subject}) of "
                f"{weakest.expose_id}: {display(weakest.score)}"
            )
        return f"{line}; no property under it was scored"

    def failure_lines(self) -> List[str]:
        """One line per missed threshold or ``minCredibility``: what was missed, and where to look."""
        lines = [self.threshold_line(result) for result in self.thresholds if not result.met]
        lines.extend(result.line() for result in self.credibility if result.met is False)
        shape = self.shape_failure_line()
        if shape is not None:
            lines.append(shape)
        return lines

    def summary_lines(self) -> List[str]:
        """What a person reads: the run, then each dimension, table, threshold and floor."""
        return [
            f"scorecard: overall {display(self.overall.score)} against profile "
            f"{self.profile_digest}{self._with_overlay()} ({self.coverage.text()})",
            *(f"  {line}" for line in self.detail_lines()),
        ]

    def _with_overlay(self) -> str:
        """`` with the overlay NAME``, or nothing when the run was scored without one."""
        return f" with the overlay {self.overlay[0]}" if self.overlay is not None else ""

    def detail_lines(self) -> List[str]:
        """The lines under the overall score, unindented."""
        lines: List[str] = []
        measured = [
            f"{name} {display(aggregate.score)}"
            for name, aggregate in self.dimensions
            if aggregate.score is not None
        ]
        unmeasured = [name for name, aggregate in self.dimensions if aggregate.score is None]
        text = ", ".join(measured) if measured else "no dimension was measured"
        if unmeasured:
            text += "; not measured: " + ", ".join(unmeasured)
        lines.append(f"dimensions: {text}")
        for table in self.tables:
            state = _table_state(table)
            if table.coverage is not None:
                state += f"; {table.coverage.text()}"
            lines.append(f"table {table.expose_id}: {state}")
        if self.queries is not None:
            lines.extend(self.queries.lines())
        lines.extend(result.line() for result in self.thresholds)
        lines.extend(result.line() for result in self.credibility)
        if self.shape is not None:
            lines.extend(self.shape.summary_lines(self.shape_limit))
        return lines

    def _format(self) -> str:
        """The schema the document names: the oldest format that can hold every row it has."""
        rows = [prop for table in self.tables for prop in table.properties]
        if any(_holds_1_5_0(prop) for prop in rows):
            return SCHEMA_URL_1_5_0
        if (
            self.overlay is not None
            or self.queries is not None
            or any(prop.kind in _FORMAT_1_4_0_ROWS or _holds_1_4_0(prop) for prop in rows)
        ):
            return SCHEMA_URL_1_4_0
        if any(prop.document is not None or prop.noise is not None for prop in rows):
            return SCHEMA_URL_1_3_0
        return SCHEMA_URL

    def to_document(self) -> Dict[str, Any]:
        """The scorecard document (docs/spec/scorecard-1.schema.json)."""
        credibility = {result.expose_id: result for result in self.credibility}
        tables: List[Dict[str, Any]] = []
        for table in self.tables:
            assertions = [
                entry
                for entry in (prop.assertion() for prop in table.properties)
                if entry is not None
            ]
            document: Dict[str, Any] = {
                "exposeId": table.expose_id,
                "declaresFidelity": table.declares_fidelity,
                "status": table.status,
                "score": _exact(table.aggregate.score),
                "counts": table.aggregate.counts(),
                "refusals": [
                    {"code": refusal.code, "reason": refusal.reason} for refusal in table.refusals
                ],
                "properties": [entry.to_document() for entry in table.properties],
                "coverage": table.coverage.to_document() if table.coverage is not None else None,
                "dataQualityAssertions": {
                    "_producer": OPENLINEAGE_PRODUCER,
                    "_schemaURL": ASSERTIONS_SCHEMA_URL,
                    "assertions": assertions,
                },
            }
            if table.expose_id in credibility:
                document["credibility"] = credibility[table.expose_id].to_document()
            tables.append(document)
        run_document: Dict[str, Any] = {
            "_producer": OPENLINEAGE_PRODUCER,
            "_schemaURL": self._format(),
            "run": {
                "runId": self.run_id,
                "planDigest": self.plan_digest,
                "runDigest": self.run_digest,
            },
            "profile": {"digest": self.profile_digest},
            "met": self.met,
            "overall": {
                "status": self.overall.status,
                "score": _exact(self.overall.score),
                "counts": self.overall.counts(),
            },
            "dimensions": [
                self._dimension_document(name, aggregate) for name, aggregate in self.dimensions
            ],
            "coverage": self.coverage.to_document(),
            "tables": tables,
            "thresholds": [result.to_document() for result in self.thresholds],
            "shapeFlags": (
                self.shape.to_document() if self.shape is not None else dict(_SHAPE_FLAGS)
            ),
            "intervals": dict(_INTERVALS),
        }
        if self.overlay is not None:
            run_document["overlay"] = {"name": self.overlay[0], "digest": self.overlay[1]}
        return run_document

    def _dimension_document(self, name: str, aggregate: Aggregate) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "dimension": name,
            "status": aggregate.status,
            "score": _exact(aggregate.score),
            "weight": "1",
            "counts": aggregate.counts(),
        }
        # Format 1-4-0: the queries the run was counted on sit in their own member of their dimension.
        if name == _QUERIES and self.queries is not None:
            document["queries"] = self.queries.to_document()
        return document

    def to_bytes(self) -> bytes:
        """The document as written: sorted keys, two-space indent, a final newline.

        The same function of the same inputs everywhere: no clock, no host,
        and every number an integer or an exact rational spelled as a string.
        """
        text = json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n"
        return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Building it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableInput:
    """What one emit hands the scorecard.

    ``compiled`` and ``evaluation`` are ``None`` for an emit with no
    fidelity block. ``min_credibility`` is the emit's declared
    ``minCredibility``, as a decimal string, or ``None``.
    """

    expose_id: str
    compiled: Optional[CompiledFidelity] = None
    evaluation: Optional[FidelityEvaluation] = None
    min_credibility: Optional[str] = None
    #: Whether the emit declares a fidelity block. ``None`` reads it off
    #: ``compiled``; a declared block whose record could not be compiled
    #: passes ``True`` with the evaluation that says why.
    declares_fidelity: Optional[bool] = None
    #: The emit's contract columns and declared relationships, for coverage;
    #: ``None`` when the run records no contract for it.
    contract: Optional[ContractShape] = None
    #: The fact and relationship rows of this emit (format 1-3-0), scored apart from the profile's
    #: statistics (:mod:`flux_engine.concordance.module_scores`).
    module_rows: Tuple["PropertyScore", ...] = ()


def _rank_document(
    outcome: Optional[PropertyOutcome], alpha: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The ``rank`` member of a rank correlation's row, or ``None`` when no comparison was made."""
    found = outcome.rank if outcome is not None else None
    if found is None:
        return None
    document: Dict[str, Any] = {
        "measure": found.measure,
        "records": found.records,
        "run": render_exact(found.run),
        "reference": render_exact(found.reference),
        "distance": render_exact(found.distance),
        "samplingWidening": render_exact(found.sampling),
        "reach": render_exact(found.reach),
        "tooNoisyToTell": found.too_noisy,
    }
    if alpha is not None:
        document["alpha"] = alpha
    return document


def _property_score(
    expose_id: str,
    entry: CompiledProperty,
    outcome: Optional[PropertyOutcome],
    refusals: Sequence[Refusal],
    table_refused: bool,
    alpha: Optional[str] = None,
) -> PropertyScore:
    declaration = entry.declaration
    kind = declaration.kind.value
    groups = outcome.groups if outcome is not None else ()
    own = tuple(refusal for refusal in refusals if refusal.property_id == entry.property_id)
    scores = [
        group.score
        for group in groups
        if group.outcome in (WITHIN, OUTSIDE) and group.score is not None
    ]
    status: str
    score: Optional[Fraction] = None
    if table_refused or own or outcome is None:
        status = REFUSED
    elif outcome.outcome == CENSORED or not scores:
        status = CENSORED
    else:
        status = outcome.outcome
        score = sum(scores, Fraction(0)) / len(scores)
    return PropertyScore(
        expose_id=expose_id,
        property_id=entry.property_id,
        kind=kind,
        statistic=definition_for_property(declaration).identifier,
        columns=tuple(declaration.columns),
        group_by=declaration.group_by,
        dimension=dimension_of(kind, declaration.group_by is not None),
        outcome=status,
        score=score,
        groups_scored=len(scores),
        groups_censored=sum(1 for group in groups if group.outcome == CENSORED),
        groups_outside=tuple(
            ordinal for ordinal, group in enumerate(groups) if group.outcome == OUTSIDE
        ),
        tolerance=dict(declaration.tolerance.declared),
        margin=render_exact(entry.margin) if entry.margin is not None else None,
        refusals=own,
        sampling=(
            render_exact(outcome.sampling)
            if outcome is not None and outcome.sampling is not None
            else None
        ),
        noise=(
            {
                "mechanism": outcome.noise_mechanism or "",
                "cellVariance": render_exact(outcome.cell_variance),
                "widening": render_exact(outcome.widening or Fraction(0)),
            }
            if outcome is not None and outcome.cell_variance is not None
            else None
        ),
        rank=_rank_document(outcome, alpha),
    )


def _table(
    table: TableInput, profile: Optional[Profile], alpha: Optional[str] = None
) -> TableScore:
    compiled, evaluation = table.compiled, table.evaluation
    declares = (
        table.declares_fidelity if table.declares_fidelity is not None else compiled is not None
    )
    if compiled is None:
        # No block to list properties from: a table that declares one is
        # refused with the evaluation's reasons, never shown as having none.
        return TableScore(
            expose_id=table.expose_id,
            declares_fidelity=declares,
            properties=table.module_rows,
            refusals=tuple(evaluation.refusals) if declares and evaluation is not None else (),
            aggregate=_aggregate(table.module_rows),
            coverage=(
                _coverage_of(table.contract, (), profile) if table.contract is not None else None
            ),
        )
    refusals = evaluation.refusals if evaluation is not None else ()
    table_refusals = tuple(refusal for refusal in refusals if refusal.property_id is None)
    outcomes = {
        outcome.property_id: outcome
        for outcome in (evaluation.outcomes if evaluation is not None else ())
    }
    properties = (
        tuple(
            _property_score(
                table.expose_id,
                entry,
                outcomes.get(entry.property_id),
                refusals,
                bool(table_refusals),
                alpha,
            )
            for entry in compiled.properties
        )
        + table.module_rows
    )
    return TableScore(
        expose_id=table.expose_id,
        declares_fidelity=True,
        properties=properties,
        refusals=table_refusals,
        aggregate=_aggregate(properties),
        coverage=(
            _coverage_of(table.contract, properties, profile)
            if table.contract is not None
            else None
        ),
    )


def _queries_aggregate(queries: QueriesDimension) -> Aggregate:
    """The ``queries`` dimension: the smaller of the shares measured, and the counts of its queries."""
    counts = queries.counts()
    return Aggregate(
        score=queries.score,
        properties=counts["properties"],
        scored=counts["scored"],
        censored=counts["censored"],
        refused=counts["refused"],
    )


def _with_queries(
    dimensions: Tuple[Tuple[str, Aggregate], ...], queries: Optional[QueriesDimension]
) -> Tuple[Tuple[str, Aggregate], ...]:
    """*dimensions* with the ``queries`` one scored from the consumers' queries, when there are any."""
    if queries is None:
        return dimensions
    return tuple(
        (name, _queries_aggregate(queries) if name == _QUERIES else aggregate)
        for name, aggregate in dimensions
    )


def build_scorecard(
    *,
    run_id: str,
    plan_digest: str,
    run_digest: str,
    profile_digest: str,
    tables: Sequence[TableInput],
    declaration: Optional[ScorecardDeclaration] = None,
    profile: Optional[Profile] = None,
    module_rows: Optional[Mapping[str, Sequence["PropertyScore"]]] = None,
    overlay: Optional[Tuple[str, str]] = None,
    queries: Optional[QueriesDimension] = None,
    copula_alphas: Optional[Mapping[str, str]] = None,
) -> Scorecard:
    """Score a run from its per-emit evaluations against the pinned profile.

    *tables* is one entry per emit the run wrote, in any order; the
    scorecard lists them by ``exposeId``. *declaration* holds the
    Simulation's thresholds, or ``None`` when it declares none. The run's
    shape flags join the result afterwards (:attr:`Scorecard.shape`). *profile*
    is the pinned profile, which says which declared relationships it
    constrains; without it none is counted as constrained. *module_rows* holds the fact and
    relationship rows of each emit by ``exposeId`` (format 1-3-0), which sit in its table.
    *overlay* is the name and digest of the profile overlay *profile* was made with, if any
    (format 1-4-0): the scorecard then names it. *queries* holds the consumers' queries as counted on
    the run (format 1-4-0): it scores the ``queries`` dimension, which otherwise no property of a
    table does. *copula_alphas* holds the shrinkage of each emit whose plan has a copula, by
    ``exposeId`` (format 1-5-0): a rank correlation row of that emit carries it.
    """
    if module_rows:
        tables = [
            replace(table, module_rows=tuple(module_rows.get(table.expose_id, ())))
            for table in tables
        ]
    alphas = copula_alphas or {}
    scored = tuple(
        _table(table, profile, alphas.get(table.expose_id))
        for table in sorted(tables, key=lambda entry: entry.expose_id)
    )
    everything = [entry for table in scored for entry in table.properties]
    dimensions = tuple(
        (name, _aggregate([entry for entry in everything if entry.dimension == name]))
        for name in DIMENSIONS
    )
    dimensions = _with_queries(dimensions, queries)
    measured = [aggregate.score for _, aggregate in dimensions if aggregate.score is not None]
    overall = Aggregate(
        score=(sum(measured, Fraction(0)) / len(measured)) if measured else None,
        properties=len(everything),
        scored=sum(1 for entry in everything if entry.score is not None),
        censored=sum(1 for entry in everything if entry.outcome == CENSORED),
        refused=sum(1 for entry in everything if entry.outcome == REFUSED),
    )
    counted = [table.coverage for table in scored if table.coverage is not None]
    run_coverage = RunCoverage(
        coverage=Coverage(
            columns=sum((entry.columns for entry in counted), Share()),
            relationships=sum((entry.relationships for entry in counted), Share()),
        ),
        tables_counted=len(counted),
        tables_total=len(scored),
    )
    by_dimension = dict(dimensions)
    by_table = {table.expose_id: table.aggregate for table in scored}
    results: List[ThresholdResult] = []
    for threshold in declaration.thresholds if declaration is not None else ():
        if threshold.scope == "overall":
            score = overall.score
        elif threshold.scope == "dimension":
            score = by_dimension[str(threshold.name)].score
        elif threshold.scope == "coverage":
            score = run_coverage.coverage.of_kind(str(threshold.name)).share
        else:
            found = by_table.get(str(threshold.name))
            score = found.score if found is not None else None
        results.append(
            ThresholdResult(threshold=threshold, score=score, met=threshold.met_by(score))
        )
    credibility = tuple(
        CredibilityResult(
            expose_id=table.expose_id,
            declared=table.min_credibility,
            value=Fraction(table.min_credibility),
            score=by_table[table.expose_id].score,
        )
        for table in sorted(tables, key=lambda entry: entry.expose_id)
        if table.min_credibility is not None
    )
    return Scorecard(
        run_id=run_id,
        plan_digest=plan_digest,
        run_digest=run_digest,
        profile_digest=profile_digest,
        tables=scored,
        dimensions=dimensions,
        overall=overall,
        thresholds=tuple(results),
        credibility=credibility,
        coverage=run_coverage,
        shape_limit=declaration.max_shape_flags if declaration is not None else None,
        overlay=overlay,
        queries=queries,
    )
