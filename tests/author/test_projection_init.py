# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes ``snapshots``, ``events`` and ``periods`` entries from the contract (ADR 0027 section 9).

Pinned here, through ``fluxsim init`` and the seam:

* each rule written for an expose that meets it whole: ##R;
* each expose that meets only part of a rule left as it is and listed as unused, with the part it
  misses;
* a projected emit declares no ``rows``, and its published ``rowCount`` is unused;
* the written bundle validates and plans;
* a contract without such exposes, and a profile without a history, is what ``init`` wrote before.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.author.projection import published_period_range
from flux_engine.seam.history import history_entries_of
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_engine.stages.plan import plan

from ._written import plan_written, resolved_emits
from .test_history_init import (
    TIER,
    _bins,
    _init,
    _levels,
    _metrics,
    _ms,
    _namespace,
    _profile,
)

pytestmark = pytest.mark.integration

CONTRACT = Path(__file__).resolve().parents[1] / "fixtures" / "projection-init" / "plans.fluid.yml"
CHANNEL = ["app", "web"]


def _number(count: int) -> Dict[str, Any]:
    return {"domain": "number", "metrics": _metrics("1", str(count), count)}


def _date(count: int) -> Dict[str, Any]:
    return {
        "domain": "timestamp",
        "metrics": _metrics(_ms(2025, 1, 1), _ms(2025, 9, 1), count),
    }


def _category(levels: List[str], count: int) -> Dict[str, Any]:
    return {
        "domain": "category",
        "statistics": [_levels(levels, [count - count // 2, count // 2], count)],
    }


def _table(count: int, **columns: Dict[str, Any]) -> Dict[str, Any]:
    return {"rowCount": count, "columns": columns}


def _tables() -> Dict[str, Dict[str, Any]]:
    """The tables of the contract that read ``plan_version``, apart from the history itself."""
    return {
        "plan_invoice": _table(
            3000, invoice_id=_number(3000), plan_id=_number(1000), amount=_number(3000)
        ),
        "plan_current": _table(
            1000,
            plan_id=_number(1000),
            tier=_category(TIER[:2], 1000),
            channel=_category(CHANNEL, 1000),
        ),
        "plan_stale": _table(
            1000,
            plan_id=_number(1000),
            tier=_category(TIER[:2], 1000),
            owner=_category(CHANNEL, 1000),
        ),
        "plan_change": _table(
            3000,
            event_id=_number(3000),
            plan_id=_number(1000),
            op={
                "domain": "category",
                "statistics": [_levels(["c", "d", "u"], [1000, 200, 1800], 3000)],
            },
            recorded_on=_date(3000),
            before_tier=_category(TIER[:2], 3000),
            after_tier=_category(TIER[:2], 3000),
            before_channel=_category(CHANNEL, 3000),
            after_channel=_category(CHANNEL, 3000),
        ),
        "plan_log": _table(
            2000,
            event_id=_number(2000),
            plan_id=_number(1000),
            op={
                "domain": "category",
                "statistics": [_levels(["c", "u"], [1000, 1000], 2000)],
            },
            recorded_on=_date(2000),
        ),
        "plan_month": _table(
            4000,
            period_id=_number(4000),
            plan_id=_number(1000),
            month=_date(4000),
            tier=_category(TIER[:2], 4000),
        ),
        "plan_week": _table(
            5000,
            period_key={
                "domain": "category",
                "statistics": [_levels(["a", "b"], [2500, 2500], 5000)],
            },
            plan_id=_number(1000),
            week=_date(5000),
        ),
        "plan_span": _table(
            600,
            period_id=_number(600),
            plan_id=_number(1000),
            first_day=_date(600),
            last_day=_date(600),
        ),
    }


def _profile_with(*names: str) -> Dict[str, Any]:
    found = copy.deepcopy(_profile())
    available = _tables()
    for name in ("plan_invoice", *names):
        found["tables"][name] = available[name]
    return found


def _written(tmp_path: Path, *names: str) -> Dict[str, Any]:
    """Run ``init`` on the contract of this fixture, with the tables *names* and the history profile."""
    contract = CONTRACT.read_text(encoding="utf-8")
    _init(tmp_path, _profile_with(*names), contract)
    return _namespace(tmp_path)


def _simulation_text(tmp_path: Path) -> str:
    return (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")


def test_a_snapshot_is_written_for_an_expose_that_holds_the_last_columns(
    tmp_path: Path,
) -> None:
    namespace = _written(tmp_path, "plan_current")
    assert namespace[SNAPSHOTS_KEY] == [
        {"emit": "plan_current", "of": "plan_version", "ended": "keep"}
    ]
    assert EVENTS_KEY not in namespace and PERIODS_KEY not in namespace


def test_events_are_written_with_the_op_the_date_and_the_columns_that_may_be_null(
    tmp_path: Path,
) -> None:
    namespace = _written(tmp_path, "plan_change")
    assert namespace[EVENTS_KEY] == [
        {
            "emit": "plan_change",
            "of": "plan_version",
            "date": "recorded_on",
            "op": "op",
            "before": {"before_tier": "tier"},
            "after": {"after_tier": "tier", "after_channel": "channel"},
        }
    ]


def test_periodic_rows_are_written_with_a_grain_overlap_and_one_kind(
    tmp_path: Path,
) -> None:
    namespace = _written(tmp_path, "plan_month")
    assert namespace[PERIODS_KEY] == [
        {
            "emit": "plan_month",
            "of": "plan_version",
            "column": "month",
            "grain": "month",
            "match": "overlap",
            "kinds": [{"count": 1}],
        }
    ]


def test_a_projected_emit_declares_no_rows_and_its_row_count_is_unused(
    tmp_path: Path,
) -> None:
    namespace = _written(tmp_path, "plan_current", "plan_change", "plan_month", "plan_log")
    for emit in ("plan_current", "plan_change", "plan_month"):
        assert "rows" not in namespace["emits"][emit]
        assert namespace["emits"][emit]["fidelity"]
    assert namespace["emits"]["plan_log"]["rows"] == 2000
    assert namespace["emits"]["plan_invoice"]["rows"] == 3000
    pointers = {item["statistic"]["pointer"]: item["reason"] for item in namespace["unused"]}
    assert "projected from a history" in pointers["/tables/plan_month/rowCount"]
    assert "/tables/plan_log/rowCount" not in pointers


def test_an_expose_that_misses_a_column_of_the_history_is_unused_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    namespace = _written(tmp_path, "plan_stale")
    assert SNAPSHOTS_KEY not in namespace
    assert namespace["emits"]["plan_stale"]["rows"] == 1000
    text = _simulation_text(tmp_path)
    assert "plan_stale" in text


def _bundle_notes(tmp_path: Path, *names: str) -> List[str]:
    from flux_engine.author.bundle import AuthorInputs, author_bundle

    contract = tmp_path / "plans.fluid.yml"
    contract.write_text(CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    import json

    profile = tmp_path / "plans.profile.json"
    profile.write_text(json.dumps(_profile_with(*names)), encoding="utf-8")
    bundle = author_bundle(AuthorInputs(contract, profile))
    return [f"{subject}: {sentence}" for subject, sentence in bundle.notes]


def test_each_partial_match_is_recorded_as_unused_with_the_part_it_misses(
    tmp_path: Path,
) -> None:
    joined = "\n".join(_bundle_notes(tmp_path, "plan_stale", "plan_log", "plan_week", "plan_span"))
    assert "plan_invoice" not in joined
    assert "plan_stale: snapshots: unused" in joined
    assert "its column owner is no column of plan_version" in joined
    assert "plan_log: events: unused" in joined
    assert "no defaultAggTimeDimension" in joined
    assert "plan_week: periods: unused" in joined
    assert "its primary key period_key is not a whole-number column" in joined
    assert "plan_span: periods: unused" in joined
    assert "2 DATE dimensions with a timeGranularity, not exactly one" in joined


def test_a_before_column_the_contract_requires_is_left_out_and_said(
    tmp_path: Path,
) -> None:
    notes = "\n".join(_bundle_notes(tmp_path, "plan_change"))
    assert "plan_change: events before: before_channel is required" in notes


def test_an_image_whose_counts_are_withheld_keeps_its_levels_and_the_events_entry(
    tmp_path: Path,
) -> None:
    # The contract lists the levels of before_tier and the profile withholds every count of it, so
    # the column is drawn at equal weights over the levels its declared support names. The events
    # entry sets it from the history, so the support is no longer checked; the levels stay drawn,
    # and the bundle keeps its projections.
    contract = CONTRACT.read_text(encoding="utf-8").replace(
        "{ name: before_tier, type: STRING, required: false }",
        "{ name: before_tier, type: \"enum('basic','plus','pro')\", required: false }",
    )
    profile = _profile_with("plan_change")
    withheld = _levels(TIER, [0, 0, 0], 3000)
    withheld["groups"] = [{"group": "", "status": "C"}]
    profile["tables"]["plan_change"]["columns"]["before_tier"] = {
        "domain": "category",
        "statistics": [withheld],
    }
    # An image whose counts are published keeps the weights they give.
    profile["tables"]["plan_change"]["columns"]["after_tier"] = {
        "domain": "category",
        "statistics": [_levels(TIER[:2], [2000, 1000], 3000)],
    }
    _init(tmp_path, profile, contract)
    namespace = _namespace(tmp_path)
    assert [entry["emit"] for entry in namespace.get(EVENTS_KEY, [])] == ["plan_change"]
    columns = namespace["emits"]["plan_change"]["fidelity"]["columns"]
    column = columns["before_tier"]
    assert "support" not in column and "maxCardinality" not in column
    assert sorted(column["levels"]) == TIER
    weights = [Decimal(text) for text in column["levels"].values()]
    assert sum(weights) == 1 and max(weights) - min(weights) <= Decimal("1e-9")
    published = columns["after_tier"]["levels"]
    assert abs(Decimal(published["basic"]) - 2 * Decimal(published["plus"])) <= Decimal("1e-8")
    plan_written(tmp_path)


def _listed_tiers(profile: Dict[str, Any], *, derived: bool = False) -> str:
    """*profile* listing the tiers of ``plan_current`` without counts; the contract, with *derived*.

    With *derived*, a contract ``derive`` sets the snapshot's tier on every row.
    """
    listed = _levels(TIER[:2], [0, 0], 1000)
    listed["definition"] = "support/1"
    del listed["levels"]
    listed["groups"] = [{"group": "", "k": 1000, "support": TIER[:2], "status": "F"}]
    profile["tables"]["plan_current"]["columns"]["tier"] = {
        "domain": "category",
        "statistics": [listed],
    }
    contract = CONTRACT.read_text(encoding="utf-8")
    if derived:
        head, tail = contract.split("  - exposeId: plan_current\n", 1)
        tail = tail.replace(
            "        - { name: channel, type: STRING, required: true }\n",
            "        - { name: channel, type: STRING, required: true }\n"
            "      invariants:\n"
            "        - { id: current_tier, derive: tier, expr: \"'basic'\" }\n",
            1,
        )
        contract = head + "  - exposeId: plan_current\n" + tail
    return contract


def test_a_snapshot_column_drawn_by_its_support_alone_keeps_its_levels(tmp_path: Path) -> None:
    # The profile lists the snapshot's tiers but not their counts, so the column is drawn at equal
    # weights by its support alone; the plan still draws it, so it keeps those levels.
    profile = _profile_with("plan_current")
    contract = _listed_tiers(profile)
    _init(tmp_path, profile, contract)
    namespace = _namespace(tmp_path)
    assert [entry["emit"] for entry in namespace.get(SNAPSHOTS_KEY, [])] == ["plan_current"]
    column = namespace["emits"]["plan_current"]["fidelity"]["columns"]["tier"]
    assert "support" not in column and sorted(column["levels"]) == TIER[:2]
    plan_written(tmp_path)


def test_a_snapshot_column_a_derive_sets_keeps_no_levels(tmp_path: Path) -> None:
    # A derive without a when sets the column on every row, so the plan draws nothing for it.
    profile = _profile_with("plan_current")
    contract = _listed_tiers(profile, derived=True)
    _init(tmp_path, profile, contract)
    namespace = _namespace(tmp_path)
    assert [entry["emit"] for entry in namespace.get(SNAPSHOTS_KEY, [])] == ["plan_current"]
    assert "tier" not in namespace["emits"]["plan_current"]["fidelity"]["columns"]
    plan_written(tmp_path)


WRITTEN = ("plan_current", "plan_change", "plan_month")


def test_the_written_bundle_validates_and_resolves(tmp_path: Path) -> None:
    namespace = _written(tmp_path, *WRITTEN)
    assert SNAPSHOTS_KEY in namespace
    emits = resolved_emits(tmp_path)
    assert history_entries_of(emits["plan_version"].extension, "plan_version")
    assert "rows" not in (emits["plan_current"].extension or {})


def test_the_written_bundle_plans(tmp_path: Path) -> None:
    _written(tmp_path, *WRITTEN)
    plan_written(tmp_path)


KEYS = (SNAPSHOTS_KEY, EVENTS_KEY, PERIODS_KEY)


def test_a_contract_without_such_exposes_is_what_init_wrote_before(
    tmp_path: Path,
) -> None:
    from .test_history_init import CONTRACT as PLANS

    _init(tmp_path, _profile(), PLANS.read_text(encoding="utf-8"))
    text = _simulation_text(tmp_path)
    for key in KEYS:
        assert f"\n    {key}:" not in text
    assert "history:" in text


def test_a_projection_the_plan_refuses_is_not_written_and_the_history_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.author import bundle as authoring

    real = authoring._check

    def refuse_projections(
        files: Any, profile: Any, references: Any = None, overlay: Any = None
    ) -> None:
        if any(b"\n    snapshots:" in entry.data for entry in files):
            raise authoring.AuthorError("FLX-3403: a projection is refused")
        real(files, profile, references, overlay=overlay)

    monkeypatch.setattr(authoring, "_check", refuse_projections)
    namespace = _written(tmp_path, "plan_current")
    assert SNAPSHOTS_KEY not in namespace
    assert namespace["emits"]["plan_current"]["rows"] == 1000
    assert "history" in namespace
    notes = "\n".join(_bundle_notes(tmp_path, "plan_current"))
    assert "plan_current: projection" in notes and "FLX-3403: a projection is refused" in notes


@pytest.mark.parametrize("marker", [b"\n    snapshots:", b"\n    history:"])
def test_a_refusal_with_a_given_window_is_reported_not_retried_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: bytes
) -> None:
    from flux_engine.author import bundle as authoring
    from flux_engine.cli._init import run_init

    real = authoring._check

    def refuse_layers(
        files: Any, profile: Any, references: Any = None, overlay: Any = None
    ) -> None:
        if any(marker in entry.data for entry in files):
            raise authoring.AuthorError("FLX-3403: the layers are refused")
        real(files, profile, references, overlay=overlay)

    monkeypatch.setattr(authoring, "_check", refuse_layers)
    (tmp_path / "plans.fluid.yml").write_text(
        CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "plans.profile.json").write_text(
        json.dumps(_profile_with("plan_current")), encoding="utf-8"
    )
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "plans.fluid.yml"),
        profile=str(tmp_path / "plans.profile.json"),
        window=("2000-01-01", "2030-01-01"),
    )
    assert result.exit_code == 1, result.stdout
    assert "FLX-3403: the layers are refused" in (result.stderr or "")
    assert "window given with --window" in (result.stderr or "")
    assert not (tmp_path / "bundle").exists()


# -- a contract date range that only says "a date" gives the window nothing ---------------------

OPEN_ENDED = "between '1900-01-01' and '9999-12-31'"


def _with_signed_on_range(constraint: str) -> str:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    for expose in document["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] == "signed_on":
                column["validationRules"] = [{"type": "range", "constraint": constraint}]
    return yaml.safe_dump(document, sort_keys=False)


def test_a_contract_range_ending_in_year_9999_leaves_the_window_plannable(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _profile_with(*WRITTEN), _with_signed_on_range(OPEN_ENDED))
    bounds = _namespace_spec(tmp_path)["timeBounds"]
    assert int(bounds["end"][:4]) < 9999
    outcome = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        stage=False,
    )
    assert outcome.success, outcome.stderr


def _namespace_spec(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)["spec"]
    return found


def _monthly(first: date, months: int, status: str = "F") -> Dict[str, Any]:
    """A month column of ``plan_month`` whose histogram has *months* monthly bins from *first*."""
    edges = []
    year, month = first.year, first.month
    for _ in range(months + 1):
        edges.append(_ms(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    column = _date(4000)
    histogram = _bins(edges, [4000 // months] * months, 4000)
    histogram["groups"][0]["status"] = status
    if status != "F":
        histogram["groups"][0] = {"group": "", "status": status}
    column["statistics"] = [histogram]
    return column


def _periods_with(tmp_path: Path, month: Dict[str, Any]) -> Dict[str, Any]:
    profile = _profile_with("plan_month")
    profile["tables"]["plan_month"]["columns"]["month"] = month
    _init(tmp_path, profile, CONTRACT.read_text(encoding="utf-8"))
    (entry,) = _namespace(tmp_path)[PERIODS_KEY]
    found: Dict[str, Any] = entry
    return found


def test_a_published_histogram_bounds_the_periods_to_the_months_it_covers(
    tmp_path: Path,
) -> None:
    # Three monthly bins from March: the first edge is the first period, the last bin's start
    # the last one, so no periodic row lies in a month the data never held.
    entry = _periods_with(tmp_path, _monthly(date(2025, 3, 1), 3))
    assert (entry["from"], entry["through"]) == ("2025-03-01", "2025-05-01")
    plan_written(tmp_path)


@pytest.mark.parametrize(
    "first, months, window, until, expected",
    [
        # Inside the window: both bounds are written.
        ("2025-03-01", 3, ("2025-01-01", "2025-10-01"), None, ("2025-03-01", "2025-05-01")),
        # Before the window's start: periods start where the window does, so no from.
        ("2024-11-01", 7, ("2025-01-01", "2025-10-01"), None, (None, "2025-05-01")),
        # Past the window's end: the last period the window holds is the default, so no through.
        ("2025-06-01", 9, ("2025-01-01", "2025-10-01"), None, ("2025-06-01", None)),
        # Past until: held to the last day before it.
        ("2025-03-01", 6, ("2025-01-01", "2025-10-01"), "2025-07-01", ("2025-03-01", "2025-06-30")),
        # Covering the whole window: nothing to bound.
        ("2024-01-01", 36, ("2025-01-01", "2025-10-01"), None, (None, None)),
        # Wholly after the window: no range is left, so nothing is written.
        ("2026-01-01", 3, ("2025-01-01", "2025-10-01"), None, (None, None)),
    ],
)
def test_bounds_are_held_to_the_window_and_until(
    first: str,
    months: int,
    window: Tuple[str, str],
    until: Optional[str],
    expected: Tuple[Optional[str], Optional[str]],
) -> None:
    table = {"columns": {"month": _monthly(date.fromisoformat(first), months)}}
    bounds = (f"{window[0]}T00:00:00.000Z", f"{window[1]}T00:00:00.000Z")
    found = published_period_range(table, "month", "month", bounds, until)
    assert (found.get("from"), found.get("through")) == expected


def test_from_is_the_start_of_the_period_that_holds_the_first_edge() -> None:
    # A first edge inside a month opens the month that holds it, so the periods keep the days
    # they start on without a from, and no row of the first bin falls before the first period.
    edges = [_ms(2025, 3, 17), _ms(2025, 5, 1), _ms(2025, 6, 1)]
    table = {"columns": {"month": {"statistics": [_bins(edges, [10, 10], 20)]}}}
    bounds = ("2025-01-01T00:00:00.000Z", "2025-10-01T00:00:00.000Z")
    assert published_period_range(table, "month", "month", bounds, None) == {
        "from": "2025-03-01",
        "through": "2025-05-01",
    }
    # By quarter the first edge lies in the window's first quarter, so the anchor stands.
    assert published_period_range(table, "month", "quarter", bounds, None) == {
        "through": "2025-05-01"
    }


def test_a_withheld_histogram_or_none_keeps_the_entry_it_always_had(tmp_path: Path) -> None:
    entry = _periods_with(tmp_path, _monthly(date(2025, 3, 1), 3, status="C"))
    assert "from" not in entry and "through" not in entry
    bare = tmp_path / "bare"
    bare.mkdir()
    entry = _periods_with(bare, _date(4000))
    assert "from" not in entry and "through" not in entry
