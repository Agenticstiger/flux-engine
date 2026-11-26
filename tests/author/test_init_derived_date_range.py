# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` draws a date the profile gives no distribution where its contract derives hold.

ADR 0014, amendment of 2026-11-26: a DATE column the profile withholds (or whose released
histogram is noise everywhere) is drawn evenly within the run's window. When a contract ``derive``
over it is a whole-unit date difference from a constant date, and the derived column declares a
range, the draw is bounded to the dates whose difference lies in that range (the engine's own
``MONTHS_BETWEEN`` and ``DAYS_BETWEEN`` decide), cut to the column's own declared range, and
declared as one bin with weight 1 labelled as an assumption. Two derives over the column
intersect; an empty intersection keeps the even draw and says why. A column the profile
publishes, a derived column with no declared range and a derive of another shape change nothing.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.expr2 import days_between, months_between
from flux_engine.stages.run import run

sys.path.insert(0, str(Path(__file__).parent))
import test_init_noise_floor as noise  # noqa: E402  (the library world this test builds on)

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
DAY = date(2020, 1, 1)

#: A fee a member owes in a month: the month's index counts from 2020-01.
FEE = """\
  - exposeId: fee
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/fee.parquet }}
    contract:
      schema:
        - {{ name: fee_id, type: INT, required: true }}
        - {{ name: member_id, type: INT, required: true }}
        - name: month_index
          type: INT
          required: true
{index_rules}
        - name: fee_month
          type: DATE
          required: true
{month_rules}
{second}      invariants:
        - id: fee_month_index
          derive: month_index
          expr: "{expr}"
{second_rule}    semantics:
      defaultAggTimeDimension: fee_month
      entities:
        - {{ name: fee, type: primary, expr: fee_id }}
      dimensions:
        - {{ name: fee_month, type: time, typeParams: {{ timeGranularity: month }} }}
      relationships:
        - {{ name: member, to: member, fromColumns: [member_id], toColumns: [member_id], cardinality: many_to_one, joinType: inner }}
"""

BETWEEN_0_11 = '          validationRules: [{ type: range, constraint: "between 0 and 11" }]'
FORWARD = "MONTHS_BETWEEN(DATE '2020-01-01', fee_month)"
SWAPPED = "MONTHS_BETWEEN(fee_month, DATE '2020-01-01')"


def _contract(
    expr: str = FORWARD,
    index_rules: str = BETWEEN_0_11,
    month_rules: str = "",
    second: Optional[Tuple[str, str, str]] = None,
) -> str:
    """The library contract with a fee table; *second* is an extra column, its rules and rule text."""
    extra, extra_rule = "", ""
    if second is not None:
        name, rules, rule = second
        extra = f"        - name: {name}\n          type: INT\n          required: true\n{rules}\n"
        extra_rule = (
            f'        - id: {name}_rule\n          derive: {name}\n          expr: "{rule}"\n'
        )
    return noise.CONTRACT + FEE.format(
        expr=expr,
        index_rules=index_rules,
        month_rules=month_rules,
        second=extra,
        second_rule=extra_rule,
    )


def _profile(fee_month: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The noise-floor world's profile, and a fee table whose date column is withheld."""
    document = noise._profile(
        ["10", "12", "9", "11", "13", "10", "9", "12", "11", "10", "12", "11"], None
    )
    document["tables"]["fee"] = {
        "rowCount": 400,
        "columns": {
            "fee_id": {"domain": "number"},
            "member_id": {"domain": "number"},
            "month_index": {"domain": "number"},
            "fee_month": fee_month if fee_month is not None else {"domain": "timestamp"},
        },
    }
    return document


def _bundle(
    tmp_path: Path,
    contract: str,
    profile: Optional[Dict[str, Any]] = None,
    directory: str = "bundle",
) -> Any:
    (tmp_path / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(profile or _profile()), encoding="utf-8")
    result = run_init(
        str(tmp_path / directory),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    return dict(yaml.safe_load(text)["extensions"][NAMESPACE])


def _fee_month(tmp_path: Path) -> Optional[Dict[str, Any]]:
    column = _block(tmp_path)["emits"]["fee"]["fidelity"]["columns"].get("fee_month")
    return None if column is None else dict(column)


def _rows(tmp_path: Path) -> List[Tuple[int, date]]:
    """Each fee row's ``month_index`` and ``fee_month`` from a run."""
    outcome = run(tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "p.json")
    assert outcome.success, outcome.stderr
    (stream,) = (tmp_path / "state").glob("runs/*/emits/fee.ndjson")
    found = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line]
    return [(row[2], date.fromisoformat(row[3])) for row in found]


def _bundle_files(directory: Path) -> Dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_a_withheld_date_stays_inside_the_range_of_its_derive(tmp_path: Path) -> None:
    result = _bundle(tmp_path, _contract())
    assert _fee_month(tmp_path) == {"bins": ["2019-12-02", "2020-12-31"], "binWeights": ["1"]}
    rows = _rows(tmp_path)
    assert len(rows) == 400
    for index, month in rows:
        assert 0 <= index <= 11
        assert months_between(DAY, month) == index
        assert date(2019, 12, 2) <= month <= date(2020, 12, 31)
    assert "fee_month_index" in result.stdout and "between 0 and 11" in result.stdout


def test_verify_finds_no_row_outside_the_declared_range(tmp_path: Path) -> None:
    _bundle(tmp_path, _contract())
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "p.json", verify=True
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    assert not re.search(r"outside|out of range", outcome.stdout + outcome.stderr, re.IGNORECASE)


def test_the_swapped_argument_order_is_bounded_too(tmp_path: Path) -> None:
    _bundle(tmp_path, _contract(SWAPPED))
    # Whole months from the date to 2020-01-01: 0 at 2020-01-01, 11 down to 2019-01-02.
    assert _fee_month(tmp_path) == {"bins": ["2019-01-02", "2020-01-31"], "binWeights": ["1"]}
    for index, month in _rows(tmp_path):
        assert 0 <= index <= 11
        assert months_between(month, DAY) == index


def test_days_between_is_bounded_in_days(tmp_path: Path) -> None:
    contract = _contract(
        "DAYS_BETWEEN(DATE '2020-01-01', fee_month)",
        '          validationRules: [{ type: range, constraint: ">= 0 and < 31" }]',
    )
    _bundle(tmp_path, contract)
    assert _fee_month(tmp_path) == {"bins": ["2020-01-01", "2020-01-31"], "binWeights": ["1"]}
    for index, month in _rows(tmp_path):
        assert 0 <= index < 31 and days_between(DAY, month) == index


def test_a_range_outside_the_run_window_decides(tmp_path: Path) -> None:
    result = _bundle(tmp_path, _contract("MONTHS_BETWEEN(DATE '2040-01-01', fee_month)"))
    # The run covers 2015 to 2027; the contract's range holds 2040 only, and it decides.
    assert _fee_month(tmp_path) == {"bins": ["2039-12-02", "2040-12-31"], "binWeights": ["1"]}
    assert "run's window" in result.stdout
    for index, month in _rows(tmp_path):
        assert 0 <= index <= 11 and months_between(date(2040, 1, 1), month) == index


def test_a_contract_range_on_the_date_cuts_the_preimage(tmp_path: Path) -> None:
    month_rules = '          validationRules: [{ type: range, constraint: "between 2020-03-01 and 2020-06-30" }]'
    _bundle(tmp_path, _contract(month_rules=month_rules))
    column = _fee_month(tmp_path)
    assert column is not None
    assert column["bins"][0] >= "2020-03-01" and column["bins"][-1] <= "2020-07-01"
    for index, month in _rows(tmp_path):
        assert 2 <= index <= 5 and date(2020, 3, 1) <= month <= date(2020, 6, 30)


def test_two_derives_over_the_date_intersect(tmp_path: Path) -> None:
    second = (
        "day_index",
        '          validationRules: [{ type: range, constraint: "between 0 and 99" }]',
        "DAYS_BETWEEN(DATE '2020-01-01', fee_month)",
    )
    _bundle(tmp_path, _contract(second=second))
    assert _fee_month(tmp_path) == {"bins": ["2020-01-01", "2020-04-09"], "binWeights": ["1"]}
    for index, month in _rows(tmp_path):
        assert 0 <= index <= 3 and date(2020, 1, 1) <= month <= date(2020, 4, 9)


def test_an_empty_intersection_keeps_the_even_draw_and_says_why(tmp_path: Path) -> None:
    second = (
        "late_index",
        '          validationRules: [{ type: range, constraint: "between 0 and 11" }]',
        "MONTHS_BETWEEN(DATE '2030-01-01', fee_month)",
    )
    result = _bundle(tmp_path, _contract(second=second))
    assert _fee_month(tmp_path) is None
    assert (
        "fee_month" in result.stdout
        and "no date is inside the range of every derive" in result.stdout
    )


def _as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: str, profile: Dict[str, Any]
) -> None:
    """Both bundles of one input, with and without the bound, are the same bytes."""
    _bundle(tmp_path, contract, profile)
    bounded = _bundle_files(tmp_path / "bundle")
    with monkeypatch.context() as patch:
        patch.setattr("flux_engine.author.bundle.bound_derived_dates", lambda *a, **k: None)
        _bundle(tmp_path, contract, profile, "before")
    assert _bundle_files(tmp_path / "before") == bounded


def test_no_declared_range_leaves_the_bundle_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_before(tmp_path, monkeypatch, _contract(index_rules=""), _profile())
    assert _fee_month(tmp_path) is None


def test_a_published_date_is_not_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    published = _profile()["tables"]["member"]["columns"]["joined_on"]
    _as_before(tmp_path, monkeypatch, _contract(), _profile(published))
    column = _fee_month(tmp_path)
    assert column is not None and column["bins"][0] == "2015-01-01"
    assert len(column["binWeights"]) == 12


def test_a_derive_of_another_shape_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract("MONTHS_BETWEEN(DATE '2020-01-01', fee_month) + 1")
    _as_before(tmp_path, monkeypatch, contract, _profile())
    assert _fee_month(tmp_path) is None


def _joined_contract() -> str:
    """The members' contract with the month each joined counted from 2020-01, which must be 0 to 11."""
    contract = noise.CONTRACT.replace(
        "        - { name: joined_on, type: DATE, required: true }\n",
        "        - { name: joined_on, type: DATE, required: true }\n"
        "        - name: joined_month\n"
        "          type: INT\n"
        "          required: true\n" + BETWEEN_0_11 + "\n      invariants:\n"
        "        - id: joined_month_index\n"
        "          derive: joined_month\n"
        "          expr: \"MONTHS_BETWEEN(DATE '2020-01-01', joined_on)\"\n",
    )
    assert "joined_month_index" in contract
    return contract


def test_a_histogram_that_is_noise_everywhere_is_bounded_too(tmp_path: Path) -> None:
    counts = ["3", "-2", "5", "0", "-4", "6", "1", "-1", "2", "0", "4", "-3"]
    profile = noise._profile(counts, "50")
    columns = profile["tables"]["member"]["columns"]
    columns["joined_month"] = {"domain": "number"}
    (tmp_path / "c.fluid.yaml").write_text(_joined_contract(), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.json"),
    )
    assert result.exit_code == 0, result.stderr
    emits = _block(tmp_path)["emits"]["member"]["fidelity"]
    column = emits["columns"]["joined_on"]
    assert column["bins"] == ["2019-12-02", "2020-12-31"] and column["binWeights"] == ["1"]
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "p.json", verify=True
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (stream,) = (tmp_path / "state").glob("runs/*/emits/member.ndjson")
    for line in stream.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        assert 0 <= row[3] <= 11
