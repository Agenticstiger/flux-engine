# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose children follow their parent's exposure (ADR 0025 sections 4 and 8).

``tests/fixtures/temporal`` is copied with ``lifetimes`` and ``within`` entries that carry
``exposure: true``: the accounts of a customer, and the invoices of an account. Every exposure here is
worked out again from the rows the run wrote, with ``datetime`` and not with the code under test. What
is pinned, on kernel 0.2.0:

* **Proportion.** Each parent has the children its exposure allots it, within the largest-remainder
  rounding of one; a parent never active inside the window has none; a TIMESTAMP child is counted in
  milliseconds.
* **A shape.** With a ``children`` shape the counts are ranked by exposure.
* **Rates.** A parent active in a month the column weighs heavily has more children than one active
  longer in a month it weighs lightly.
* **The plan.** The member binds into the child's digest only, and a bundle without ``exposure`` keeps
  every digest, with the hook registered and without it.
* **Refusals**, and ``--jobs 1`` and ``--jobs 4`` giving the same digests.
* **Verify.** ``exposure:<relationship>`` passes on a profile whose exposure matches, fails on one that
  is three times off, takes a ``facts`` tolerance, and is not scored on a suppressed count or a hidden
  table; a profile that publishes none adds nothing. The command line plans, applies and verifies a run.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator.counts import register_expected_count_hook
from flux_engine.generator.exposure import EXPOSURE_HOOK
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_3_0
from tests.fixtures.temporal.bundle import copy_uncut

from .test_verify_calendar import _MEANS, _merged, _written

pytestmark = pytest.mark.integration

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WINDOW_START, WINDOW_END = date(2025, 1, 1), date(2025, 12, 31)
#: The window of the fixture, in epoch milliseconds, as a profile states it.
WINDOW_MS = (1735689600000, 1767225599000)
JANUARY = ["20"] + ["1"] * 11
NAMESPACE = "  com.agentics.flux/1:\n"

LIFETIMES = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
"""
EXPOSED = LIFETIMES + """\
within:
  - { emit: account, relationship: customer, date: a_open, exposure: true }
  - { emit: invoice, relationship: account, date: i_issued, exposure: true }
"""
#: The same bundle with the entries and no ``exposure``.
PLAIN = EXPOSED.replace(", exposure: true", "")
SHAPED = EXPOSED + (
    "counts:\n"
    "  - { emit: account, relationship: customer, children: "
    "{ levels: ['0', '1', '4'], levelWeights: ['0.6', '0.2', '0.2'] } }\n"
)
#: Customers start in the first quarter and live 0 to 20 days or 20 to 300, and January weighs 20 to 1.
RATED = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '20', '300'], binWeights: ['1', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, date: a_open, exposure: true }
rates:
  - { emit: account, column: a_open, month: %s }
""" % JANUARY


def _bundle(
    root: Path,
    name: str,
    keys: str,
    *,
    customers: int = 60,
    accounts: int = 1500,
    invoices: int = 3000,
    first_quarter: bool = False,
) -> Path:
    """The fixture with *keys* and rows enough for counts per parent to follow exposures."""
    bundle = copy_uncut(root, name, keys)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    replacements = [
        ("rows: 60\n", f"rows: {customers}\n"),
        ("rows: 150\n", f"rows: {accounts}\n"),
        ("rows: 900\n", f"rows: {invoices}\n"),
    ]
    if first_quarter:
        replacements.append(
            (
                "cu_start: { range: { min: '2023-01-01', max: '2025-06-30' } }",
                "cu_start: { range: { min: '2025-01-01', max: '2025-03-31' } }",
            )
        )
    for old, new in replacements:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, "utf-8")
    return bundle


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    bundle = _bundle(tmp_path_factory.mktemp("exposure"), "bundle", EXPOSED)
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _rows(outcome: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    emitted = next(e for e in outcome.emits if e.resolved.expose_id == expose_id)
    return [dict(zip(emitted.expose.column_names, row, strict=True)) for row in emitted.rows]


# -- the lifetimes, worked out again by hand ------------------------------------------------------


def _days_in_window(
    start: date, end: Optional[date], weigh: Callable[[date], int] = lambda _: 1
) -> int:
    """The days of a lifetime inside the window, each weighed: an end that is NULL or before the start runs to the window's end."""
    last = WINDOW_END if end is None or end < start else min(end, WINDOW_END)
    first = max(start, WINDOW_START)
    return sum(weigh(first + timedelta(days=n)) for n in range((last - first).days + 1))


def _january(day: date) -> int:
    return 20 if day.month == 1 else 1


def _children(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return counts


def _assert_in_proportion(
    exposures: Dict[str, Fraction], children: Dict[str, int], total: int
) -> None:
    """Each parent has ``total * E / sum(E)`` children, to the one the largest remainder rounds by."""
    whole = sum(exposures.values())
    assert sum(children.values()) == total
    for parent, exposure in exposures.items():
        quota = Fraction(total) * exposure / whole
        assert abs(children.get(parent, 0) - quota) < 1, (parent, exposure, quota)


def _customer_exposures(
    outcome: pipeline.PipelineResult, weigh: Callable[[date], int] = lambda _: 1
) -> Dict[str, Fraction]:
    return {
        row["cu_id"]: Fraction(_days_in_window(row["cu_start"], row["cu_end"], weigh))
        for row in _rows(outcome, "customer")
    }


def test_accounts_follow_the_exposure_of_their_customer(result: pipeline.PipelineResult) -> None:
    exposures = _customer_exposures(result)
    accounts = _rows(result, "account")
    _assert_in_proportion(exposures, _children(accounts, "a_customer"), len(accounts))
    assert (
        len({e for e in exposures.values() if e}) > 5
    )  # the exposures differ, so the test says something


def test_a_customer_never_active_inside_the_window_has_no_accounts(
    result: pipeline.PipelineResult,
) -> None:
    exposures = _customer_exposures(result)
    children = _children(_rows(result, "account"), "a_customer")
    idle = [customer for customer, exposure in exposures.items() if exposure == 0]
    assert idle and len(idle) < len(exposures)
    assert all(children.get(customer, 0) == 0 for customer in idle)
    assert all(children.get(customer, 0) > 0 for customer, e in exposures.items() if e > 30)


def test_invoices_follow_the_exposure_of_their_account_in_milliseconds(
    result: pipeline.PipelineResult,
) -> None:
    """A TIMESTAMP child: the exposure is the part of a DATE lifetime inside the window, to the last millisecond."""
    first = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
    last = WINDOW_MS[1]
    exposures: Dict[str, Fraction] = {}
    for row in _rows(result, "account"):
        start = (
            int(datetime.combine(row["a_open"], datetime.min.time(), timezone.utc).timestamp())
            * 1000
        )
        close = row["a_close"]
        end: Optional[int] = None
        if close is not None and close >= row["a_open"]:
            end = (
                int(datetime.combine(close, datetime.min.time(), timezone.utc).timestamp()) * 1000
                + 86_400_000
                - 1
            )
        low, high = max(start, first), last if end is None else min(end, last)
        exposures[row["a_id"]] = Fraction(max(high - low + 1, 0), 86_400_000)
    invoices = _rows(result, "invoice")
    _assert_in_proportion(exposures, _children(invoices, "i_account"), len(invoices))


def test_each_child_is_still_dated_inside_its_parents_lifetime(
    result: pipeline.PipelineResult,
) -> None:
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    for account in _rows(result, "account"):
        parent = customers[account["a_customer"]]
        assert parent["cu_start"] <= account["a_open"]
        if parent["cu_end"] is not None and parent["cu_end"] >= parent["cu_start"]:
            assert account["a_open"] <= parent["cu_end"]


# -- a children shape -----------------------------------------------------------------------------


def test_with_a_children_shape_the_counts_are_ranked_by_exposure(tmp_path: Path) -> None:
    shaped = pipeline.execute(_bundle(tmp_path, "shaped", SHAPED), kernel=KERNEL_V2)
    exposures = _customer_exposures(shaped)
    accounts = _rows(shaped, "account")
    children = _children(accounts, "a_customer")
    assert sum(children.values()) == len(accounts)
    ranked = sorted(exposures, key=lambda customer: exposures[customer])
    counts = [children.get(customer, 0) for customer in ranked]
    for low, high in zip(ranked, ranked[1:], strict=False):
        if exposures[low] < exposures[high]:
            assert children.get(low, 0) <= children.get(high, 0)
    # customers of equal exposure rank in any order, so the last of them need not be the one with children
    assert counts[0] == 0 and max(counts) > 0
    # the zero-exposure customers rank lowest and take the level 0 slots
    assert all(children.get(c, 0) == 0 for c, e in exposures.items() if e == 0)
    assert len({count for count in counts}) >= 3  # the three levels of the shape, as allotted


# -- rates ---------------------------------------------------------------------------------------


def test_with_rates_a_parent_active_in_a_heavy_month_has_more_children(tmp_path: Path) -> None:
    rated = pipeline.execute(
        _bundle(tmp_path, "rated", RATED, first_quarter=True), kernel=KERNEL_V2
    )
    customers = _rows(rated, "customer")
    exposures = _customer_exposures(rated, _january)
    accounts = _rows(rated, "account")
    _assert_in_proportion(exposures, _children(accounts, "a_customer"), len(accounts))
    children = _children(accounts, "a_customer")

    def january_only(row: Dict[str, Any]) -> bool:
        return (
            row["cu_start"].month == 1
            and row["cu_end"] is not None
            and row["cu_end"] >= row["cu_start"]
            and row["cu_end"].month == 1
        )

    heavy = [r for r in customers if january_only(r) and (r["cu_end"] - r["cu_start"]).days >= 10]
    light = [
        r
        for r in customers
        if r["cu_start"].month > 1
        and _days_in_window(r["cu_start"], r["cu_end"]) <= 100
        and r["cu_end"] is not None
        and r["cu_end"] >= r["cu_start"]
    ]
    assert heavy and light
    # at least ten days at twenty to one outweigh at most a hundred days at one to one
    assert min(children.get(r["cu_id"], 0) for r in heavy) > max(
        children.get(r["cu_id"], 0) for r in light
    )
    longer = max(light, key=lambda r: _days_in_window(r["cu_start"], r["cu_end"]))
    shorter = min(heavy, key=lambda r: _days_in_window(r["cu_start"], r["cu_end"]))
    assert _days_in_window(longer["cu_start"], longer["cu_end"]) > _days_in_window(
        shorter["cu_start"], shorter["cu_end"]
    )


# -- the plan ------------------------------------------------------------------------------------


def _digests(outcome: pipeline.PipelineResult) -> Dict[str, str]:
    return {
        e.resolved.expose_id: e.plan.digest().value for e in outcome.emits if e.plan is not None
    }


def test_the_member_binds_into_the_child_plan_and_the_parents_digest_is_the_childs_input(
    tmp_path: Path, result: pipeline.PipelineResult
) -> None:
    plain = pipeline.execute(_bundle(tmp_path, "plain", PLAIN), kernel=KERNEL_V2)
    exposed, bare = _digests(result), _digests(plain)
    assert exposed["account"] != bare["account"] and exposed["invoice"] != bare["invoice"]
    # the customer is read by nothing that moved; the payment reads the invoice's plan, which moved
    assert exposed["customer"] == bare["customer"] and exposed["payment"] != bare["payment"]
    document = next(e for e in result.emits if e.resolved.expose_id == "invoice").plan.as_document()
    member = document["relations"]["i_account"]["counts"]["expectedCount"]
    assert member == {
        "by": "exposure",
        "relationship": "account",
        "column": "i_issued",
        "start": "a_open",
        "end": "a_close",
    }
    bare_doc = next(e for e in plain.emits if e.resolved.expose_id == "invoice").plan.as_document()
    assert "counts" not in bare_doc["relations"]["i_account"]


def test_the_rates_of_the_child_column_are_part_of_the_member(tmp_path: Path) -> None:
    with_rates = pipeline.execute(
        _bundle(tmp_path, "r1", RATED, first_quarter=True), kernel=KERNEL_V2
    )
    other = RATED.replace("['20',", "['30',")
    changed = pipeline.execute(_bundle(tmp_path, "r2", other, first_quarter=True), kernel=KERNEL_V2)
    first = next(
        e for e in with_rates.emits if e.resolved.expose_id == "account"
    ).plan.as_document()
    second = next(e for e in changed.emits if e.resolved.expose_id == "account").plan.as_document()
    member = first["relations"]["a_customer"]["counts"]["expectedCount"]
    assert member["rates"]["month"][0] == "20"
    assert second["relations"]["a_customer"]["counts"]["expectedCount"]["rates"]["month"][0] == "30"


def test_a_bundle_without_exposure_keeps_every_digest_with_the_hook_registered_or_not(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "plain", PLAIN)
    registered = pipeline.execute(bundle, kernel=KERNEL_V2)
    unregister = register_expected_count_hook(EXPOSURE_HOOK)  # idempotent: the same object
    unregister()
    try:
        without = pipeline.execute(bundle, kernel=KERNEL_V2)
    finally:
        register_expected_count_hook(EXPOSURE_HOOK)
    assert _digests(registered) == _digests(without)
    assert [e.digests.h.value for e in registered.emits] == [
        e.digests.h.value for e in without.emits
    ]


# -- the refusals --------------------------------------------------------------------------------


def _refusal(bundle: Path, kernel: Any = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle, kernel=kernel)
    return refused.value


def test_exposure_false_is_refused(tmp_path: Path) -> None:
    keys = EXPOSED.replace("i_issued, exposure: true", "i_issued, exposure: false")
    refused = _refusal(_bundle(tmp_path, "false", keys))
    assert refused.code.code == "FLX-3105" and "exposure is true, or absent" in str(refused)


def test_exposure_on_a_chain_of_relationships_is_refused(tmp_path: Path) -> None:
    keys = (
        EXPOSED
        + "  - { emit: invoice, relationship: account.customer, date: i_issued, exposure: true }\n"
    )
    refused = _refusal(_bundle(tmp_path, "chain", keys))
    assert refused.code.code == "FLX-3105" and "a chain of relationships" in str(refused)


def test_exposure_on_a_parent_with_no_lifetime_is_refused(tmp_path: Path) -> None:
    keys = EXPOSED.replace(
        "  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }\n",
        "",
    )
    refused = _refusal(_bundle(tmp_path, "nolife", keys))
    assert refused.code.code == "FLX-3105" and "declares no lifetime" in str(refused)


def test_a_second_exposure_entry_for_one_relationship_is_refused(tmp_path: Path) -> None:
    keys = (
        EXPOSED + "  - { emit: account, relationship: customer, date: a_close, exposure: true }\n"
    )
    refused = _refusal(_bundle(tmp_path, "twice", keys))
    assert refused.code.code == "FLX-3105" and "second exposure entry" in str(refused)


def test_kernel_0_1_0_refuses_the_key_naming_the_kernel(tmp_path: Path) -> None:
    refused = _refusal(_bundle(tmp_path, "v1", EXPOSED), KERNEL_V1)
    assert refused.code.code == "FLX-3105" and "kernel 0.1.0" in str(refused)


def test_exposure_without_a_window_is_refused_by_the_hook(result: pipeline.PipelineResult) -> None:
    invoice = next(e for e in result.emits if e.resolved.expose_id == "invoice")
    assert invoice.plan is not None
    namespace = {
        "within": [
            {"emit": "invoice", "relationship": "account", "date": "i_issued", "exposure": True}
        ]
    }
    with pytest.raises(FluxError) as refused:
        EXPOSURE_HOOK.member(namespace, replace(invoice.plan, time_bounds=None), "account")
    assert refused.value.code.code == "FLX-3105" and "timeBounds" in str(refused.value)
    assert EXPOSURE_HOOK.member({}, invoice.plan, "account") is None
    assert EXPOSURE_HOOK.member(namespace, invoice.plan, "other") is None


# -- jobs ----------------------------------------------------------------------------------------


def _loop(bundle: Path, state: Path, jobs: int) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state, jobs=jobs, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True, jobs=jobs
    )
    assert verified.success, verified.stderr
    return {
        "layout": layout,
        "plan": planned.artifacts.get("plan_digest"),
        "digests": {
            k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"
        },
    }


@pytest.mark.slow
def test_jobs_one_and_four_give_the_same_digests(tmp_path: Path) -> None:
    """Each worker works the layout out from the plan it was sent, which carries the hook and its declarations."""
    rows = 2 * parallel.MIN_ROWS_PER_JOB + 301
    big = _bundle(tmp_path, "big", EXPOSED, accounts=rows, invoices=rows)
    runs = [_loop(big, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for expose_id in ("account", "invoice"):
        first = runs[0]["layout"].emit_path(expose_id).read_text("utf-8")
        assert len(first.splitlines()) == rows
        assert first == runs[1]["layout"].emit_path(expose_id).read_text("utf-8")


# -- verify --------------------------------------------------------------------------------------


def _whole_days(start: str, end: Optional[str]) -> int:
    return _days_in_window(
        date.fromisoformat(start), None if end is None else date.fromisoformat(end)
    )


def _profile(
    document: Dict[str, Any], children: Optional[int], days: Optional[int]
) -> Dict[str, Any]:
    def count(value: Optional[int]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "definition": "count/1",
            "status": "F" if value is not None else "A",
            "sampleSize": {"min": 1, "max": 10**9},
            "disclosure": "none",
        }
        if value is not None:
            entry["value"] = value
        return entry

    document["relationships"] = [
        {
            "parent": {"table": "customer", "columns": ["cu_id"]},
            "child": {"table": "account", "columns": ["a_customer"]},
            "statistics": [],
            "exposure": [
                {
                    "start": "cu_start",
                    "end": "cu_end",
                    "children": count(children),
                    "activeDays": count(days),
                    "window": {"start": str(WINDOW_MS[0]), "end": str(WINDOW_MS[1])},
                }
            ],
        }
    ]
    return document


#: What a run of the bundle with the given keys wrote, measured once: the active days of the customers, the
#: number of accounts, and the count and sum of each mean's column. Every ``Pinned`` of those keys reads
#: the same numbers, since the run that gives them is the same run each time.
_MEASURED: Dict[str, Tuple[int, int, Dict[str, Tuple[int, int]]]] = {}


def _measured(keys: str) -> Tuple[int, int, Dict[str, Tuple[int, int]]]:
    if keys not in _MEASURED:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            applied = apply(_bundle(root, "unpinned", keys), state_dir=root / "state-plain")
            assert applied.success, applied.stderr
            layout = RunLayout.for_state_dir(root / "state-plain", str(applied.artifacts["run_id"]))
            starts, ends = _written(layout, "customer", "cu_start"), _written(
                layout, "customer", "cu_end"
            )
            days = sum(_whole_days(s, e) for s, e in zip(starts, ends, strict=True))
            means = {
                name: (len(cells), sum(cells))
                for name, column in _MEANS.items()
                for cells in [_written(layout, name, column)]
            }
            _MEASURED[keys] = (days, len(_written(layout, "account", "a_id")), means)
    return _MEASURED[keys]


class Pinned:
    """The fixture applied under a profile whose exposure is the run's own, scaled by a test."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        keys: str = EXPOSED,
        children_scale: Fraction = Fraction(1),
        suppress: bool = False,
        hidden_customer: bool = False,
        with_exposure: bool = True,
    ) -> None:
        self.days, self.children, means = _measured(keys)
        tables = {
            "customer": {
                "columns": {
                    "cu_id": {"domain": "category"},
                    "cu_start": {"domain": "timestamp"},
                    "cu_end": {"domain": "timestamp"},
                }
            },
            "account": {"columns": {"a_customer": {"domain": "category"}}},
        }
        document: Dict[str, Any] = {
            "_producer": "urn:example:test-profiler",
            "_schemaURL": SCHEMA_URL_1_3_0,
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "disclosureRules": {"none": {}},
            "tables": _merged(tables, means),
        }
        if with_exposure:
            scaled = int(self.children * children_scale)
            _profile(document, None if suppress else scaled, self.days)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.profile = tmp_path / "profile.json"
        self.profile.write_text(json.dumps(document), "utf-8")
        self.bundle = _bundle(tmp_path, "pinned", keys)
        simulation = self.bundle / "simulation.flux.yml"
        pin = digest_of_document(document, subject="profile").value
        text = simulation.read_text("utf-8").replace(
            NAMESPACE, f"{NAMESPACE}    profile:\n      digest: {pin}\n", 1
        )
        if hidden_customer:
            text = text.replace("      customer:\n", "      customer:\n        hidden: true\n", 1)
        simulation.write_text(text, "utf-8")
        self.state = tmp_path / "state"
        applied = apply(self.bundle, state_dir=self.state, profile=self.profile)
        assert applied.success, applied.stderr
        self.layout = RunLayout.for_state_dir(self.state, str(applied.artifacts["run_id"]))
        self.run_id = self.layout.run_id

    def verify(self) -> Any:
        return run_verify(
            self.bundle,
            run_id=self.run_id,
            state_dir=self.state,
            profile=self.profile,
            allow_declared_baseline_drift=True,
        )


def test_a_run_that_matches_its_profiles_exposure_passes_the_line(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path)
    result = pinned.verify()
    assert result.success, result.stderr
    document = result.artifacts["exposure"]
    assert document["met"] is True
    (line,) = document["lines"]
    assert (line["line"], line["emit"], line["outcome"]) == (
        "exposure:customer",
        "account",
        "within",
    )
    # the run's counts are the ones counted by hand, and the line says them
    assert line["children"] == pinned.children and line["activeDays"] == pinned.days
    assert line["profileChildren"] == pinned.children and line["gap"] == "0"
    assert "  exposure:customer (account): passed -- children per active day" in result.stdout


def test_a_profile_three_times_off_fails_the_line(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, children_scale=Fraction(3))
    result = pinned.verify()
    assert not result.success
    (line,) = result.artifacts["exposure"]["lines"]
    assert line["outcome"] == "outside"
    assert Fraction(line["gap"]) == Fraction(2, 3)  # |1 - 3| / 3 of the profile's rate
    assert result.artifacts["exposure"]["met"] is False
    assert "exposure:customer (account): failed" in result.stderr


def test_ship_withholds_a_run_whose_exposure_line_verify_fails_and_names_it(
    tmp_path: Path,
) -> None:
    pinned = Pinned(tmp_path, children_scale=Fraction(3))
    assert not pinned.verify().success
    shipped = ship(
        pinned.bundle,
        run_id=pinned.run_id,
        out_dir=tmp_path / "out",
        state_dir=pinned.state,
        profile=pinned.profile,
    )
    assert not shipped.success
    assert shipped.error_code == "FLX-8001"
    assert "run.exposure: exposure:customer (account): failed" in shipped.stdout
    assert shipped.artifacts["scorecard"]["met"] is False
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_ship_refuses_a_bundle_that_is_not_the_runs_as_verify_does(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path)
    assert pinned.verify().success
    document = pinned.bundle / "simulation.flux.yml"
    document.write_text(document.read_text() + "# moved\n")
    assert pinned.verify().error_code == "FLX-2101"
    shipped = ship(
        pinned.bundle,
        run_id=pinned.run_id,
        out_dir=tmp_path / "out",
        state_dir=pinned.state,
        profile=pinned.profile,
    )
    assert not shipped.success
    assert shipped.exit_code == 1
    assert shipped.error_code == "FLX-2101"
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_ship_packs_a_run_whose_exposure_line_verify_passes(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path)
    assert pinned.verify().success
    shipped = ship(
        pinned.bundle,
        run_id=pinned.run_id,
        out_dir=tmp_path / "out",
        state_dir=pinned.state,
        profile=pinned.profile,
    )
    assert shipped.success, shipped.stderr


def test_a_facts_entry_names_the_line_and_gives_its_tolerance(tmp_path: Path) -> None:
    keys = EXPOSED + "facts:\n  - { emit: account, rule: 'exposure:customer', tolerance: '0.5' }\n"
    off = Pinned(tmp_path / "a", keys=keys, children_scale=Fraction(5, 4))
    assert off.verify().success  # a fifth off the profile's rate, inside 50 %
    tight = (
        EXPOSED + "facts:\n  - { emit: account, rule: 'exposure:customer', tolerance: '0.01' }\n"
    )
    close = Pinned(tmp_path / "b", keys=tight, children_scale=Fraction(19, 20))
    result = close.verify()
    assert not result.success
    assert result.artifacts["exposure"]["lines"][0]["tolerance"] == "0.01"
    # without an entry the tolerance is ten percent, and 5 % off passes
    assert Pinned(tmp_path / "c", children_scale=Fraction(19, 20)).verify().success


def test_a_suppressed_count_is_not_scored(tmp_path: Path) -> None:
    result = Pinned(tmp_path, suppress=True).verify()
    assert result.success, result.stderr
    document = result.artifacts["exposure"]
    assert document["lines"] == []
    assert document["notScored"] == [
        {
            "fact": "exposure:customer of account",
            "reason": "the profile withheld the count of children or of active days",
        }
    ]
    assert "  facts: not scored -- exposure:customer of account:" in result.stdout


def test_a_hidden_table_is_not_scored(tmp_path: Path) -> None:
    pinned = Pinned(tmp_path, hidden_customer=True)
    result = pinned.verify()
    assert result.success, result.stderr
    (unscored,) = result.artifacts["exposure"]["notScored"]
    assert unscored["fact"] == "exposure:customer of account"
    assert "the hidden table customer" in unscored["reason"]
    assert result.artifacts["exposure"]["lines"] == []


def test_a_profile_that_publishes_no_exposure_verifies_as_it_did(tmp_path: Path) -> None:
    result = Pinned(tmp_path, with_exposure=False).verify()
    assert result.success, result.stderr
    assert "exposure" not in result.artifacts and "exposure:" not in result.stdout


def test_a_bundle_without_exposure_entries_makes_no_line_from_a_published_exposure(
    tmp_path: Path,
) -> None:
    result = Pinned(tmp_path, keys=PLAIN).verify()
    assert result.success, result.stderr
    assert "exposure" not in result.artifacts


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_run_with_exposure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = Pinned(tmp_path)
    state = str(tmp_path / ".flux-cli")
    profile = str(pinned.profile)
    bundle = str(pinned.bundle)
    assert (
        main(
            ["-j", "plan", bundle, "--state-dir", state, "--kernel", "0.2.0", "--profile", profile]
        )
        == 0
    )
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", bundle, "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    layout = RunLayout.for_state_dir(state, run_id)
    accounts = layout.emit_path("account").read_text("utf-8").splitlines()
    assert len(accounts) == pinned.children
    args = ["-j", "verify", bundle, "--state-dir", state, "--run-id", run_id, "--profile", profile]
    assert main([*args, "--allow-declared-baseline-drift"]) == 0
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    assert artifacts["exposure"]["met"] is True
    assert [line["line"] for line in artifacts["exposure"]["lines"]] == ["exposure:customer"]
