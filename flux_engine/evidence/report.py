# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``EVIDENCE.md`` and the ``claims[]`` that back it.

Eight fixed sections, always in this order, always all eight present --
"fixed" so a reader who has seen one evidence pack knows exactly where to
look in the next one. A run with forecasts registered for its bundle gains an
ninth, "Forecast Accuracy", after "Fidelity" (:func:`evidence_sections`,
:func:`render_forecast_accuracy_section`); a run with none has the eight:

1. **What This Bundle Is** -- identity: bundle name, kernel version, the
   run digest, when it was generated.
2. **Conformance** -- did the bundle obey the FLUX schema and its
   cross-document rules.
3. **Reproducibility** -- did the same bundle and seed produce the same
   bytes.
4. **Fidelity** -- what the fidelity check (SEAM-FIDELITY-SPEC) found, if
   anything was checked.
5. **Population**: the run's histograms against the profile's, one chart
   each (:mod:`flux_engine.evidence.population`, ADR 0031).
6. **Privacy Grade**: the pattern the run's numbers rest on, per column and
   for the run, and the guarantee that pattern carries, in the terms NIST SP
   800-226 asks for (:mod:`flux_engine.evidence.grades`, ADR 0020).
7. **Column Provenance** -- how each column of each exposed table was
   produced (declared, copied, derived or a type default) and from what
   (:mod:`flux_engine.evidence.provenance`).
8. **What Was Not Proven** -- **mandatory**, generated from a fixed
   template (never hand-edited, the same discipline SEAM-FIDELITY-SPEC §6
   applies to a fidelity record's own ``doesNotCertify`` list): in v0.1
   this always states that no validity claim is being made and names
   ``temperature.campaignResponse`` as an authored dial -- a value the
   bundle's author declared, not one this engine measured or fitted. It
   also names every column that fell back to a type default, or says that
   none did, or says that the run recorded nothing to tell. When the run's
   plan binds a parameter register it states the shares of the run's
   numbers that rest on the pinned profile, on assumptions and on
   defaults, and lists every assumption with its reason
   (:mod:`flux_engine.evidence.register`).

:func:`build_claims` assembles the ``claims[]`` a customer-facing record
carries alongside the prose: one :class:`~flux_engine.api.claims.Claim`
each for conformance and reproducibility (basis
:attr:`~flux_engine.api.claims.ClaimBasis.NONE` -- neither is a claim
"measured against the world"), the fidelity claims a caller already built
elsewhere, passed through unchanged, and a validity claim **only** when a
*promoted* :class:`~flux_engine.api.accuracy.AccuracyVerdict` is supplied
(see :func:`flux_engine.accuracy.policy.promote`) -- never fabricated to
fill the fourth slot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flux_engine._vocabulary import BANNED_WORDS
from flux_engine._vocabulary import find_banned_words as _find_banned_words
from flux_engine.api.accuracy import AccuracyVerdict
from flux_engine.api.claims import Claim, ClaimBasis, ClaimType
from flux_engine.api.digests import DigestRef
from flux_engine.api.verdict import Reproducibility, Validity
from flux_engine.core.errors import FluxError, register
from flux_engine.evidence.forecast_accuracy import (
    NOT_SCORED,
    PENDING,
    ForecastAccuracy,
    ForecastRow,
)
from flux_engine.evidence.grades import GeneratorGrade, Pattern, RunGrade, weakest
from flux_engine.evidence.population import PopulationProperty, render_population_section
from flux_engine.evidence.provenance import (
    quoted_name,
    render_not_generated_sentence,
    render_provenance_section,
    render_uncovered_columns,
)
from flux_engine.seam.coverage import ExposeCoverage
from flux_engine.seam.declared import NotGenerated

__all__ = [
    "DEFAULT_AUTHORED_DIALS",
    "EVIDENCE_SECTIONS",
    "FLX_8030",
    "FORECAST_ACCURACY_SECTION",
    "StoredGenerator",
    "build_claims",
    "evidence_sections",
    "find_banned_words",
    "render_evidence_report",
    "render_forecast_accuracy_section",
    "render_privacy_section",
    "render_reproducibility_section",
    "require_no_banned_words",
]

FLX_8030 = register(
    "FLX-8030",
    "generated evidence prose contains a reserved word",
    "EVIDENCE.md is rendered from a fixed template, but a caller-supplied "
    "value (a bundle name, a conformance summary) can still introduce a "
    "word this project reserves (the list is in flux_engine/_vocabulary.py). "
    "The report is refused rather than written with the word inside it.",
)

#: The eight sections of ``EVIDENCE.md``, in the fixed order they always
#: appear. A reader who has seen one evidence pack already knows where to
#: look in the next one.
EVIDENCE_SECTIONS: Tuple[str, ...] = (
    "What This Bundle Is",
    "Conformance",
    "Reproducibility",
    "Fidelity",
    "Population",
    "Privacy Grade",
    "Column Provenance",
    "What Was Not Proven",
)

#: The section a run gains when the outcome-accuracy store holds a forecast registered for its bundle.
FORECAST_ACCURACY_SECTION = "Forecast Accuracy"


def evidence_sections(*, forecast_accuracy: bool = False) -> Tuple[str, ...]:
    """The sections of ``EVIDENCE.md``, in order: the eight, and "Forecast Accuracy" after "Fidelity".

    The ninth is there only when *forecast_accuracy* says the run has forecasts to report, so a
    run with none has the eight it always had.
    """
    if not forecast_accuracy:
        return EVIDENCE_SECTIONS
    after = EVIDENCE_SECTIONS.index("Fidelity") + 1
    return EVIDENCE_SECTIONS[:after] + (FORECAST_ACCURACY_SECTION,) + EVIDENCE_SECTIONS[after:]


#: v0.1 names exactly one authored dial in the mandatory "What Was Not
#: Proven" section -- the one the design names. A later
#: increment with a real calibration loop grows this list; nothing here
#: shrinks it silently.
DEFAULT_AUTHORED_DIALS: Tuple[str, ...] = ("temperature.campaignResponse",)

#: The reserved-word list. Re-exported from
#: :mod:`flux_engine._vocabulary`, which is the single definition shared
#: with ``scripts/register_lint.py``: the list used to be hand-copied into
#: both gates plus three test modules, with nothing asserting they were
#: equal, so a word added to one left the others blind to it.
_BANNED_WORDS: Tuple[str, ...] = BANNED_WORDS


def find_banned_words(text: str) -> Tuple[str, ...]:
    """Return every reserved word found in *text*, lower-cased, sorted, deduplicated.

    Thin wrapper over :func:`flux_engine._vocabulary.find_banned_words`,
    kept because this module is where the runtime refusal lives: unlike
    ``README.md`` or ``docs/``, ``EVIDENCE.md`` has no file on disk for a
    static scan to find until a bundle produces one, so the check has to
    run in-process, on the rendered string, before it is written.
    Deliberately does *not* mask dotted module paths -- generated
    customer-facing prose has no business naming an internal module.
    """
    return _find_banned_words(text)


def require_no_banned_words(text: str, *, label: str = "EVIDENCE.md") -> None:
    """Raise ``FLX-8030`` when *text* contains a reserved word."""
    hits = find_banned_words(text)
    if hits:
        raise FluxError(FLX_8030, f"{label} contains reserved vocabulary: {', '.join(hits)}")


#: One emit whose plan holds conditions: its ``exposeId`` and each group in order, as its number of
#: rows (``None`` when the run does not say) and the names of the columns it fixes. No value.
ConditionedEmit = Tuple[str, Sequence[Tuple[Optional[int], Sequence[str]]]]


def render_conditions_note(conditioned: Sequence[ConditionedEmit]) -> Optional[str]:
    """What "What Was Not Proven" says of each conditioned emit, or ``None`` when there is none.

    One entry per emit: its ``exposeId``, each group in order with its row count and the names of
    the columns it fixes, then one sentence on what a fixed value is. A fixed value is never
    printed: the report holds the names of the columns and nothing a group fixes them to.
    """
    if not conditioned:
        return None
    entries = []
    for expose_id, groups in conditioned:
        parts = []
        for number, (rows, columns) in enumerate(groups, start=1):
            counted = "its rows" if rows is None else f"{rows} rows"
            names = ", ".join(_cell(name, "a column") for name in columns)
            parts.append(f"group {number} holds {counted} and fixes {names}")
        entries.append(f"Conditioned emit {_cell(expose_id, 'an emit')}: " + "; ".join(parts) + ".")
    entries.append(
        "The values a group fixes are declarations of the bundle: no statistic stands behind "
        "them and no privacy grade covers them. They are not printed here."
    )
    return "\n\n".join(entries)


#: One emit whose plan holds a copula: its ``exposeId``, its columns in draw order, its measure and
#: its shrinkage ``alpha`` as the plan states it.
CopulaEmit = Tuple[str, Sequence[str], str, str]


def render_copula_note(copulas: Sequence[CopulaEmit]) -> Optional[str]:
    """What "Fidelity" says of each emit drawn through a copula, or ``None`` when there is none.

    The columns, the measure and the shrinkage. A shrunk matrix is said to be: every latent
    correlation was multiplied by ``1 - alpha``, so the columns are less correlated than declared.
    """
    if not copulas:
        return None
    entries = []
    for expose_id, columns, measure, alpha in copulas:
        names = ", ".join(_cell(name, "a column") for name in columns)
        text = (
            f"Copula of emit {_cell(expose_id, 'an emit')}: {names} are drawn together through a "
            f"Gaussian copula of the declared {_number_word(measure)} rank correlations."
        )
        if _number(alpha) != "0":
            text += (
                f" The declared matrix was shrunk (alpha {_number(alpha)}): every correlation was "
                "multiplied by one minus alpha on the latent scale, so the columns are less correlated "
                "than declared."
            )
        else:
            text += " The declared matrix was valid as written (alpha 0)."
        entries.append(text)
    return "\n\n".join(entries)


def _number_word(measure: str) -> str:
    return {"kendall": "Kendall", "spearman": "Spearman"}.get(measure, "rank")


_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _number(text: Optional[str]) -> str:
    """A number the store holds, as written, or a placeholder for text that is not one."""
    return text if text is not None and _NUMBER.fullmatch(text) else "not a number"


def _grade_label(pattern: Pattern) -> str:
    return f"{pattern.value} ({pattern.label})"


def _drift_cell(drift: Optional[Any]) -> str:
    """What a verdict recorded of drift: the metric, the largest value, its statistic and whether stale."""
    if not isinstance(drift, dict):
        return "none"
    metric = _cell(str(drift.get("metric")), "a metric")
    largest = drift.get("largest")
    if largest is None:
        return f"{metric}: no statistic compared"
    stale = "stale" if drift.get("stale") else "not stale"
    statistic = _cell(str(drift.get("statistic")), "a statistic")
    return f"{metric} {_number(str(largest))} at {statistic}, {stale}"


def _forecast_row_cells(position: int, row: ForecastRow) -> List[str]:
    """The cells of one forecast's row: pending, not scored, or scored, a miss as a hit."""
    state = _cell(row.state_digest, "a state") if row.state_digest is not None else "not stated"
    interval = "no interval"
    if row.interval_low is not None and row.interval_high is not None:
        interval = f"[{_number(row.interval_low)}, {_number(row.interval_high)}]"
    head = [
        _cell(row.prediction_id, f"forecast {position}"),
        _cell(row.metric, "a metric"),
        state,
        _cell(row.registered_at, "an instant"),
        f"{_number(row.forecast)} {interval}",
    ]
    if row.status == PENDING:
        return head + ["pending", "pending", "pending", "pending", "pending"]
    drift = _drift_cell(row.drift)
    if row.status == NOT_SCORED:
        why = _cell(row.reason, "a reason") if row.reason else "no reason stated"
        return head + [f"not scored: {why}", "not scored", "n/a", "n/a", drift]
    return head + [
        f"{_number(row.observed)} ({row.sample_size} records)",
        "inside" if row.inside else "outside",
        _number(row.interval_score),
        _number(row.absolute_error),
        drift,
    ]


def render_forecast_accuracy_section(accuracy: ForecastAccuracy) -> str:
    """The body of "Forecast Accuracy": every forecast of the run, pending or scored, and the totals.

    The forecasts registered for the run's ``inputDigest``, ordered by id, each with its metric, the
    digest of its state, its registration time, the forecast and its interval, the observation,
    inside or outside, the interval score, the absolute error and the drift. A forecast with no
    verdict is listed as pending, and a miss is listed as a hit is. For each state digest listed, the
    forecasts the store holds on that state across all bundles. The totals: scored, inside, the
    measured coverage and the Playback's declared ``scorecard.intervalCoverage``. The grade of each
    profile the forecasts read. Every name a profile or a bundle supplies goes through ``_cell``, so
    none can end a table cell or carry a reserved word into the report.
    """
    rows = [
        "| Id | Metric | State | Registered | Forecast and interval | Observation | Result "
        "| Interval score | Absolute error | Drift |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for position, row in enumerate(accuracy.rows, start=1):
        rows.append("| " + " | ".join(_forecast_row_cells(position, row)) + " |")
    here: Dict[str, int] = {}
    for row in accuracy.rows:
        if row.state_digest is not None:
            here[row.state_digest] = here.get(row.state_digest, 0) + 1
    counts = [
        f"- State {_cell(digest, 'a state')}: {total} forecasts in the store across all bundles, "
        f"{here.get(digest, 0)} of them for this run."
        for digest, total in sorted(accuracy.state_counts.items())
    ]
    pending = sum(1 for row in accuracy.rows if row.status == PENDING)
    unscored = sum(1 for row in accuracy.rows if row.status == NOT_SCORED)
    coverage = (
        f"{accuracy.coverage} ({accuracy.inside} of {accuracy.scored} scored forecasts)"
        if accuracy.coverage is not None
        else "not measured, no forecast has been scored"
    )
    declared = (
        f"{_number(accuracy.declared_coverage)}"
        + (
            f", from the Playback {_cell(accuracy.playback, 'of the bundle')}"
            if accuracy.playback is not None
            else ""
        )
        if accuracy.declared_coverage is not None
        else "none declared"
    )
    totals = [
        f"- Registered for this run: {len(accuracy.rows)}. Scored: {accuracy.scored}. "
        f"Not scored: {unscored}. Pending: {pending}.",
        f"- Inside their interval: {accuracy.inside} of {accuracy.scored}.",
        f"- Measured interval coverage: {coverage}.",
        f"- Declared interval coverage: {declared}. The engine never rewrites the declared figure.",
    ]
    grades = [
        f"- Pinned state {_cell(digest, 'a state')}: "
        + (
            f"**{_grade_label(accuracy.pinned_grades[digest])}**"
            if digest in accuracy.pinned_grades
            else "not graded"
        )
        for digest in sorted(accuracy.state_counts)
    ] + [
        f"- Next state {_cell(digest, 'a state')}: **{_grade_label(grade)}**"
        for digest, grade in sorted(accuracy.next_grades.items())
    ]
    return "\n\n".join(
        [
            "Every forecast registered for this run's input digest is listed, ordered by id, "
            "whether it was scored, missed or is still pending. Nothing is filtered: a miss is "
            "listed as a hit is. An observation is a cell the next state's profile released.",
            "\n".join(rows),
            "Forecasts the store holds on each state listed above:\n" + "\n".join(counts),
            "Totals:\n" + "\n".join(totals),
            "Grade of each profile the forecasts read, under the rule that grades a profile and "
            "never a declaration, a family or a forecast:\n" + "\n".join(grades),
        ]
    )


def render_weakest_grade_note(
    privacy: Optional[RunGrade],
    accuracy: Optional[ForecastAccuracy] = None,
    held_out_grade: Optional[Pattern] = None,
) -> str:
    """The sentence "Privacy Grade" adds when a run read more than one profile: the weakest grade of them all.

    The profiles are the pinned one, each next state the forecasts were scored against (*accuracy*) and
    the held-out profile of the consumer queries (*held_out_grade*). An empty string when only the
    pinned profile was read, so such a report keeps its bytes.
    """
    if accuracy is None and held_out_grade is None:
        return ""
    grades = list(accuracy.all_grades) if accuracy is not None else []
    if held_out_grade is not None:
        grades.append(held_out_grade)
    if privacy is not None:
        grades.append(privacy.pattern)
    if accuracy is not None and held_out_grade is not None:
        covers = (
            "the pinned state, each next state its forecasts were scored against, and the "
            "held-out profile of the consumer queries"
        )
    elif accuracy is not None:
        covers = "the pinned state and each next state its forecasts were scored against"
    else:
        covers = "the pinned profile and the held-out profile of the consumer queries"
    return (
        f"Weakest grade of every profile this run read: **{_grade_label(weakest(grades))}**. "
        f"It covers {covers}."
    )


def _render_not_proven_section(
    validity: Validity,
    authored_dials: Sequence[str],
    uncovered: str,
    scorecard_note: Optional[str] = None,
    register_note: Optional[str] = None,
    not_generated_note: Optional[str] = None,
    privacy: Optional[RunGrade] = None,
    conditions_note: Optional[str] = None,
) -> str:
    """Render the mandatory last section from its fixed template.

    Never hand-edited per call site: the *content* is fixed except for the
    dial list, which is data (the set of dials this bundle declares rather
    than fits), and the sentence about columns that fell back to a type
    default (:func:`flux_engine.evidence.provenance.render_uncovered_columns`),
    which is data too, not prose a caller chooses.
    """
    dial_list = ", ".join(f"`{dial}`" for dial in authored_dials) if authored_dials else "none"
    stated = privacy is not None and privacy.pattern in (Pattern.P3, Pattern.P4)
    lines = [
        (
            "Beyond the guarantee stated under Privacy Grade, this bundle asserts no "
            "privacy or re-identification claim about any source data"
            if stated
            else "This bundle asserts no privacy or re-identification claim about any "
            "source data"
        )
        + ", no fairness claim about any source system, and no claim "
        "about any system trained or tested using its output.",
    ]
    if validity is Validity.NOT_ASSERTED:
        lines.append(
            # "validity: not asserted" is the exact half of the headline
            # `fluxsim verify` prints, so a reader can reproduce this line
            # with a command rather than reading the source. The symbol it
            # comes from is flux_engine.api.verdict.Validity.NOT_ASSERTED;
            # that name belongs in this comment, not in a document handed
            # to someone with no access to the source tree.
            "No validity claim is made (validity: not asserted): this run's "
            "predicted outcomes have not been measured against the world. "
            f"In particular, the following are authored dials -- values this "
            f"bundle's author declared, not values this engine fitted or measured: "
            f"{dial_list}."
        )
    elif validity is Validity.SUPPORTED:
        lines.append(
            "A validity claim is made for the outcome named in claims[] below, on "
            "the basis recorded there. Every other predicted outcome this bundle "
            "declares -- authored dials among them -- remains unmeasured against "
            f"the world: {dial_list}."
        )
    else:
        lines.append(
            "A validity claim was evaluated and did not hold for the outcome "
            f"named in claims[] below. Authored dials remain unmeasured against "
            f"the world regardless: {dial_list}."
        )
    lines.append(uncovered)
    if not_generated_note is not None:
        lines.append(not_generated_note)
    if conditions_note is not None:
        lines.append(conditions_note)
    if register_note is not None:
        lines.append(register_note)
    if scorecard_note is not None:
        lines.append(scorecard_note)
    return "\n\n".join(lines)


_PATTERNS_NOTE = (
    "Patterns: **P0 assumed**, no profile was pinned and every parameter is declared by hand; "
    "**P1 checked**, a profile of aggregates under the profiler's output checks, with no formal "
    "guarantee; **P3 differentially private marginals**, a profile that states one release under "
    "differential privacy and holds only noisy statistics; **P4 differentially private model**, a "
    "model from a generator plugin whose bundle declares the account of its training; **P5 learned "
    "model, no privacy account**, a model from a generator plugin with no such account, the weakest "
    "pattern. P2 is not assigned, and the numbers are stable. The grade is the weakest of the pinned "
    "profile's and every declared generator plugin's, and is one grade for the run; a grade for "
    "each column is a later refinement."
)

_P0 = (
    "Declared hand-written: no profile was pinned, so the engine read no data; it cannot confirm "
    "that no data shaped the numbers a person typed. No privacy guarantee is stated."
)
_P1 = (
    "No formal guarantee. The profile this run pins is not a wholly noisy release, for the reasons "
    "listed below, and a statistic in it released under output-checking rules (small cells "
    "suppressed, counts rounded) is a check and carries no formal guarantee."
)
_P3 = (
    "Formal guarantee, as stated by the profile's release. The engine checks that the stated shares "
    "add up, not that noise was applied."
)
_P4 = "Formal guarantee, as the bundle declares it for each generator plugin below."
_P5 = (
    "No privacy account. A generator plugin below is a learned model whose bundle declares none, so "
    "its privacy rests on empirical checks alone, and no formal guarantee is stated."
)


def _cell(name: str, fallback: str) -> str:
    """A name for a cell of a Markdown table: quoted, with the pipes that would end the cell escaped.

    :func:`~flux_engine.evidence.provenance.quoted_name` withholds a name that is not plain text, a
    backtick or a reserved word, and lets a ``|`` through: in a table row it would end the cell
    and start another, so a contract could forge the cells of a row. It is written ``\\|``.
    """
    return quoted_name(name, fallback).replace("|", "\\|")


def _accounting_lines(accounting: Any) -> List[str]:
    """The release's account, in the terms NIST SP 800-226 asks a guarantee to be stated in."""
    total = accounting.total
    loss = (
        f"rho = {total.get('rho')}"
        if accounting.measure == "zeroConcentratedDP"
        else f"epsilon = {total.get('epsilon')}, delta = {total.get('delta')}"
    )
    spent = ", ".join(f"{key} {value}" for key, value in accounting.spent.items())
    return [
        f"- Definition: {accounting.definition}.",
        *_unit_lines(accounting),
        f"- Privacy loss, in total: {loss}.",
        f"- Accounting: {accounting.composition}; {accounting.parts} noisy "
        f"part{'s' if accounting.parts != 1 else ''} spend {spent or 'nothing'}, within the total.",
    ]


def _unit_lines(accounting: Any) -> List[str]:
    """The unit of privacy, with what it follows, the tables it reaches and the tables it treats as fixed.

    A profile that names format 1-4-0 states the unit with what it follows (ADR 0037): for a unit
    that follows relationships, the rows of each followed table one row of the expose takes with it,
    and in every case the tables whose rows the unit does not reach and the guarantee therefore
    treats as fixed. An earlier profile keeps the one line it always had.
    """
    expose = _cell(accounting.unit, "the expose")
    if not accounting.from_1_4:
        return [
            f"- Unit of privacy: one row of {expose}; neighbouring "
            "data sets differ by adding or removing one row of it."
        ]
    if not accounting.follows:
        lines = [
            f"- Unit of privacy: one row of {expose}; neighbouring data sets differ by adding or "
            "removing one row of it."
        ]
    else:
        followed = ", ".join(
            f"at most {cap} row{'s' if cap != 1 else ''} of "
            f"{_cell(child, 'a table')} for each row of {_cell(parent, 'a table')}"
            for parent, child, cap in accounting.follows
        )
        lines = [
            f"- Unit of privacy: one row of {expose} together with the rows that follow it, "
            f"{followed}; neighbouring data sets differ by adding or removing one such unit."
        ]
    reached = [(table, rows) for table, rows in accounting.reached if table != accounting.unit]
    if reached:
        shown = ", ".join(
            f"{_cell(table, 'a table')} ({rows} row{'s' if rows != 1 else ''})"
            for table, rows in reached
        )
        lines.append(
            f"- Tables the unit reaches besides {expose}, with the most rows one unit contributes: "
            f"{shown}."
        )
    if accounting.fixed:
        shown = ", ".join(_cell(table, "a table") for table in accounting.fixed)
        lines.append(
            f"- Tables the unit does not reach: {shown}. The guarantee treats their rows as fixed, "
            "the same in every neighbouring data set, so it says nothing about them; a column of "
            "such a table that carries a sensitivity label counts as P1 for its minimum grade."
        )
    return lines


_NOT_COVERED = (
    "The profile states, and the engine cannot check, that the mechanism it names made the numbers "
    "and that the levels of its histograms, their bin edges and the sampleSize bands were chosen "
    "without the data. Not covered: hand-written parameters (they are not data, and the guarantee "
    "says nothing about how they were chosen), any entity larger than the unit, any other release "
    "of the same data, and the profiler's own conduct."
)

#: The same for a profile that names format 1-4-0, whose declaration covers the structure of the
#: release more fully: what was chosen without the data, or is part of the release.
_NOT_COVERED_1_4 = (
    "The profile states, and the engine cannot check, that the mechanism it names made the numbers "
    "and that the levels and bin edges of its histograms and cliques, the bins of every "
    "groupByBins, the group keys of its grouped noisy statistics, its sampleSize bands, every "
    "rowFloor and every maxChildren were chosen without the data. Not covered: hand-written "
    "parameters (they are not data, and the guarantee says nothing about how they were chosen), any "
    "entity larger than the unit, any other release of the same data, and the profiler's own "
    "conduct."
)


#: What the profiler owes for a noisy history (format 1-5-0), stated by the profile and not
#: checked by the engine (ADR 0019 decision 4).
_OBLIGATIONS_NOISY_HISTORY = (
    "Noisy history, stated and not checked: every version of an entity references one row of "
    "the parent of its table's listed relationship, so an entity lies in one unit; the "
    "maxChildren bounding of the table keeps or drops an entity's versions together, by a rule "
    "that reads only that parent's rows; and publicStructure also covers the bins of "
    "versionsPerEntity, firstStart and versionLength, the levels of initial and transitions, "
    "and the candidate columns."
)

#: The same for a noisy query (format 1-5-0, PROFILE-SPEC.md "A noisy query").
_OBLIGATIONS_NOISY_QUERY = (
    "Noisy query, stated and not checked: its records are what its unit states; the query and "
    "its unit pass the checks of ADR 0028; whether a record matches reads only rows of that "
    "record's own unit and of tables the unit does not reach; under a history's entities, an "
    "entity's rows lie in one unit and the bounding of its table keeps or drops an entity's "
    "versions together; and a profiler that cannot run a query withholds every query of the "
    "release."
)


def _generator_lines(generators: Sequence[GeneratorGrade]) -> str:
    """Each generator plugin the bundle declares, with the grade it states and, for ``P4``, its account.

    The account is the bundle's, as it declares it: the unit of privacy, the privacy loss and the
    accounting. The engine checks its form, not how the model was trained.
    """
    lines = ["Generator plugins, as the bundle declares them:"]
    for position, item in enumerate(generators, start=1):
        head = (
            f"- {_cell(item.expose_id, f'expose {position}')}: plugin "
            f"{_cell(item.plugin, f'plugin {position}')} from "
            f"{_cell(item.distribution, f'distribution {position}')} "
            f"{_cell(item.version, f'version {position}')}, declared "
            f"{item.pattern.value} ({item.pattern.label})."
        )
        if item.pattern is Pattern.P4 and item.unit is not None:
            head += (
                f" Unit of privacy: {_cell(item.unit, 'withheld')}. "
                f"Privacy loss: {item.loss}. "
                f"Accounting: {_cell(item.accounting or '', 'withheld')}."
            )
        lines.append(head)
    lines.append(
        "The engine checks the account's form, not how the model was trained: it is the bundle's "
        "statement, which the plugin repeats."
    )
    return "\n".join(lines)


def _minimum(column: Any) -> str:
    """The minimum grade a column's sensitivity sets."""
    if column.minimum is None:
        return "any pattern"
    return f"{column.minimum.value} or stronger"


def render_privacy_section(grade: Optional[RunGrade]) -> str:
    """The body of ``EVIDENCE.md``'s "Privacy Grade" section.

    The run's grade, what it rests on, each generator plugin the bundle declares with the grade it
    states, and a table of each column's sensitivity and minimum and whether the run meets it. A run
    graded P3 states its guarantee in the terms NIST SP 800-226 asks for: the unit of privacy, the
    privacy loss (epsilon and delta, or rho) and the accounting, the definition and how the parts
    compose. A plugin graded P4 is stated in the same terms, as the bundle declares them. A run that is not P3 and whose profile carries such an
    account says why it is not.
    """
    if grade is None:
        return (
            "This run was not graded: the report was written with no profile to read, so it "
            "states no pattern and no guarantee. " + _PATTERNS_NOTE
        )
    profile = grade.profile
    pattern = grade.pattern
    blocks = [
        f"Run grade: **{pattern.value} ({pattern.label})**.",
        _PATTERNS_NOTE,
        {
            Pattern.P0: _P0,
            Pattern.P1: _P1,
            Pattern.P3: _P3,
            Pattern.P4: _P4,
            Pattern.P5: _P5,
        }[pattern],
    ]
    if pattern is Pattern.P1 and profile.reasons:
        blocks.append(
            "Why not P3:\n"
            + "\n".join(f"- {reason[0].upper()}{reason[1:]}." for reason in profile.reasons)
        )
    if profile.accounting is not None:
        head = (
            "The release the guarantee is for:"
            if pattern is Pattern.P3
            else "The pinned profile also carries the account of a release under differential "
            "privacy, which gives this run no guarantee at its grade:"
        )
        lines = [head, *_accounting_lines(profile.accounting)]
        if pattern is Pattern.P3:
            lines.append(f"- {_NOT_COVERED_1_4 if profile.accounting.from_1_4 else _NOT_COVERED}")
            if profile.accounting.from_1_5:
                if profile.accounting.noisy_history:
                    lines.append(f"- {_OBLIGATIONS_NOISY_HISTORY}")
                if profile.accounting.noisy_query:
                    lines.append(f"- {_OBLIGATIONS_NOISY_QUERY}")
        blocks.append("\n".join(lines))
    if grade.generators:
        blocks.append(_generator_lines(grade.generators))
    if grade.columns:
        rows = [
            "| Expose | Column | Sensitivity | Minimum | Met |",
            "|---|---|---|---|---|",
        ]
        for position, column in enumerate(grade.columns, start=1):
            rows.append(
                f"| {_cell(column.expose_id, 'an expose')} "
                f"| {_cell(column.column, f'column {position}')} "
                f"| {column.sensitivity or 'none'} | {_minimum(column)} "
                f"| {'yes' if column.met else 'NO, below its minimum'} |"
            )
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def render_reference_block(reference_exposes: Sequence[str]) -> str:
    """The paragraph that lists each reference expose as released whole (ADR 0023 section 2).

    Empty for a run with none, so such a run's "Privacy Grade" section is the one it always was.
    A reference expose holds the rows its profile publishes, with no noise on them, so the grade
    above does not cover it: the owner released it as non-personal reference data.
    """
    if not reference_exposes:
        return ""
    names = ", ".join(
        _cell(name, f"expose {position}") for position, name in enumerate(reference_exposes, 1)
    )
    return (
        f"Reference exposes, released whole and outside the guarantee above: {names}. Each holds "
        "the rows the pinned profile publishes for it, with no noise on them, because the owner "
        "released the table as non-personal reference data; no grade covers those rows."
    )


@dataclass(frozen=True)
class StoredGenerator:
    """One expose a generator plugin made (ADR 0032 section 6).

    The plugin, its distribution and version are the bundle's declaration, which ``plan.json`` may
    only agree with (:func:`flux_engine.stages.stored.stored_generators`). ``reproducibility`` is
    the one the run is recorded with: ``none`` for an editable install, whatever the bundle
    declares. ``environment_digest`` is the digest of the environment the engine measured when the
    plan ran, ``sha256:`` and 64 hex digits.
    """

    expose_id: str
    plugin: str
    distribution: str
    version: str
    editable: bool
    reproducibility: str
    environment_digest: str


#: What each recorded reproducibility means for a stored plugin run, in a replay's terms.
_REPRODUCIBILITY_MEANS = {
    "bytes": "the same values from the same request on any machine, so a replay generates them "
    "again and compares",
    "environment": "the same values only in the same environment, so a replay generates them "
    "again only where the environment digest is the recorded one",
    "none": "no promise that another generation gives the same values, so a replay reports the "
    "stored bytes as the run of record",
}


def render_reproducibility_section(
    reproducibility: Reproducibility, stored: Sequence[StoredGenerator] = ()
) -> str:
    """The body of ``EVIDENCE.md``'s "Reproducibility" section.

    ``reproducibility: <value>`` as it always was. For a run that has generator plugins, it says
    which exposes were stored and not regenerated, with the reproducibility each is recorded with,
    and, for an editable install, why that is ``none``.
    """
    head = f"reproducibility: {reproducibility.value}"
    if not stored:
        return head
    blocks = [
        head,
        "Stored, not regenerated: the exposes below were made by a generator plugin, once, when "
        "the run was planned, and the run holds those bytes as they were stored. Applying the run "
        "commits them and never asks the plugin for them again. The reproducibility recorded for "
        "each is what the engine can say of generating them again.",
    ]
    lines = []
    for position, item in enumerate(stored, start=1):
        line = (
            f"- {_cell(item.expose_id, f'expose {position}')}: plugin "
            f"{_cell(item.plugin, f'plugin {position}')} from "
            f"{_cell(item.distribution, f'distribution {position}')} "
            f"{_cell(item.version, f'version {position}')}, recorded "
            f"{_cell(item.reproducibility, 'an unknown reproducibility')} "
            f"({_REPRODUCIBILITY_MEANS.get(item.reproducibility, 'unknown')})."
        )
        if item.editable:
            line += (
                " The distribution is an editable install, so its code lies in a source tree that "
                "can change without a new version, and the engine records its run as `none` "
                "whatever the bundle declares."
            )
        lines.append(line)
    blocks.append("\n".join(lines))
    environments = sorted({item.environment_digest for item in stored if item.environment_digest})
    if environments:
        blocks.append(
            "Environment digest measured by the engine when the run was planned: "
            + ", ".join(_cell(digest, "an unreadable digest") for digest in environments)
            + "."
        )
    return "\n\n".join(blocks)


def render_evidence_report(
    *,
    bundle_name: str,
    kernel_version: str,
    run_digest: DigestRef,
    generated_at: str,
    conformance_summary: str,
    reproducibility: Reproducibility,
    fidelity_summary: str = "No fidelity check was run for this bundle.",
    coverage: Optional[Sequence[ExposeCoverage]] = None,
    validity: Validity = Validity.NOT_ASSERTED,
    authored_dials: Sequence[str] = DEFAULT_AUTHORED_DIALS,
    scorecard_note: Optional[str] = None,
    register_note: Optional[str] = None,
    not_generated: Sequence[NotGenerated] = (),
    privacy: Optional[RunGrade] = None,
    reference_exposes: Sequence[str] = (),
    population: Optional[Sequence[PopulationProperty]] = None,
    stored_generators: Sequence[StoredGenerator] = (),
    conditioned: Sequence[ConditionedEmit] = (),
    forecast_accuracy: Optional[ForecastAccuracy] = None,
    held_out_grade: Optional[Pattern] = None,
    copulas: Sequence[CopulaEmit] = (),
) -> str:
    """Render ``EVIDENCE.md``'s eight fixed sections for one bundle, and a ninth for forecasts.

    *stored_generators* is each expose a generator plugin made, as the bundle declares it and the
    plan recorded it (:func:`flux_engine.stages.stored.stored_generators`): "Reproducibility" says that its bytes were stored and not
    regenerated, and with what reproducibility. Empty, the default, adds nothing.

    *population* is the scored histogram properties with the cells their scores were computed
    over (:mod:`flux_engine.evidence.population`): drawn under "Population". ``None``, the
    default, says the run was shipped without a profile, and the section says so.

    *privacy* is the run's grade (:func:`flux_engine.evidence.grades.grade_profile`): the pattern its
    numbers rest on, per column and for the run, and the guarantee that pattern carries
    (:func:`render_privacy_section`). ``None``, the default, says the run was not graded, so a
    caller that has no register or profile to read still gets the section.

    *reference_exposes* names each expose of the run that is a reference expose, released whole
    (:func:`render_reference_block`); empty, the default, adds nothing.

    *coverage* is how each column of each expose was produced. It is the one
    input to two places that must agree, the "Column Provenance" section and
    the sentence about columns that fell back to a type default under "What Was
    Not Proven", so they are both rendered from it here. ``None``, the default,
    says the run recorded nothing to tell, in both.

    *scorecard_note* is what a run's scorecard does not show, added to "What
    Was Not Proven" when the run was scored against a pinned profile
    (docs/spec/SCORECARD-SPEC.md). ``None``, the default, adds nothing.

    *register_note* is the parameter register's paragraph: the shares of the
    run's numbers that rest on the profile, on assumptions and on defaults, and
    each assumption with its reason (docs/spec/PARAMETER-REGISTER-SPEC.md,
    :func:`flux_engine.evidence.register.render_register_note`), added to "What
    Was Not Proven" when the run's plan binds a register. ``None``, the default,
    adds nothing, so a run with no register gets the report it got before.

    *not_generated* is what the run's contract declares and the engine does not
    generate (:mod:`flux_engine.seam.declared`), worked out again from the bundle
    ``ship`` packs, whose digest the plan binds. It adds one sentence to "What Was Not Proven" naming each
    declaration; empty, the default, adds nothing, so a run that declares none gets
    the report it got before.

    *conditioned* is each emit of the run whose plan holds conditions
    (:func:`render_conditions_note`); empty, the default, adds nothing, so a run with
    no conditions gets the report it got before.

    *copulas* is each emit of the run whose plan holds a copula (:func:`render_copula_note`),
    stated under "Fidelity"; empty, the default, adds nothing.

    *forecast_accuracy* is the run's forecasts, scored or pending
    (:func:`flux_engine.evidence.forecast_accuracy.build_forecast_accuracy`). Given one, the
    report gains "Forecast Accuracy" after "Fidelity"
    (:func:`render_forecast_accuracy_section`), and "Privacy Grade" states the weakest grade of
    every profile the run read (:func:`render_weakest_grade_note`). ``None``, the default, adds
    neither, so a run with no forecast registered gets the report it got before.

    *held_out_grade* is the grade of the held-out profile ``ship --held-out`` read. Given one, "Privacy
    Grade" states the weakest grade of every profile the run read, with or without forecasts. ``None``,
    the default, adds nothing for it, so every other report keeps its bytes.

    Raises ``FLX-8030`` if the rendered text contains a reserved
    word -- most likely introduced through a caller-supplied
    string such as *bundle_name* or *conformance_summary*.
    """
    privacy_parts = [
        render_privacy_section(privacy),
        render_reference_block(reference_exposes),
    ]
    privacy_parts.append(render_weakest_grade_note(privacy, forecast_accuracy, held_out_grade))
    bodies = {
        "What This Bundle Is": (
            f"- Bundle: `{bundle_name}`\n"
            f"- Kernel version: `{kernel_version}`\n"
            f"- Run digest: `{run_digest.value}`\n"
            f"- Generated at: {generated_at}"
        ),
        "Conformance": conformance_summary,
        "Reproducibility": render_reproducibility_section(reproducibility, stored_generators),
        "Fidelity": "\n\n".join(
            part for part in (fidelity_summary, render_copula_note(copulas)) if part
        ),
        FORECAST_ACCURACY_SECTION: (
            render_forecast_accuracy_section(forecast_accuracy)
            if forecast_accuracy is not None
            else ""
        ),
        "Population": render_population_section(population),
        "Privacy Grade": "\n\n".join(part for part in privacy_parts if part),
        "Column Provenance": render_provenance_section(coverage),
        "What Was Not Proven": _render_not_proven_section(
            validity,
            authored_dials,
            render_uncovered_columns(coverage),
            scorecard_note,
            register_note,
            render_not_generated_sentence(not_generated),
            privacy=privacy,
            conditions_note=render_conditions_note(conditioned),
        ),
    }
    sections = [f"# Evidence: {bundle_name}"] + [
        f"## {title}\n\n{bodies[title]}"
        for title in evidence_sections(forecast_accuracy=forecast_accuracy is not None)
    ]
    report = "\n\n".join(sections) + "\n"
    require_no_banned_words(report)
    return report


def build_claims(
    *,
    conformance_statement: str,
    conformance_evidence: Sequence[DigestRef],
    reproducibility: Reproducibility,
    reproducibility_evidence: Sequence[DigestRef] = (),
    verifier: str = "flux-engine",
    fidelity_claims: Sequence[Claim] = (),
    promoted_verdict: Optional[AccuracyVerdict] = None,
) -> Tuple[Claim, ...]:
    """Assemble the ``claims[]`` an evidence pack carries alongside ``EVIDENCE.md``.

    A validity claim is appended only when *promoted_verdict* is given --
    see :func:`flux_engine.accuracy.policy.promote`, which decides whether
    a verdict is allowed to reach here at all. Nothing here promotes a
    verdict itself; this function only renders one that already was.
    """
    claims = [
        Claim(
            claim_type=ClaimType.CONFORMANCE,
            statement=conformance_statement,
            basis=ClaimBasis.NONE,
            evidence=tuple(conformance_evidence),
            verifier=verifier,
        ),
        Claim(
            claim_type=ClaimType.REPRODUCIBILITY,
            statement=f"reproducibility: {reproducibility.value}",
            basis=ClaimBasis.NONE,
            evidence=tuple(reproducibility_evidence),
            verifier=verifier,
        ),
    ]
    claims.extend(fidelity_claims)
    if promoted_verdict is not None:
        claims.append(
            Claim(
                claim_type=ClaimType.VALIDITY,
                statement=(
                    f"metric {promoted_verdict.metric_id} outcome "
                    f"{promoted_verdict.outcome.value}"
                ),
                basis=promoted_verdict.basis,
                evidence=promoted_verdict.evidence,
                verifier=verifier,
            )
        )
    return tuple(claims)
