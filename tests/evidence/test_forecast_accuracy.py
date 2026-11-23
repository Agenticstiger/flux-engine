# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The "Forecast Accuracy" section and ``forecast-accuracy.json`` (ADR 0029 section 8).

The rows are built from a store written by hand here, so every shape a verdict can take is covered
without running a forecast: pending, scored inside, scored outside, not scored. Pinned: the rows
are the forecasts registered for the run's ``inputDigest`` and no other, ordered by id; a miss is
listed as a hit is; the state counts span every bundle; the totals; the grades, and the weakest of
every profile the run read; that the section sits after "Fidelity" and exists only when the run has a
forecast; and that a name from a profile or a bundle can neither end a table cell nor carry a reserved
word into the report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from flux_engine._vocabulary import BANNED_WORDS
from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
from flux_engine.api.claims import ClaimBasis
from flux_engine.api.verdict import Reproducibility
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.core.grades import Pattern
from flux_engine.evidence.forecast_accuracy import (
    FORECAST_ACCURACY_FILE,
    build_forecast_accuracy,
    forecast_accuracy_bytes,
    forecast_accuracy_document,
)
from flux_engine.evidence.grades import ProfileGrade, RunGrade
from flux_engine.evidence.report import (
    EVIDENCE_SECTIONS,
    evidence_sections,
    render_evidence_report,
    render_forecast_accuracy_section,
    render_weakest_grade_note,
    require_no_banned_words,
)

pytestmark = pytest.mark.unit

RUN_DIGEST = DigestRef(
    value="sha256:" + "aa" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)
MINE = "sha256:" + "1" * 64
OTHER = "sha256:" + "2" * 64
STATE_A = "sha256:" + "a" * 64
STATE_B = "sha256:" + "b" * 64
NEXT_A = "sha256:" + "c" * 64
NEXT_B = "sha256:" + "d" * 64


def _register(
    store: AccuracyStore,
    prediction_id: str,
    *,
    input_digest: str = MINE,
    state_digest: str = STATE_A,
    metric: str = "query:renewed/premium",
    predicted: str = "0.5",
    low: str = "0.4",
    high: str = "0.6",
    registered_at: str = "2026-07-01T00:00:00Z",
) -> None:
    document: Dict[str, Any] = {
        "prediction_id": prediction_id,
        "run_digest": "sha256:" + "9" * 64,
        "metric_id": metric,
        "statement": "A forecast.",
        "predicted": predicted,
        "interval_low": low,
        "interval_high": high,
        "horizon_end": "2026-09-30T00:00:00Z",
        "registered_at": registered_at,
        "stateDigest": state_digest,
        "inputDigest": input_digest,
    }
    store.register(prediction_from_document(document), document)


def _verdict(
    store: AccuracyStore,
    prediction_id: str,
    outcome: AccuracyOutcome,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    verdict = AccuracyVerdict(
        prediction_id=prediction_id,
        metric_id="query:renewed/premium",
        outcome=outcome,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
    )
    store.write_verdict(verdict, extra)


def _scored(next_digest: str = NEXT_A, **extra: Any) -> Dict[str, Any]:
    return {
        "observed": "0.55",
        "sampleSize": 200,
        "absoluteError": "0.05",
        "intervalScore": "0.2",
        "nextStateDigest": next_digest,
        "grades": {"pinned": "P1", "next": "P1"},
        **extra,
    }


@pytest.fixture()
def store(tmp_path: Path) -> AccuracyStore:
    return AccuracyStore.for_state_dir(tmp_path / "state")


# ------------------------------------------------------------------ which forecasts, in what order


def test_a_store_with_no_forecast_for_the_run_gives_nothing(store: AccuracyStore) -> None:
    assert build_forecast_accuracy(store, MINE) is None
    _register(store, "elsewhere", input_digest=OTHER)
    assert build_forecast_accuracy(store, MINE) is None


def test_a_record_with_no_input_digest_is_no_forecast_for_a_run(store: AccuracyStore) -> None:
    document: Dict[str, Any] = {
        "prediction_id": "plain",
        "run_digest": "sha256:" + "9" * 64,
        "metric_id": "m",
        "statement": "s",
        "predicted": "1",
        "horizon_end": "2026-09-30T00:00:00Z",
        "registered_at": "2026-07-01T00:00:00Z",
    }
    store.register(prediction_from_document(document), document)
    assert build_forecast_accuracy(store, MINE) is None


def test_the_rows_are_the_runs_forecasts_ordered_by_id(store: AccuracyStore) -> None:
    for name in ("zeta.s", "alpha.s", "mid.s"):
        _register(store, name)
    _register(store, "other.s", input_digest=OTHER)
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert [row.prediction_id for row in accuracy.rows] == ["alpha.s", "mid.s", "zeta.s"]


def test_a_forecast_with_no_verdict_is_pending(store: AccuracyStore) -> None:
    _register(store, "p.s")
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    (row,) = accuracy.rows
    assert row.status == "pending"
    assert row.observed is None and row.inside is None and row.interval_score is None
    assert (accuracy.scored, accuracy.inside, accuracy.coverage) == (0, 0, None)


def test_a_miss_is_listed_as_a_hit_is(store: AccuracyStore) -> None:
    _register(store, "hit.s")
    _register(store, "miss.s")
    _verdict(store, "hit.s", AccuracyOutcome.WITHIN_INTERVAL, extra=_scored())
    _verdict(
        store,
        "miss.s",
        AccuracyOutcome.OUTSIDE_INTERVAL,
        extra=_scored(observed="0.9", absoluteError="0.4", intervalScore="2.2"),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    rows = {row.prediction_id: row for row in accuracy.rows}
    assert rows["hit.s"].inside is True and rows["hit.s"].status == "scored"
    assert rows["miss.s"].inside is False and rows["miss.s"].status == "scored"
    assert rows["miss.s"].observed == "0.9" and rows["miss.s"].interval_score == "2.2"
    assert rows["miss.s"].absolute_error == "0.4" and rows["miss.s"].sample_size == 200


def test_a_forecast_not_scored_keeps_its_reason_and_counts_as_neither(
    store: AccuracyStore,
) -> None:
    _register(store, "w.s")
    _verdict(
        store,
        "w.s",
        AccuracyOutcome.NOT_SCORED,
        extra={"reason": "the cell is withheld", "nextStateDigest": NEXT_A},
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    (row,) = accuracy.rows
    assert row.status == "not-scored" and row.reason == "the cell is withheld"
    assert row.observed is None and row.inside is None
    assert (accuracy.scored, accuracy.inside) == (0, 0)


def test_the_totals_and_the_measured_coverage(store: AccuracyStore) -> None:
    for name in ("a.s", "b.s", "c.s", "d.s"):
        _register(store, name)
    _verdict(store, "a.s", AccuracyOutcome.WITHIN_INTERVAL, extra=_scored())
    _verdict(store, "b.s", AccuracyOutcome.WITHIN_INTERVAL, extra=_scored())
    _verdict(store, "c.s", AccuracyOutcome.OUTSIDE_INTERVAL, extra=_scored(observed="0.9"))
    accuracy = build_forecast_accuracy(store, MINE, declared_coverage="0.92", playback="pb")
    assert accuracy is not None
    assert (accuracy.scored, accuracy.inside) == (3, 2)
    assert accuracy.coverage == "0.6666666666666666666666666667"
    assert accuracy.declared_coverage == "0.92" and accuracy.playback == "pb"


def test_a_whole_coverage_is_written_without_a_trailing_fraction(store: AccuracyStore) -> None:
    _register(store, "a.s")
    _verdict(store, "a.s", AccuracyOutcome.WITHIN_INTERVAL, extra=_scored())
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None and accuracy.coverage == "1"


# ------------------------------------------------------------------ the states


def test_a_state_is_counted_across_every_bundle_of_the_store(store: AccuracyStore) -> None:
    _register(store, "mine-1.a")
    _register(store, "mine-2.a")
    _register(store, "theirs.a", input_digest=OTHER)
    _register(store, "theirs-b.b", input_digest=OTHER, state_digest=STATE_B)
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert dict(accuracy.state_counts) == {STATE_A: 3}


def test_each_state_the_rows_name_is_listed(store: AccuracyStore) -> None:
    _register(store, "a.s", state_digest=STATE_A)
    _register(store, "b.s", state_digest=STATE_B)
    _register(store, "c.s", input_digest=OTHER, state_digest=STATE_B)
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert dict(accuracy.state_counts) == {STATE_A: 1, STATE_B: 2}


# ------------------------------------------------------------------ grades


def test_the_grades_are_the_ones_the_verdicts_state(store: AccuracyStore) -> None:
    _register(store, "a.s")
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "P3", "next": "P1"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert dict(accuracy.pinned_grades) == {STATE_A: Pattern.P3}
    assert dict(accuracy.next_grades) == {NEXT_A: Pattern.P1}
    assert accuracy.weakest_grade is Pattern.P1


def test_two_accounts_of_one_profile_give_the_weaker(store: AccuracyStore) -> None:
    _register(store, "a.s")
    _register(store, "b.s")
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "P3", "next": "P3"}),
    )
    _verdict(
        store,
        "b.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "P1", "next": "P3"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert dict(accuracy.pinned_grades) == {STATE_A: Pattern.P1}
    assert dict(accuracy.next_grades) == {NEXT_A: Pattern.P3}


def test_the_runs_own_grade_stands_for_the_state_it_pins(store: AccuracyStore) -> None:
    _register(store, "pending.s")
    accuracy = build_forecast_accuracy(store, MINE, pinned=(STATE_A, Pattern.P3))
    assert accuracy is not None
    assert dict(accuracy.pinned_grades) == {STATE_A: Pattern.P3}
    assert accuracy.weakest_grade is Pattern.P3


def test_a_grade_that_is_not_a_pattern_is_ignored(store: AccuracyStore) -> None:
    _register(store, "a.s")
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "Z9", "next": "P1"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert dict(accuracy.pinned_grades) == {} and dict(accuracy.next_grades) == {NEXT_A: Pattern.P1}


def test_the_weakest_grade_is_the_weakest_of_every_profile_read(store: AccuracyStore) -> None:
    _register(store, "a.s", state_digest=STATE_A)
    _register(store, "b.s", state_digest=STATE_B)
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(NEXT_A, grades={"pinned": "P3", "next": "P3"}),
    )
    _verdict(
        store,
        "b.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(NEXT_B, grades={"pinned": "P3", "next": "P1"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    assert accuracy.weakest_grade is Pattern.P1
    run = RunGrade(profile=ProfileGrade(Pattern.P3, pinned=True))
    # A run graded P3 whose forecasts read a P1 profile is stated at P1.
    note = render_weakest_grade_note(run, accuracy)
    assert "**P1 (checked)**" in note


def test_a_run_graded_weaker_than_its_forecast_profiles_is_stated_at_its_own_grade(
    store: AccuracyStore,
) -> None:
    _register(store, "a.s")
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "P3", "next": "P3"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    run = RunGrade(profile=ProfileGrade(Pattern.P1, pinned=True))
    assert "**P1 (checked)**" in render_weakest_grade_note(run, accuracy)
    assert "**P3 (differentially private marginals)**" in render_weakest_grade_note(None, accuracy)


def test_the_weakest_grade_sentence_names_what_it_covers_in_each_case(store: AccuracyStore) -> None:
    _register(store, "a.s")
    _verdict(
        store,
        "a.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(grades={"pinned": "P3", "next": "P3"}),
    )
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    run = RunGrade(profile=ProfileGrade(Pattern.P3, pinned=True))
    stronger = "**P3 (differentially private marginals)**"
    # Forecasts and no held-out profile: the sentence a run with forecasts has always had.
    assert render_weakest_grade_note(run, accuracy) == (
        f"Weakest grade of every profile this run read: {stronger}. It covers the pinned state "
        "and each next state its forecasts were scored against."
    )
    # A held-out profile and no forecasts.
    assert render_weakest_grade_note(run, None, Pattern.P1) == (
        "Weakest grade of every profile this run read: **P1 (checked)**. It covers the pinned "
        "profile and the held-out profile of the consumer queries."
    )
    # Both: the held-out profile is the weakest of the three kinds.
    assert render_weakest_grade_note(run, accuracy, Pattern.P1) == (
        "Weakest grade of every profile this run read: **P1 (checked)**. It covers the pinned "
        "state, each next state its forecasts were scored against, and the held-out profile of "
        "the consumer queries."
    )
    # A held-out profile stronger than the rest does not lift the grade.
    weak = RunGrade(profile=ProfileGrade(Pattern.P1, pinned=True))
    assert "**P1 (checked)**" in render_weakest_grade_note(weak, None, Pattern.P3)


def test_one_profile_read_adds_no_sentence() -> None:
    run = RunGrade(profile=ProfileGrade(Pattern.P3, pinned=True))
    assert render_weakest_grade_note(run) == ""
    assert render_weakest_grade_note(None) == ""


# ------------------------------------------------------------------ the document


def _filled(store: AccuracyStore) -> Any:
    _register(store, "hit.s", metric="query:renewed/premium")
    _register(store, "miss.s", metric="query:renewed/basic")
    _register(store, "wait.s")
    _register(store, "gone.s")
    _verdict(
        store,
        "hit.s",
        AccuracyOutcome.WITHIN_INTERVAL,
        extra=_scored(
            drift={
                "metric": "psi",
                "threshold": "0.2",
                "largest": "0.03",
                "statistic": "query:renewed/premium",
                "stale": False,
            }
        ),
    )
    _verdict(
        store,
        "miss.s",
        AccuracyOutcome.OUTSIDE_INTERVAL,
        extra=_scored(observed="0.9", intervalScore="2.2", absoluteError="0.4"),
    )
    _verdict(
        store,
        "gone.s",
        AccuracyOutcome.NOT_SCORED,
        extra={"reason": "the cell is withheld", "nextStateDigest": NEXT_A},
    )
    accuracy = build_forecast_accuracy(
        store, MINE, declared_coverage="0.92", playback="pb-renewal", pinned=(STATE_A, Pattern.P1)
    )
    assert accuracy is not None
    return accuracy


def test_the_document_holds_the_rows_and_the_totals(store: AccuracyStore) -> None:
    document = forecast_accuracy_document(_filled(store))
    assert document["inputDigest"] == MINE
    assert [row["predictionId"] for row in document["rows"]] == [
        "gone.s",
        "hit.s",
        "miss.s",
        "wait.s",
    ]
    assert [row["status"] for row in document["rows"]] == [
        "not-scored",
        "scored",
        "scored",
        "pending",
    ]
    assert document["totals"] == {
        "registered": 4,
        "scored": 2,
        "inside": 1,
        "measuredCoverage": "0.5",
        "declaredCoverage": "0.92",
        "playback": "pb-renewal",
    }
    assert document["stateCounts"] == {STATE_A: 4}
    assert document["grades"] == {
        "pinned": {STATE_A: "P1"},
        "next": {NEXT_A: "P1"},
        "weakest": "P1",
    }
    hit = document["rows"][1]
    assert hit["inside"] is True and hit["drift"]["stale"] is False
    assert document["rows"][2]["inside"] is False
    assert document["rows"][3]["inside"] is None and document["rows"][3]["observed"] is None


def test_the_bytes_are_sorted_indented_and_end_with_a_newline(store: AccuracyStore) -> None:
    accuracy = _filled(store)
    data = forecast_accuracy_bytes(accuracy)
    assert data.endswith(b"}\n")
    assert json.loads(data) == forecast_accuracy_document(accuracy)
    assert data == forecast_accuracy_bytes(accuracy)
    assert FORECAST_ACCURACY_FILE == "forecast-accuracy.json"


# ------------------------------------------------------------------ the section


def test_the_section_lists_every_row_pending_scored_and_not_scored(store: AccuracyStore) -> None:
    text = render_forecast_accuracy_section(_filled(store))
    lines = [line for line in text.splitlines() if line.startswith("| `")]
    assert [line.split("|")[1].strip() for line in lines] == [
        "`gone.s`",
        "`hit.s`",
        "`miss.s`",
        "`wait.s`",
    ]
    by_id = {line.split("|")[1].strip(): line for line in lines}
    assert "not scored: `the cell is withheld`" in by_id["`gone.s`"]
    assert "0.55 (200 records) | inside | 0.2 | 0.05 |" in by_id["`hit.s`"]
    assert "`psi` 0.03 at `query:renewed/premium`, not stale" in by_id["`hit.s`"]
    assert "0.9 (200 records) | outside | 2.2 | 0.4 | none |" in by_id["`miss.s`"]
    assert "| pending | pending | pending | pending | pending |" in by_id["`wait.s`"]
    assert "0.5 [0.4, 0.6]" in by_id["`hit.s`"]


def test_the_section_states_the_state_counts_the_totals_and_the_grades(
    store: AccuracyStore,
) -> None:
    _register(store, "foreign.s", input_digest=OTHER)
    text = render_forecast_accuracy_section(_filled(store))
    assert (
        f"- State `{STATE_A}`: 5 forecasts in the store across all bundles, 4 of them for this run."
        in text
    )
    assert "- Registered for this run: 4. Scored: 2. Not scored: 1. Pending: 1." in text
    assert "- Inside their interval: 1 of 2." in text
    assert "- Measured interval coverage: 0.5 (1 of 2 scored forecasts)." in text
    assert "- Declared interval coverage: 0.92, from the Playback `pb-renewal`." in text
    assert f"- Pinned state `{STATE_A}`: **P1 (checked)**" in text
    assert f"- Next state `{NEXT_A}`: **P1 (checked)**" in text
    assert "Nothing is filtered: a miss is listed as a hit is." in text


def test_a_section_with_nothing_scored_says_the_coverage_is_not_measured(
    store: AccuracyStore,
) -> None:
    _register(store, "wait.s")
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    text = render_forecast_accuracy_section(accuracy)
    assert "Measured interval coverage: not measured, no forecast has been scored." in text
    assert "Declared interval coverage: none declared." in text
    assert "Inside their interval: 0 of 0." in text


def test_a_name_that_would_end_a_table_cell_or_carry_a_reserved_word_is_withheld(
    store: AccuracyStore,
) -> None:
    _register(store, "a.s", metric="query:a|b/premium")
    reserved = BANNED_WORDS[0]
    _register(store, "b.s", metric=f"query:{reserved}/premium")
    accuracy = build_forecast_accuracy(store, MINE)
    assert accuracy is not None
    text = render_forecast_accuracy_section(accuracy)
    assert "`query:a\\|b/premium`" in text
    assert reserved not in text and "a metric" in text
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert all(row.count(" | ") + row.count("\\|") >= 9 for row in rows)
    assert all(len(row.replace("\\|", "").split("|")) == 12 for row in rows)


# ------------------------------------------------------------------ the report


def _report(**overrides: Any) -> str:
    kwargs: Dict[str, Any] = dict(
        bundle_name="subscriptions",
        kernel_version="0.2.0",
        run_digest=RUN_DIGEST,
        generated_at="2026-01-01T00:00:00.000Z",
        conformance_summary="Every document validated against the FLUX schema.",
        reproducibility=Reproducibility.NOT_CHECKED,
    )
    kwargs.update(overrides)
    return render_evidence_report(**kwargs)


def test_the_sections_gain_forecast_accuracy_after_fidelity_only_with_forecasts() -> None:
    assert evidence_sections() == EVIDENCE_SECTIONS
    assert evidence_sections(forecast_accuracy=False) == EVIDENCE_SECTIONS
    gained = evidence_sections(forecast_accuracy=True)
    assert gained.index("Forecast Accuracy") == gained.index("Fidelity") + 1
    assert tuple(name for name in gained if name != "Forecast Accuracy") == EVIDENCE_SECTIONS
    assert gained[-1] == "What Was Not Proven", "the mandatory section stays last"


def test_a_report_with_no_forecast_is_the_report_it_was() -> None:
    assert _report() == _report(forecast_accuracy=None)
    assert "Forecast Accuracy" not in _report()
    assert "Weakest grade of every profile" not in _report()


def test_a_report_with_forecasts_has_the_section_and_the_weakest_grade(
    store: AccuracyStore,
) -> None:
    accuracy = _filled(store)
    run = RunGrade(profile=ProfileGrade(Pattern.P1, pinned=True))
    report = _report(forecast_accuracy=accuracy, privacy=run)
    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert headings == [f"## {name}" for name in evidence_sections(forecast_accuracy=True)]
    assert report.index("## Fidelity") < report.index("## Forecast Accuracy")
    assert report.index("## Forecast Accuracy") < report.index("## Privacy Grade")
    privacy = report.split("## Privacy Grade")[1].split("## Column Provenance")[0]
    assert "Weakest grade of every profile this run read: **P1 (checked)**." in privacy
    require_no_banned_words(report)
