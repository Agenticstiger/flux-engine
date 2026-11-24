# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle ``fluxsim init`` writes passes ``verify`` against the profile it was written from.

Pinned here, through ``fluxsim init`` and a real plan, apply and verify:

* ``init`` writes no property its profile has no statistic for (``FLX-5037``), so a bundle written
  from the orders profile with no mean, from a history profile and from a projection profile exits 0
  at ``verify``, and so does the one written with an overlay;
* the property left out is listed in the report and in the command's document, with the reason;
* a table left with no property declares an empty list, and ``init`` says it has nothing to verify;
* a profile that does carry the statistic still gets the property;
* a column of a projected emit that holds the history's values declares no range or levels the
  history could leave, and no property of the table's own statistics, so the run passes;
* every ``within`` entry with ``exposure: true`` and every ``rates`` entry the bundle declares is
  tried by a run of the bundle itself (ADR 0025 sections 7 and 8). An item whose line the run does
  not reproduce, or whose line the trial does not form, is left out and recorded as unused with the
  trial's numbers, so the run of the bundle ``init`` writes exits 0. Two profiles (a customer with
  subscriptions, invoices and orders, and a plan with invoices) give the end-to-end cases; the unit
  cases stand in for the trial's run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import pytest
import yaml

from flux_engine.api.stages import StageResult
from flux_engine.author import bundle as authoring
from flux_engine.cli._init import run_init
from flux_engine.stages.apply import apply as apply_stage
from flux_engine.stages.pipeline import PlannedRowsAboveCeiling
from flux_engine.stages.plan import plan as plan_stage
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify as verify_stage
from tests.author.test_history_init import CONTRACT as HISTORY_CONTRACT
from tests.author.test_history_init import _profile as history_profile
from tests.author.test_projection_init import CONTRACT as PROJECTION_CONTRACT
from tests.author.test_projection_init import _profile_with
from tests.overlays import (
    DOUBLE_ORDERS,
    ORDERS_CONTRACT,
    ORDERS_MEAN_PROFILE,
    ORDERS_PROFILE,
)

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "init-verifies"
NAMESPACE = "com.agentics.flux/1"


def _init(root: Path, contract: Path, profile: Path, overlays: tuple[str, ...] = ()) -> Any:
    result = run_init(
        str(root / "bundle"), contract=str(contract), profile=str(profile), overlays=overlays
    )
    assert result.exit_code == 0, result.stderr
    return result


def _verified(bundle: Path, profile: Path, state: Path) -> Any:
    """Plan, apply and verify *bundle* against *profile*."""
    planned = plan_stage(str(bundle), state_dir=str(state), profile=str(profile))
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    return verify_stage(
        bundle,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(profile),
        allow_declared_baseline_drift=True,
    )


def _emits(bundle: Path) -> Dict[str, Any]:
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    emits: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    return emits


def _written(root: Path, contract: Path, profile: Dict[str, Any]) -> tuple[Any, Path, Path]:
    """``init`` for *contract* and the profile document *profile*: the result, the bundle and the file."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return _init(root, contract, path), root / "bundle", path


def test_orders_without_a_mean_write_no_property_and_verify_exits_0(tmp_path: Path) -> None:
    result = _init(tmp_path, ORDERS_CONTRACT, ORDERS_PROFILE)
    bundle = tmp_path / "bundle"
    fidelity = _emits(bundle)["orders"]["fidelity"]
    assert fidelity["properties"] == []
    # Nothing the profile publishes is dropped: the draw's range is still declared.
    assert fidelity["columns"]["amount"]["range"] == {"min": "10", "max": "100"}
    text = result.stdout
    assert "verify --profile` refuses" not in text
    assert "left out 1 property" in text and "marginalHistogram of amount (orders)" in text
    assert "(FLX-5037)" in text
    assert "table orders has nothing to verify" in text
    (table,) = result.artifacts["exposes"]
    assert [item["property"]["property"] for item in table["leftOut"]] == ["marginalHistogram"]
    assert "FLX-5037" in table["leftOut"][0]["reason"]
    verified = _verified(bundle, ORDERS_PROFILE, tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert "fidelity orders: not scored" in verified.stdout
    assert "FLX-5037" not in verified.stdout


def test_orders_with_a_mean_still_write_and_pass_the_mean_property(tmp_path: Path) -> None:
    result = _init(tmp_path, ORDERS_CONTRACT, ORDERS_MEAN_PROFILE)
    bundle = tmp_path / "bundle"
    properties = _emits(bundle)["orders"]["fidelity"]["properties"]
    assert [item["property"] for item in properties] == ["mean"]
    assert "left out" not in result.stdout and "nothing to verify" not in result.stdout
    verified = _verified(bundle, ORDERS_MEAN_PROFILE, tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert "fidelity orders: passed" in verified.stdout


def test_the_overlay_example_of_the_docs_passes_end_to_end(tmp_path: Path) -> None:
    overlay = tmp_path / "double-orders.yml"
    overlay.write_text(yaml.safe_dump(DOUBLE_ORDERS, sort_keys=False), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_bytes(ORDERS_PROFILE.read_bytes())
    result = run_init(
        str(tmp_path / "bundles"),
        contract=str(ORDERS_CONTRACT),
        profile=str(profile),
        overlays=(str(overlay),),
    )
    assert result.exit_code == 0, result.stderr
    bundle = tmp_path / "bundles" / "double-orders"
    assert _emits(bundle)["orders"]["rows"] == 1200
    assert _emits(bundle)["orders"]["fidelity"]["properties"] == []
    verified = _verified(bundle, profile, tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert "FLX-5037" not in verified.stdout


def _closed(history: Dict[str, Any]) -> None:
    history["lastVersion"]["groups"][0]["levelCounts"] = [1000, 0]


def _day_before(history: Dict[str, Any]) -> None:
    history["chain"]["groups"][0]["levelCounts"] = [800, 10, 20, 20]


def _one_version(history: Dict[str, Any]) -> None:
    history["versionsPerEntity"]["bins"] = ["1", "2"]
    history["versionsPerEntity"]["groups"][0]["binCounts"] = [1000]


def _two_reasons(history: Dict[str, Any]) -> None:
    history["reasons"]["groups"][0]["levelCounts"] = [100, 50, 700]
    history["columns"]["channel"]["transitions"]["groups"] = [
        {"group": "app", "k": 60, "levelCounts": [0, 60], "status": "F"},
        {"group": "web", "k": 40, "levelCounts": [40, 0], "status": "F"},
    ]


HISTORIES: Dict[str, Optional[Callable[[Dict[str, Any]], None]]] = {
    "base": None,
    "every version closed": _closed,
    "day before": _day_before,
    "one version": _one_version,
    "two reasons": _two_reasons,
}


@pytest.mark.parametrize("name", sorted(HISTORIES))
def test_a_history_bundle_verifies_with_exit_0(tmp_path: Path, name: str) -> None:
    profile = history_profile()
    change = HISTORIES[name]
    if change is not None:
        change(profile["tables"]["plan_version"]["history"])
    result, bundle, path = _written(tmp_path, HISTORY_CONTRACT, profile)
    # The account table has only a key, which no statistic of the profile can score a mean on.
    assert _emits(bundle)["account"]["fidelity"]["properties"] == []
    assert "table account has nothing to verify" in result.stdout
    assert "verify --profile` refuses" not in result.stdout
    verified = _verified(bundle, path, tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert "fidelity account: not scored" in verified.stdout
    assert "FLX-5037" not in verified.stdout


TABLES = ("plan_current", "plan_change", "plan_month")


def test_a_projection_bundle_verifies_with_exit_0(tmp_path: Path) -> None:
    result, bundle, path = _written(tmp_path, PROJECTION_CONTRACT, _profile_with(*TABLES))
    emits = _emits(bundle)
    # The projected emit's tier is the history's, so it declares no support the history could
    # leave (FLX-5005) and no level histogram of the table's own statistic.
    for emit, column in (("plan_current", "tier"), ("plan_month", "tier")):
        declared = emits[emit]["fidelity"]["columns"][column]
        assert "support" not in declared and "range" not in declared
        assert all(item["column"] != column for item in emits[emit]["fidelity"]["properties"])
    change = emits["plan_change"]["fidelity"]
    for column in ("before_tier", "after_tier"):
        assert "support" not in change["columns"][column]
        assert all(item.get("column") != column for item in change["properties"])
    assert "tables plan_current, plan_month have nothing to verify" in result.stdout
    assert "projection's own checks" in result.stdout
    verified = _verified(bundle, path, tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr
    assert "FLX-5037" not in verified.stdout and "FLX-5005" not in verified.stdout
    # The projections are kept: the emits take their rows from the history.
    namespace = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    assert "snapshots" in namespace["extensions"][NAMESPACE]


def _fixture_bundle(tmp_path_factory: pytest.TempPathFactory, name: str) -> Path:
    directory = tmp_path_factory.mktemp(name) / "bundle"
    result = run_init(
        str(directory),
        contract=str(FIXTURES / name / "contract.fluid.yaml"),
        profile=str(FIXTURES / name / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return directory


def _block(directory: Path) -> Dict[str, Any]:
    document = yaml.safe_load((directory / "simulation.flux.yml").read_text(encoding="utf-8"))
    return dict(document["extensions"][NAMESPACE])


def _reasons(directory: Path, prefix: str) -> List[str]:
    """The reason of each unused statistic whose pointer starts with *prefix*."""
    return [
        item["reason"]
        for item in _block(directory)["unused"]
        if item["statistic"]["pointer"].startswith(prefix)
    ]


def _run_verified(directory: Path, name: str, tmp_path: Path) -> StageResult:
    return run(
        directory,
        state_dir=tmp_path / "state",
        profile=FIXTURES / name / "profile.json",
        verify=True,
    )


@pytest.fixture(scope="module")
def jc1(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _fixture_bundle(tmp_path_factory, "jc1")


@pytest.fixture(scope="module")
def journey_d(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _fixture_bundle(tmp_path_factory, "journey-d")


def test_the_bundle_of_a_customer_profile_passes_its_own_verify(jc1: Path, tmp_path: Path) -> None:
    result = _run_verified(jc1, "jc1", tmp_path)
    assert result.exit_code == 0, result.stderr
    verify = result.artifacts["verify"]
    assert verify["calendar"]["met"] is True
    # A column keeps its rates only with a calendar its run passes; the calendar of a column whose
    # rates are left out is printed, and does not gate, whatever its distances.
    _assert_calendars_gate_only_where_rated(jc1, verify["calendar"]["lines"])


def test_the_exposures_over_root_lifetimes_the_run_cannot_meet_are_left_out(jc1: Path) -> None:
    within = {entry["relationship"]: entry for entry in _block(jc1)["within"]}
    assert "exposure" not in within["subscription_to_customer"]
    assert "exposure" not in within["order_to_customer"]
    for pointer in ("/relationships/0/exposure/0/", "/relationships/2/exposure/0/"):
        reasons = _reasons(jc1, pointer)
        assert len(reasons) == 2 and reasons[0] == reasons[1]
        assert "cannot reproduce this exposure's rate under the declared lifetimes" in reasons[0]
        assert "a trial run has children per active day" in reasons[0]
        assert "against the profile's" in reasons[0] and "gap " in reasons[0]
        assert "(tolerance 0.1)" in reasons[0]
        assert "within its own parent's lifetime" not in reasons[0]


def test_the_chained_exposure_keeps_its_note(jc1: Path) -> None:
    reasons = _reasons(jc1, "/relationships/1/exposure/0/")
    assert reasons and "whose parent's lifetime lies within its own parent's lifetime" in reasons[0]


def _assert_calendars_gate_only_where_rated(
    directory: Path, lines: List[Mapping[str, Any]]
) -> None:
    """Each calendar line gates exactly when its column has a ``rates`` entry, and then passes."""
    rated = {(entry["emit"], entry["column"]) for entry in _block(directory).get("rates", [])}
    for line in lines:
        key = (line["emit"], str(line["line"]).split(":", 1)[1])
        assert line.get("gated", True) is (key in rated), line["line"]
        if key in rated:
            assert line["outcome"] == "within", line["line"]


_TRIAL_DECLINED = "puts this column's calendar outside its tolerance ("


def _assert_rates_follow_the_trial(directory: Path, emit: str, column: str) -> None:
    """The ``rates`` of *column* are kept, with no trial's reason, or left out with the trial's distances.

    Which of the two holds sits close to the tolerance on these profiles, and any change to what
    ``init`` writes moves every draw of the run, so the rule is pinned and not the outcome: the
    end-to-end tests pin that the bundle passes its own ``verify``, and the unit tests below pin
    the trial's decline with its numbers.
    """
    rated = (emit, column) in {
        (entry["emit"], entry["column"]) for entry in _block(directory).get("rates", [])
    }
    pointer = f"/tables/{emit}/calendar/"
    reasons = [reason for reason in _reasons(directory, pointer) if "trial run" in reason]
    if rated:
        assert reasons == [], reasons
        return
    assert reasons
    for reason in reasons:
        assert _TRIAL_DECLINED in reason and "distance " in reason and "tolerance 0.1" in reason
        assert "so its rates are not declared and its calendar line does not gate" in reason


def test_the_rates_of_the_orders_follow_their_trial(jc1: Path) -> None:
    _assert_rates_follow_the_trial(jc1, "shop_order", "ordered_at")


def test_the_bundle_of_a_plan_profile_passes_its_own_verify(
    journey_d: Path, tmp_path: Path
) -> None:
    result = _run_verified(journey_d, "journey-d", tmp_path)
    assert result.exit_code == 0, result.stderr
    assert result.artifacts["verify"]["calendar"]["met"] is True
    _assert_calendars_gate_only_where_rated(
        journey_d, result.artifacts["verify"]["calendar"]["lines"]
    )


def test_the_rates_of_the_plan_invoice_follow_their_trial(journey_d: Path) -> None:
    _assert_rates_follow_the_trial(journey_d, "plan_invoice", "issued_at")


# -- the trial, stood in for ------------------------------------------------------------------


def _trial_of(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exposure: Optional[List[Mapping[str, Any]]] = None,
    calendar: Optional[List[Mapping[str, Any]]] = None,
) -> None:
    """Make the trial's ``run`` return a run whose verify holds the given lines."""
    verify: Dict[str, Any] = {}
    if exposure is not None:
        verify["exposure"] = {"lines": exposure}
    if calendar is not None:
        verify["calendar"] = {"lines": calendar}
    result = StageResult(stage="run", exit_code=1, _artifacts={"verify": verify})
    monkeypatch.setattr(sys.modules["flux_engine.stages.run"], "run", lambda *a, **k: result)


def _calendar_line(outcome: str, **more: Any) -> Dict[str, Any]:
    histograms = [
        {"histogram": "month", "outcome": outcome, "distance": "0.1193", "tolerance": "0.1"},
        {"histogram": "dayOfWeek", "outcome": "within", "distance": "0.02", "tolerance": "0.1"},
    ]
    return {
        "line": "calendar:ordered_at",
        "emit": "shop_order",
        "outcome": outcome,
        "histograms": histograms,
        **more,
    }


_RATES = [{"emit": "shop_order", "column": "ordered_at"}]


def _refuse(
    within: List[Mapping[str, Any]], rates: List[Mapping[str, Any]]
) -> authoring._ExposuresDeclined:
    with pytest.raises(authoring._ExposuresDeclined) as declined:
        authoring._refuse_unless_reproduced([], within, rates, None, Path("profile.json"))
    return declined.value


def test_a_gated_calendar_line_outside_its_tolerance_declines_the_rates_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trial_of(monkeypatch, calendar=[_calendar_line("outside")])
    declined = _refuse([], _RATES)
    assert declined.reasons == {}
    reason = declined.rates[("shop_order", "ordered_at")]
    assert "month distance 0.1193, tolerance 0.1" in reason
    assert "dayOfWeek" not in reason
    assert reason.endswith("so its rates are not declared and its calendar line does not gate")


def test_a_shaped_column_the_trial_forms_no_calendar_line_for_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trial_of(monkeypatch, calendar=[])
    declined = _refuse([], _RATES)
    assert (
        "formed no gated calendar line for this column"
        in declined.rates[("shop_order", "ordered_at")]
    )


def test_a_calendar_line_that_does_not_gate_is_not_a_line_that_was_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trial_of(monkeypatch, calendar=[_calendar_line("outside", gated=False)])
    declined = _refuse([], _RATES)
    assert "formed no gated calendar line" in declined.rates[("shop_order", "ordered_at")]


def test_an_exposure_over_a_root_lifetime_the_trial_fails_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = {
        "line": "exposure:order_to_customer",
        "emit": "shop_order",
        "outcome": "outside",
        "children": 2354,
        "activeDays": 52217,
        "profileChildren": 2354,
        "profileActiveDays": 85713,
        "gap": "0.6415",
        "tolerance": "0.1",
    }
    _trial_of(monkeypatch, exposure=[line])
    within = [{"emit": "shop_order", "relationship": "order_to_customer", "exposure": True}]
    declined = _refuse(within, [])
    reason = declined.reasons[("shop_order", "order_to_customer")]
    assert "under the declared lifetimes: a trial run has children per active day" in reason
    assert "2354 over 52217 against the profile's 2354 over 85713, gap 0.6415" in reason
    assert "within its own parent's lifetime" not in reason


def test_every_failing_item_of_one_trial_is_declined_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _trial_of(monkeypatch, exposure=[], calendar=[_calendar_line("outside")])
    within = [{"emit": "shop_order", "relationship": "order_to_customer", "exposure": True}]
    declined = _refuse(within, _RATES)
    assert set(declined.reasons) == {("shop_order", "order_to_customer")}
    assert set(declined.rates) == {("shop_order", "ordered_at")}


def test_a_trial_that_reproduces_every_item_declines_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _trial_of(monkeypatch, calendar=[_calendar_line("within")])
    authoring._refuse_unless_reproduced([], [], _RATES, None, Path("profile.json"))


def test_a_bundle_with_no_exposure_and_no_rates_is_not_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no trial is made")

    monkeypatch.setattr(sys.modules["flux_engine.stages.run"], "run", refuse)
    authoring._refuse_unless_reproduced(
        [], [{"emit": "a", "relationship": "r"}], [], None, Path("p")
    )


def test_a_trial_that_cannot_be_made_declines_the_rates_entry_and_names_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = StageResult(
        stage="run", exit_code=1, stderr="FLX-3405: bad\nx", error_code="FLX-3405"
    )
    monkeypatch.setattr(sys.modules["flux_engine.stages.run"], "run", lambda *a, **k: refused)
    declined = _refuse([], _RATES)
    assert "could not be made (FLX-3405: bad)" in declined.rates[("shop_order", "ordered_at")]


def test_a_trial_whose_plan_holds_too_many_rows_is_unmade_and_declines_its_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_many(*args: Any, **kwargs: Any) -> StageResult:
        assert kwargs["most_rows"] == authoring._MOST_TRIAL_PLANNED_ROWS
        raise PlannedRowsAboveCeiling(100_000_000, kwargs["most_rows"])

    monkeypatch.setattr(sys.modules["flux_engine.stages.run"], "run", too_many)
    trial = authoring._trial_run([], Path("profile.json"), None)
    assert trial.unmade == "the trial would generate 100000000 rows, more than 60000"
    within = [{"emit": "shop_order", "relationship": "order_to_customer", "exposure": True}]
    declined = _refuse(within, _RATES)
    assert "the trial would generate 100000000 rows" in declined.rates[("shop_order", "ordered_at")]
    assert "the trial would generate" in declined.reasons[("shop_order", "order_to_customer")]


def test_passes_spent_on_trial_declines_end_with_a_message_that_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def decline(*args: Any, **kwargs: Any) -> None:
        raise authoring._ExposuresDeclined({}, {})

    monkeypatch.setattr(authoring, "_author_with_fallbacks", decline)
    with pytest.raises(authoring.AuthorError) as ended:
        authoring.author_bundle(authoring.AuthorInputs(Path("contract"), Path("profile")))
    message = str(ended.value)
    assert "trial runs did not settle which exposures, rates entries or within entries" in message
    assert f"within {authoring._MOST_WIDENINGS} passes" in message
    assert "cannot be widened" not in message


def test_passes_spent_on_widening_keep_the_widening_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def widen(*args: Any, **kwargs: Any) -> None:
        raise authoring._RangesWidened({("a", "b"): (1, 2)})

    monkeypatch.setattr(authoring, "_author_with_fallbacks", widen)
    with pytest.raises(authoring.AuthorError, match="cannot be widened"):
        authoring.author_bundle(authoring.AuthorInputs(Path("contract"), Path("profile")))


def test_a_profile_whose_history_would_derive_a_hundred_million_rows_is_written_quickly(
    tmp_path: Path,
) -> None:
    """The rows a history projection derives are bounded by the plan, not by the scale of the trial.

    The plan version's longest length and its latest end make the projection hold about a hundred
    million period rows. The trial is not run, so the bundle is written at once, and the ``rates``
    entry the trial would have checked is left out with the reason.
    """
    profile = json.loads((FIXTURES / "journey-d" / "profile.json").read_text(encoding="utf-8"))
    table = profile["tables"]["plan_version"]
    table["history"]["versionLength"]["bins"][3] = "3000000"
    table["columns"]["valid_to"]["metrics"]["max"]["value"] = "95000000000000"
    edited = tmp_path / "profile.json"
    edited.write_text(json.dumps(profile), encoding="utf-8")
    directory = tmp_path / "bundle"
    started = time.monotonic()
    result = run_init(
        str(directory),
        contract=str(FIXTURES / "journey-d" / "contract.fluid.yaml"),
        profile=str(edited),
    )
    elapsed = time.monotonic() - started
    assert result.exit_code == 0, result.stderr
    assert elapsed < 45
    assert (directory / "simulation.flux.yml").is_file()
    reasons = _reasons(directory, "/tables/plan_invoice/calendar/0/")
    assert reasons and all("the trial would generate" in reason for reason in reasons)
    assert "plan_invoice" not in {entry["emit"] for entry in _block(directory).get("rates", [])}
