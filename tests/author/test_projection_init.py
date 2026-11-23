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
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.seam.history import history_entries_of
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate

from .test_history_init import (
    TIER,
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
    from flux_engine.author.bundle import author_bundle

    contract = tmp_path / "plans.fluid.yml"
    contract.write_text(CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    import json

    profile = tmp_path / "plans.profile.json"
    profile.write_text(json.dumps(_profile_with(*names)), encoding="utf-8")
    bundle = author_bundle(contract, profile)
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


WRITTEN = ("plan_current", "plan_change", "plan_month")


def test_the_written_bundle_validates_and_resolves(tmp_path: Path) -> None:
    namespace = _written(tmp_path, *WRITTEN)
    assert SNAPSHOTS_KEY in namespace
    bundle = tmp_path / "bundle"
    assert validate(bundle).exit_code == 0
    loaded = load_bundle(bundle)
    (simulation_id,) = loaded.simulation_ids()
    emits = {e.expose_id: e for e in resolve_all_emits(loaded, simulation_id=simulation_id)}
    assert history_entries_of(emits["plan_version"].extension, "plan_version")
    assert "rows" not in (emits["plan_current"].extension or {})


def test_the_written_bundle_plans(tmp_path: Path) -> None:
    _written(tmp_path, *WRITTEN)
    outcome = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        kernel="0.2.0",
        stage=False,
    )
    assert outcome.success, outcome.stderr


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
