# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` starts a periods entry and limits an events entry where the data does (ADR 0027 section 9).

A lending library: membership versions, a monthly fee table and a renewal event table, in a run
whose window starts in 2020. Pinned here:

* a contract range on the fee month writes ``from`` (and ``through``) on the periods entry, which
  plan accepts, and the run holds no period before it;
* without a declared range, a published minimum alone does not start the periods (only a published
  histogram does), but it limits the events;
* the events of a history that ends no entity are limited to the versions that start inside the
  date column's span, by a ``when`` on the validity start; a history that ends entities is not
  limited, and says why;
* a contract that declares no range, with a profile that publishes no span, writes what it wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan

from .test_history_init import _levels, _ms, _namespace
from .test_projection_init import CONTRACT, _profile_with

pytestmark = pytest.mark.integration

WINDOW = ("2020-01-01", "2025-12-31")


def _renamed(text: str) -> str:
    for old, new in (
        ("plan_month", "member_fee"),
        ("plan_change", "member_renewal"),
        ("plan_version", "member_version"),
        ("plan_", "member_"),
        ("name: month,", "name: fee_month,"),
        ('"month":', '"fee_month":'),
    ):
        text = text.replace(old, new)
    return text


def _library(
    tmp_path: Path,
    *names: str,
    declare: Optional[Dict[str, str]] = None,
    published: bool = True,
    ending: bool = True,
    window: Optional[Any] = WINDOW,
    start: str = "valid_from",
) -> Any:
    """Run ``init`` on the lending library: its contract (a range per column in *declare*) and profile.

    *start* names the history's validity start column.
    """
    document = yaml.safe_load(
        _renamed(CONTRACT.read_text(encoding="utf-8")).replace("valid_from", start)
    )
    for expose in document["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] in (declare or {}):
                column["validationRules"] = [
                    {"type": "range", "constraint": (declare or {})[column["name"]]}
                ]
    profile = json.loads(_renamed(json.dumps(_profile_with(*(_nick(n) for n in names)))))
    tables = profile["tables"]
    _begin_in_2020(tables["member_version"])
    profile = json.loads(json.dumps(profile).replace("valid_from", start))
    tables = profile["tables"]
    if not published:
        for table, column in (("member_fee", "fee_month"), ("member_renewal", "recorded_on")):
            if table in tables:
                del tables[table]["columns"][column]["metrics"]["min"]
                del tables[table]["columns"][column]["metrics"]["max"]
    if not ending:
        tables["member_version"]["history"]["lastVersion"] = _levels(
            ["closed", "open"], [0, 1000], 1000
        )
    (tmp_path / "library.fluid.yml").write_text(yaml.safe_dump(document, sort_keys=False))
    (tmp_path / "library.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
        window=window,
    )
    assert result.exit_code == 0, result.stderr
    return result


def _begin_in_2020(table: Dict[str, Any]) -> None:
    """Memberships begin from 2020, so the periods before 2024 have versions to hold."""
    history = table["history"]
    history["firstStart"]["bins"] = [
        _ms(2020, 1, 1),
        _ms(2022, 1, 1),
        _ms(2024, 1, 1),
        _ms(2025, 9, 1),
    ]
    for name, low in (("valid_from", _ms(2020, 1, 1)), ("valid_to", _ms(2020, 2, 1))):
        table["columns"][name]["metrics"]["min"]["value"] = low


def _nick(name: str) -> str:
    return {"member_fee": "plan_month", "member_renewal": "plan_change"}[name]


def _entries(tmp_path: Path, key: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = _namespace(tmp_path)[key]
    return found


def _apply(tmp_path: Path) -> Dict[str, Any]:
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    planned = plan(
        bundle,
        state_dir=state,
        profile=tmp_path / "library.profile.json",
        kernel="0.2.0",
        stage=False,
    )
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    (found,) = (state / "runs").glob("*/emits/member_fee.ndjson")
    lines = found.read_text(encoding="utf-8").splitlines()
    return {"fee": [json.loads(line)[2] for line in lines]}  # fee_month: the third column


def test_a_declared_range_starts_the_periods_where_the_contract_says(tmp_path: Path) -> None:
    _library(tmp_path, "member_fee", declare={"fee_month": "between '2024-01-01' and '2025-06-30'"})
    (entry,) = _entries(tmp_path, PERIODS_KEY)
    assert entry["from"] == "2024-01-01"
    assert entry["through"] == "2025-06-30"
    months = _apply(tmp_path)["fee"]
    assert months
    assert min(str(month)[:10] for month in months) >= "2024-01-01"
    assert max(str(month)[:10] for month in months) <= "2025-06-30"
    # Without the range (and with no published span), the periods start at the window's start.
    other = tmp_path / "other"
    other.mkdir()
    _library(other, "member_fee", published=False)
    everything = _apply(other)["fee"]
    assert min(str(month)[:10] for month in everything) == "2020-01-01"
    assert len(months) < len(everything)


def test_a_declared_range_gives_fewer_periods_than_the_window_does(tmp_path: Path) -> None:
    _library(tmp_path, "member_fee", declare={"fee_month": ">= '2024-01-01'"})
    (entry,) = _entries(tmp_path, PERIODS_KEY)
    assert entry["from"] == "2024-01-01"
    assert "through" not in entry or entry["through"] >= entry["from"]


def test_a_published_minimum_alone_does_not_start_the_periods(tmp_path: Path) -> None:
    _library(tmp_path, "member_fee")
    (entry,) = _entries(tmp_path, PERIODS_KEY)
    assert "from" not in entry and "through" not in entry  # the profile's minimum is not read
    assert plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        kernel="0.2.0",
        stage=False,
    ).success


def test_a_published_span_limits_the_events_when_no_range_is_declared(tmp_path: Path) -> None:
    _library(tmp_path, "member_renewal", ending=False)
    (entry,) = _entries(tmp_path, EVENTS_KEY)
    assert entry["when"] == "valid_from >= DATE '2025-01-01' AND valid_from <= DATE '2025-09-01'"


def test_a_range_before_the_window_writes_no_from(tmp_path: Path) -> None:
    _library(tmp_path, "member_fee", declare={"fee_month": ">= '2019-01-01'"}, published=False)
    (entry,) = _entries(tmp_path, PERIODS_KEY)
    assert "from" not in entry


def test_events_of_a_history_that_ends_nobody_start_at_the_date_columns_span(
    tmp_path: Path,
) -> None:
    _library(
        tmp_path,
        "member_renewal",
        declare={"recorded_on": "between '2024-01-01' and '2025-06-30'"},
        ending=False,
    )
    (entry,) = _entries(tmp_path, EVENTS_KEY)
    assert entry["when"] == "valid_from >= DATE '2024-01-01' AND valid_from <= DATE '2025-06-30'"
    assert plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        kernel="0.2.0",
        stage=False,
    ).success


@pytest.mark.parametrize("start", ["date", "end", "Valid From"])
def test_a_start_column_the_language_reserves_is_quoted_in_the_when(
    tmp_path: Path, start: str
) -> None:
    _library(
        tmp_path,
        "member_renewal",
        declare={"recorded_on": "between '2024-01-01' and '2025-06-30'"},
        ending=False,
        start=start,
    )
    (entry,) = _entries(tmp_path, EVENTS_KEY)
    column = start if start.isidentifier() and start != "date" and start != "end" else f'"{start}"'
    assert entry["when"] == f"{column} >= DATE '2024-01-01' AND {column} <= DATE '2025-06-30'"
    assert plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        kernel="0.2.0",
        stage=False,
    ).success


def test_no_upper_bound_is_written_where_the_window_ends_on_the_last_day(
    tmp_path: Path,
) -> None:
    # The window ends at the start of 2025-12-31, so its last day is 2025-12-30 and a start lies on no later day.
    _library(
        tmp_path,
        "member_renewal",
        declare={"recorded_on": "between '2024-01-01' and '2025-12-30'"},
        ending=False,
    )
    (entry,) = _entries(tmp_path, EVENTS_KEY)
    assert entry["when"] == "valid_from >= DATE '2024-01-01'"


def test_events_of_a_history_that_ends_entities_are_not_limited_and_say_why(
    tmp_path: Path,
) -> None:
    result = _library(
        tmp_path, "member_renewal", declare={"recorded_on": "between '2024-01-01' and '2025-06-30'"}
    )
    (entry,) = _entries(tmp_path, EVENTS_KEY)
    assert "when" not in entry
    written = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "dated by a version's end" in written + json.dumps(result.artifacts, default=str)


def test_a_contract_with_no_range_and_no_published_span_writes_no_start(tmp_path: Path) -> None:
    _library(tmp_path, "member_fee", published=False)
    (entry,) = _entries(tmp_path, PERIODS_KEY)
    assert entry == {
        "emit": "member_fee",
        "of": "member_version",
        "column": "fee_month",
        "grain": "month",
        "match": "overlap",
        "kinds": [{"count": 1}],
    }


def test_events_with_no_range_and_no_published_span_are_not_limited(tmp_path: Path) -> None:
    _library(tmp_path, "member_renewal", published=False, ending=False)
    (event,) = _entries(tmp_path, EVENTS_KEY)
    assert "when" not in event
