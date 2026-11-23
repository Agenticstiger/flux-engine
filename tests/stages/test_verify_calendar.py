# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` scores a date column's calendar against the pinned profile (ADR 0025 section 8).

``tests/fixtures/temporal`` is planned and applied with its fidelity blocks tolerating any mean, so the
only thing a profile can fail it on is its calendar. The profile is built here from the rows the
run wrote, counted with ``datetime`` (not with the code under test), then pinned in a second copy of
the bundle; the same seed draws the same rows, which the test checks before it relies on it.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import CALENDAR_LEVELS, SCHEMA_URL_1_3_0
from tests.fixtures.temporal.bundle import copy_bundle

pytestmark = pytest.mark.integration

#: The module, which ``flux_engine.stages`` shadows with its function of the same name.
verify_module = importlib.import_module("flux_engine.stages.verify")

_NAMESPACE = "  com.agentics.flux/1:\n"
#: The invoice's issue date is set by a ``within`` entry and shaped by a ``rates`` entry, so its
#: calendar is one the bundle shapes; the customer's start date is set by a lifetime and no ``rates``
#: entry shapes it, so its calendar is not gated.
SHAPED = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open }
  - { emit: invoice, relationship: account, date: i_issued }
rates:
  - { emit: invoice, column: i_issued, month: ['1', '1', '1', '1', '1', '1', '1', '1', '1', '1', '1', '1'] }
"""
_HIDDEN_TAIL = "the run's streams hold no hidden column and no hidden table"


def _histogram(
    key: str, counts: Sequence[Optional[int]], status: Optional[List[str]] = None
) -> Any:
    group: Dict[str, Any] = {
        "group": "",
        "k": sum(count or 0 for count in counts),
        "levelCounts": list(counts),
        "status": "F",
    }
    if status is not None:
        group["cellStatus"] = status
    return {
        "definition": "levelHistogram/1",
        "levels": list(CALENDAR_LEVELS[key]),
        "sampleSize": {"min": 1, "max": 10**9},
        "disclosure": "none",
        "groups": [group],
    }


def _profile(tables: Dict[str, Any], means: Dict[str, Tuple[int, int]]) -> Dict[str, Any]:
    return {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": _merged(tables, means),
    }


#: The column each emit's fidelity block compares a mean of.
_MEANS = {
    "customer": "cu_score",
    "account": "a_limit",
    "invoice": "i_amount",
    "payment": "p_amount",
}


def _merged(tables: Dict[str, Any], means: Dict[str, Tuple[int, int]]) -> Dict[str, Any]:
    """Every emit's table, each holding the mean its fidelity block asks for, and the calendars given.

    The mean is the run's own (``k`` records and the sum ``Sx`` of their values on a grid of 2^16), so
    no emit is refused or outside for want of a matching statistic, and the calendar is all that can
    fail a run.
    """
    found: Dict[str, Any] = {}
    for name, column in _MEANS.items():
        count, total = means[name]
        mean = {
            "definition": "mean/1",
            "sampleSize": {"min": 1, "max": 10**9},
            "disclosure": "none",
            "groups": [{"group": "", "k": count, "Sx": str(total * 65536), "status": "F"}],
        }
        given = tables.get(name, {})
        columns = {column: {"domain": "number", "statistics": [mean]}, **given.get("columns", {})}
        found[name] = {**given, "columns": columns}
    return found


def _written(layout: RunLayout, expose_id: str, column: str) -> List[Any]:
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    at = names.index(column)
    return [
        json.loads(line)[at] for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _by_hand(cells: Sequence[Optional[str]], *, timestamp: bool) -> Dict[str, List[int]]:
    """The three counts of a column's known values, counted with ``datetime``."""
    month = [0] * 12
    weekday = [0] * 7
    hour = [0] * 168
    for cell in cells:
        if cell is None:
            continue
        if timestamp:
            moment = datetime.strptime(cell, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            hour[(moment.isoweekday() - 1) * 24 + moment.hour] += 1
        else:
            moment = datetime.fromisoformat(cell)
        month[moment.month - 1] += 1
        weekday[moment.isoweekday() - 1] += 1
    return {"month": month, "dayOfWeek": weekday, "hourOfWeek": hour}


class Pinned:
    """The fixture applied under a profile built from its own rows, then altered by a test."""

    def __init__(self, tmp_path: Path, shape: Any, scorecard: str = "", keys: str = SHAPED) -> None:
        plain = copy_bundle(tmp_path, "plain", keys)
        planned = apply(plain, state_dir=tmp_path / "state-plain")
        assert planned.success, planned.stderr
        layout = RunLayout.for_state_dir(tmp_path / "state-plain", str(planned.artifacts["run_id"]))
        self.invoice = _by_hand(_written(layout, "invoice", "i_issued"), timestamp=True)
        self.customer = _by_hand(_written(layout, "customer", "cu_start"), timestamp=False)
        means = {
            name: (len(cells), sum(cells))
            for name, column in _MEANS.items()
            for cells in [_written(layout, name, column)]
        }
        document = _profile(shape(self.invoice, self.customer), means)
        self.profile = tmp_path / "profile.json"
        self.profile.write_text(json.dumps(document), "utf-8")
        self.bundle = copy_bundle(tmp_path, "pinned", keys)
        simulation = self.bundle / "simulation.flux.yml"
        pin = digest_of_document(document, subject="profile").value
        simulation.write_text(
            simulation.read_text("utf-8").replace(
                _NAMESPACE, f"{_NAMESPACE}    profile:\n      digest: {pin}\n{scorecard}", 1
            ),
            "utf-8",
        )
        self.state = tmp_path / "state"
        applied = apply(self.bundle, state_dir=self.state, profile=self.profile)
        assert applied.success, applied.stderr
        self.layout = RunLayout.for_state_dir(self.state, str(applied.artifacts["run_id"]))
        assert _written(self.layout, "invoice", "i_issued") == _written(
            layout, "invoice", "i_issued"
        )

    def verify(self) -> Any:
        return run_verify(
            self.bundle,
            run_id=self.layout.run_id,
            state_dir=self.state,
            profile=self.profile,
            allow_declared_baseline_drift=True,
        )


def _matching(invoice: Dict[str, List[int]], customer: Dict[str, List[int]]) -> Dict[str, Any]:
    return {
        "invoice": {
            "columns": {"i_issued": {"domain": "timestamp"}},
            "calendar": [
                {
                    "column": "i_issued",
                    "month": _histogram("month", invoice["month"]),
                    "dayOfWeek": _histogram("dayOfWeek", invoice["dayOfWeek"]),
                    "hourOfWeek": _histogram("hourOfWeek", invoice["hourOfWeek"]),
                }
            ],
        },
        "customer": {
            "columns": {"cu_start": {"domain": "timestamp"}},
            "calendar": [
                {
                    "column": "cu_start",
                    "month": _histogram("month", customer["month"]),
                    "hourOfWeek": _histogram("hourOfWeek", [1] * 168),
                }
            ],
        },
    }


@pytest.fixture
def matching(tmp_path: Path) -> Pinned:
    return Pinned(tmp_path, _matching)


def test_a_run_that_matches_its_profile_passes_each_calendar_line(matching: Pinned) -> None:
    result = matching.verify()
    assert result.success, result.stderr
    assert (
        "  calendar:i_issued (invoice): passed -- month distance 0 (tolerance 0.1); "
        "dayOfWeek distance 0 (tolerance 0.1); hourOfWeek distance 0 (tolerance 0.1)"
    ) in result.stdout
    assert (
        "  calendar:cu_start (customer): passed -- month distance 0 (tolerance 0.1)"
        in result.stdout
    )
    document = result.artifacts["calendar"]
    assert document["met"] is True
    assert [(line["line"], line["emit"], line["outcome"]) for line in document["lines"]] == [
        ("calendar:cu_start", "customer", "within"),
        ("calendar:i_issued", "invoice", "within"),
    ]


def test_a_date_column_with_a_published_hour_of_week_lists_it_as_not_scored(
    matching: Pinned,
) -> None:
    result = matching.verify()
    assert (
        "  facts: not scored -- calendar:cu_start.hourOfWeek: a DATE has no hour" in result.stdout
    )
    assert result.artifacts["calendar"]["notScored"] == [
        {"fact": "calendar:cu_start.hourOfWeek", "reason": "a DATE has no hour"}
    ]


def test_the_counts_are_the_ones_counted_by_hand(matching: Pinned) -> None:
    document = matching.verify().artifacts["calendar"]
    by_line = {line["line"]: line for line in document["lines"]}
    invoice = {part["histogram"]: part for part in by_line["calendar:i_issued"]["histograms"]}
    assert invoice["month"]["records"] == sum(matching.invoice["month"])
    assert invoice["hourOfWeek"]["records"] == sum(matching.invoice["hourOfWeek"])


def _skewed(invoice: Dict[str, List[int]], customer: Dict[str, List[int]]) -> Dict[str, Any]:
    tables = _matching(invoice, customer)
    # The profile puts every invoice in January, so the run, spread over the year, is far from it.
    tables["invoice"]["calendar"][0]["month"] = _histogram("month", [100] + [0] * 11)
    return tables


def test_a_skewed_run_fails_and_prints_the_distance(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, _skewed)
    result = pinned.verify()
    assert not result.success
    run = pinned.invoice["month"]
    expected = 1 - sum(run[:1]) / sum(run)
    document = result.artifacts["calendar"]
    assert document["met"] is False
    line = next(entry for entry in document["lines"] if entry["line"] == "calendar:i_issued")
    month = next(part for part in line["histograms"] if part["histogram"] == "month")
    assert month["outcome"] == "outside"
    assert month["tolerance"] == "0.1"
    assert Fraction(month["distance"]) == 1 - Fraction(run[0], sum(run))
    assert expected > 0.1
    assert "calendar:i_issued (invoice): failed -- month distance" in result.stderr
    assert "(tolerance 0.1) (outside)" in result.stderr
    # The other histograms of the line still match, and the customer's line is untouched.
    assert (
        next(p for p in line["histograms"] if p["histogram"] == "dayOfWeek")["outcome"] == "within"
    )
    assert (
        next(e for e in document["lines"] if e["line"] == "calendar:cu_start")["outcome"]
        == "within"
    )


def _suppressed(invoice: Dict[str, List[int]], customer: Dict[str, List[int]]) -> Dict[str, Any]:
    tables = _matching(invoice, customer)
    counts: List[Optional[int]] = list(invoice["month"])
    # The profile withheld the busiest month, and says nothing else wrong.
    busiest = counts.index(max(c for c in counts if c is not None))
    status = ["F"] * 12
    counts[busiest] = None
    status[busiest] = "C"
    tables["invoice"]["calendar"][0]["month"] = _histogram("month", counts, status)
    return tables


def test_a_suppressed_cell_is_left_out_on_both_sides(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, _suppressed)
    result = pinned.verify()
    assert result.success, result.stderr
    line = next(
        entry
        for entry in result.artifacts["calendar"]["lines"]
        if entry["line"] == "calendar:i_issued"
    )
    month = next(part for part in line["histograms"] if part["histogram"] == "month")
    assert month["outcome"] == "within"
    assert month["distance"] == "0"


def test_a_hidden_column_is_listed_as_not_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = Pinned(tmp_path, _matching)
    # The recorded plan lists a hidden column apart (tests/stages/test_hidden_columns_run.py); the
    # fixture declares none, so the plan's reading is replaced by one that names a column.
    monkeypatch.setattr(
        verify_module, "_hidden_columns_of", lambda plan: {"invoice": ("i_issued",)}
    )
    result = pinned.verify()
    reason = f"it reads the hidden column invoice.i_issued; {_HIDDEN_TAIL}"
    assert f"  facts: not scored -- calendar:i_issued: {reason}" in result.stdout
    document = result.artifacts["calendar"]
    assert {"fact": "calendar:i_issued", "reason": reason} in document["notScored"]
    assert [line["line"] for line in document["lines"]] == ["calendar:cu_start"]
    assert result.success, result.stderr


def _no_calendar(invoice: Dict[str, List[int]], customer: Dict[str, List[int]]) -> Dict[str, Any]:
    return {"invoice": {"columns": {"i_issued": {"domain": "timestamp"}}}}


def test_a_profile_with_no_calendar_verifies_as_it_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = Pinned(tmp_path, _no_calendar)
    result = pinned.verify()
    assert result.success, result.stderr
    assert "calendar" not in result.artifacts
    assert "calendar:" not in result.stdout
    assert "not scored" not in result.stdout

    def nothing(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(verify_module, "_calendar_pass", nothing)
    monkeypatch.setattr(verify_module, "_calendar_checks", nothing)
    again = pinned.verify()
    assert again.stdout == result.stdout
    assert again.stderr == result.stderr
    assert _stable(again.artifacts) == _stable(result.artifacts)


def _stable(artifacts: Dict[str, Any]) -> str:
    """The artifacts as JSON, without the one thing that is a clock."""
    return json.dumps(artifacts, sort_keys=True, default=str)


def test_a_run_without_a_pinned_profile_adds_no_calendar(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "unpinned")
    applied = apply(bundle, state_dir=tmp_path / "state")
    assert applied.success, applied.stderr
    result = run_verify(
        bundle,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=tmp_path / "state",
        allow_declared_baseline_drift=True,
    )
    assert "calendar" not in result.artifacts
    assert "calendar:" not in result.stdout


def test_a_hidden_column_of_the_recorded_plan_is_not_scored_and_the_written_one_is(
    tmp_path: Path,
) -> None:
    """The run of ``test_hidden_columns_run``: ``signup`` is drawn and never written, ``cu_signup`` is written."""
    from .test_hidden_columns_run import make_bundle

    month = _histogram("month", [1] * 12)
    document = _profile({}, {name: (1, 0) for name in _MEANS})
    document["tables"] = {
        "customer": {
            "columns": {
                "cu_signup": {"domain": "timestamp"},
                "signup": {"domain": "timestamp"},
            },
            "calendar": [
                {"column": "signup", "month": month},
                {"column": "cu_signup", "month": month},
            ],
        },
        "account": {"columns": {}},
    }
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(document), "utf-8")
    bundle = make_bundle(tmp_path)
    pin = digest_of_document(document, subject="profile").value
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text("utf-8").replace(
            _NAMESPACE, f"{_NAMESPACE}    profile:\n      digest: {pin}\n", 1
        ),
        "utf-8",
    )
    applied = apply(bundle, state_dir=tmp_path / "state", profile=profile)
    assert applied.success, applied.stderr
    result = run_verify(
        bundle,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=tmp_path / "state",
        profile=profile,
        allow_declared_baseline_drift=True,
    )
    reason = f"it reads the hidden column customer.signup; {_HIDDEN_TAIL}"
    document = result.artifacts["calendar"]
    assert document["notScored"] == [{"fact": "calendar:signup", "reason": reason}]
    assert [(line["line"], line["emit"]) for line in document["lines"]] == [
        ("calendar:cu_signup", "customer")
    ]


# ---------------------------------------------------------------------------
# The scorecard's calendar rows (SCORECARD-SPEC.md section 3, format 1-4-0)
# ---------------------------------------------------------------------------

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
_FORMAT_1_4_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/scorecard/1-4-0/FluxScorecard.json"
)


def _temporal_floor(value: str) -> str:
    return (
        "    scorecard:\n      thresholds:\n        dimensions:\n"
        f'          temporal: {{ mustBeGreaterOrEqualTo: "{value}" }}\n'
    )


def _rows(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        row["propertyId"]: row
        for table in document["tables"]
        for row in table["properties"]
        if row["property"] == "calendar"
    }


def _dimension(document: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(entry for entry in document["dimensions"] if entry["dimension"] == name)


def test_each_calendar_histogram_verify_scores_is_a_row_of_the_scorecard(
    matching: Pinned,
) -> None:
    result = matching.verify()
    assert result.success, result.stderr
    document = result.artifacts["scorecard"]["document"]
    assert document["_schemaURL"] == _FORMAT_1_4_0
    rows = _rows(document)
    # The DATE column's published hourOfWeek is listed as not scored, so it is no row.
    assert sorted(rows) == [
        "calendar:cu_start.month",
        "calendar:i_issued.dayOfWeek",
        "calendar:i_issued.hourOfWeek",
        "calendar:i_issued.month",
    ]
    for row in rows.values():
        assert row["dimension"] == "temporal" and row["outcome"] == "within"
        assert row["score"] == "1" and row["distance"] == "0" and row["tolerance"] == "0.1"
    assert rows["calendar:i_issued.hourOfWeek"]["records"] == sum(matching.invoice["month"])
    temporal = _dimension(document, "temporal")
    assert temporal["status"] == "measured" and temporal["score"] == "1"
    assert temporal["counts"]["scored"] == 4
    schema = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    # The calendar lines read the same comparison: the rows add nothing to what verify prints.
    assert result.artifacts["calendar"]["met"] is True


def test_a_skewed_calendar_scores_one_minus_the_distance_the_line_prints(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, _skewed)
    result = pinned.verify()
    document = result.artifacts["scorecard"]["document"]
    month = _rows(document)["calendar:i_issued.month"]
    run = pinned.invoice["month"]
    distance = 1 - Fraction(run[0], sum(run))
    line = next(
        entry
        for entry in result.artifacts["calendar"]["lines"]
        if entry["line"] == "calendar:i_issued"
    )
    printed = next(part for part in line["histograms"] if part["histogram"] == "month")
    assert month["outcome"] == "outside" and month["distance"] == printed["distance"]
    assert Fraction(month["distance"]) == distance
    assert Fraction(month["score"]) == 1 - distance
    assert _rows(document)["calendar:i_issued.dayOfWeek"]["outcome"] == "within"
    temporal = _dimension(document, "temporal")
    expected = (1 - distance + 3) / 4
    assert Fraction(temporal["score"]) == expected


def test_a_threshold_on_temporal_is_met_and_withheld_by_the_calendar_scores(
    tmp_path: Path,
) -> None:
    met = Pinned(tmp_path / "met", _matching, _temporal_floor("0.99")).verify()
    assert met.success, met.stderr
    (entry,) = met.artifacts["scorecard"]["document"]["thresholds"]
    assert entry["target"] == "dimensions.temporal" and entry["met"] is True
    missed = Pinned(tmp_path / "missed", _skewed, _temporal_floor("0.99")).verify()
    assert not missed.success
    (entry,) = missed.artifacts["scorecard"]["document"]["thresholds"]
    assert entry["met"] is False


def test_a_threshold_on_temporal_is_not_met_when_the_profile_publishes_no_calendar(
    tmp_path: Path,
) -> None:
    result = Pinned(tmp_path, _no_calendar, _temporal_floor("0")).verify()
    document = result.artifacts["scorecard"]["document"]
    (entry,) = document["thresholds"]
    assert entry["met"] is False and not result.success
    assert _dimension(document, "temporal")["status"] == "not measured"
    assert _rows(document) == {}


def test_a_profile_with_no_calendar_writes_a_scorecard_with_no_calendar_row_and_not_1_4_0(
    tmp_path: Path,
) -> None:
    document = Pinned(tmp_path, _no_calendar).verify().artifacts["scorecard"]["document"]
    assert document["_schemaURL"] != _FORMAT_1_4_0
    assert _rows(document) == {}
    assert _dimension(document, "temporal") == {
        "dimension": "temporal",
        "status": "not measured",
        "score": None,
        "weight": "1",
        "counts": {"properties": 0, "scored": 0, "censored": 0, "refused": 0},
    }


def test_ship_writes_the_same_calendar_rows_into_the_scorecard_it_gates_on(
    tmp_path: Path,
) -> None:
    pinned = Pinned(tmp_path, _matching, _temporal_floor("0.99"))
    verified = pinned.verify().artifacts["scorecard"]["document"]
    shipped = ship(
        pinned.bundle,
        run_id=pinned.layout.run_id,
        out_dir=tmp_path / "out",
        state_dir=pinned.state,
        profile=pinned.profile,
    )
    assert shipped.success, shipped.stderr
    written = json.loads((pinned.layout.run_dir / "scorecard.json").read_text("utf-8"))
    assert _rows(written) == _rows(verified) and _rows(written)
    assert _dimension(written, "temporal") == _dimension(verified, "temporal")
    (entry,) = written["thresholds"]
    assert entry["met"] is True


# ---------------------------------------------------------------------------
# Which calendars gate a run, and what ship does with the ones that do
# ---------------------------------------------------------------------------


def _skewed_customer(
    invoice: Dict[str, List[int]], customer: Dict[str, List[int]]
) -> Dict[str, Any]:
    tables = _matching(invoice, customer)
    # The profile puts every customer's start in January: far from the run, on a column nothing shapes.
    tables["customer"]["calendar"][0]["month"] = _histogram("month", [100] + [0] * 11)
    return tables


def _ship(pinned: Pinned, out: Path) -> Any:
    return ship(
        pinned.bundle,
        run_id=pinned.layout.run_id,
        out_dir=out,
        state_dir=pinned.state,
        profile=pinned.profile,
    )


def test_a_calendar_outside_its_tolerance_on_a_column_no_rates_entry_shapes_is_not_gated(
    tmp_path: Path,
) -> None:
    pinned = Pinned(tmp_path, _skewed_customer)
    result = pinned.verify()
    assert result.success, result.stderr
    line = next(
        entry
        for entry in result.artifacts["calendar"]["lines"]
        if entry["line"] == "calendar:cu_start"
    )
    assert line["outcome"] == "outside" and line["gated"] is False
    assert result.artifacts["calendar"]["met"] is True
    printed = next(row for row in result.stdout.splitlines() if "calendar:cu_start (" in row)
    assert "not gated: no rates entry shapes this column" in printed
    assert "month distance" in printed and "(outside)" in printed
    # Its scorecard row keeps its outcome.
    rows = [
        entry
        for table in result.artifacts["scorecard"]["document"]["tables"]
        for entry in table["properties"]
        if entry.get("propertyId") == "calendar:cu_start.month"
    ]
    assert [entry["outcome"] for entry in rows] == ["outside"]
    assert result.artifacts["scorecard"]["met"] is True


def test_ship_packs_a_run_whose_ungated_calendar_is_outside_its_tolerance(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, _skewed_customer)
    assert pinned.verify().success
    shipped = _ship(pinned, tmp_path / "out")
    assert shipped.success, shipped.stderr


def test_ship_withholds_a_run_verify_fails_on_a_shaped_calendar_and_names_the_line(
    tmp_path: Path,
) -> None:
    pinned = Pinned(tmp_path, _skewed)
    verified = pinned.verify()
    assert not verified.success
    shipped = _ship(pinned, tmp_path / "out")
    assert not shipped.success
    assert shipped.error_code == "FLX-8001"
    assert "withheld: invoice" in shipped.stdout
    assert "run.calendar: calendar:i_issued (invoice): failed -- month distance" in shipped.stdout
    assert "calendar:i_issued" in shipped.stderr
    assert shipped.artifacts["scorecard"]["met"] is False
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())
    assert not (pinned.layout.run_dir / "EVIDENCE.md").exists()
    assert not (pinned.layout.run_dir / "scorecard.json").exists()


def test_verify_marks_the_scorecard_not_met_for_a_failed_calendar_line(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, _skewed)
    result = pinned.verify()
    assert result.artifacts["scorecard"]["met"] is False
    assert result.artifacts["scorecard"]["document"]["met"] is False
