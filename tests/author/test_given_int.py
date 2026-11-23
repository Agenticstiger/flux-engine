# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An INT with few values is a categorical over its integer levels, and a parent of ``given``.

``fluxsim init`` draws a column given another only when the other is categorical. An INT column
that the profile groups another column by, and publishes a ``levelHistogram/1`` or a
``support/1`` of over at most 64 whole numbers, is declared over those levels (canonical decimal
text), so the grouped column is drawn given it under the forest rules of categorical parents.
A number grouping that init cannot use stays listed as unused, with its reason.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.author.columns import MOST_INT_LEVELS, ColumnContext, few_int_levels
from flux_engine.author.forms import BY_NUMBER
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

from .test_given import _group, _metric, _statistic, _with_k

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: rating.shop
name: Rating shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: review
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/review.parquet }
    contract:
      schema:
        - { name: review_id, type: INT, required: true }
        - { name: rating, type: INT, required: true }
        - { name: verdict, type: "enum('keep','return')", required: true }
    semantics:
      entities:
        - { name: review, type: primary, expr: review_id }
"""
RATING = "/tables/review/columns/rating/statistics/0"
VERDICT = "/tables/review/columns/verdict/statistics/1"


def _profile(*, number_domain: bool = False, support_only: bool = False) -> Dict[str, Any]:
    levels = ["1", "2", "3"]
    if number_domain:
        # A number column publishes bins, never levels (PROFILE-SPEC.md section 3).
        rating_statistic = _statistic(
            "marginalHistogram/1",
            [_with_k(_group("", [300, 500, 200], "binCounts"), 1000)],
            bins=["1", "2", "3", "4"],
        )
    elif support_only:
        rating_statistic = _statistic(
            "support/1", [{"group": "", "k": 1000, "support": levels, "status": "F"}]
        )
    else:
        rating_statistic = _statistic(
            "levelHistogram/1",
            [_with_k(_group("", [300, 500, 200]), 1000)],
            levels=levels,
        )
    verdict_by_rating = _statistic(
        "levelHistogram/1",
        [
            _with_k(_group("1", [60, 240]), 300),
            _with_k(_group("2", [250, 250]), 500),
            _with_k(_group("3", [180, 20]), 200),
        ],
        levels=["keep", "return"],
        groupBy="rating",
    )
    columns: Dict[str, Any] = {
        "review_id": {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", 1000),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", "1000"),
            },
        },
        "rating": {
            "domain": "number" if number_domain else "category",
            "metrics": {"count": _metric("count/1", 1000)},
            "statistics": [rating_statistic],
        },
        "verdict": {
            "domain": "category",
            "metrics": {"count": _metric("count/1", 1000)},
            "statistics": [
                _statistic(
                    "levelHistogram/1",
                    [_with_k(_group("", [490, 510]), 1000)],
                    levels=["keep", "return"],
                ),
                verdict_by_rating,
            ],
        },
    }
    document: Dict[str, Any] = {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            }
        },
        "tables": {"review": {"rowCount": 1000, "columns": columns}},
    }
    parse_profile(copy.deepcopy(document))
    return document


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block["emits"]["review"]["fidelity"]


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


@pytest.mark.parametrize("support_only", [False, True])
def test_the_levels_are_declared_and_the_grouped_column_is_given_them(
    tmp_path: Path, support_only: bool
) -> None:
    result = _init(tmp_path, _profile(support_only=support_only))
    columns = _fidelity(tmp_path)["columns"]
    assert columns["rating"]["support"] == {"values": ["1", "2", "3"]}
    assert columns["rating"]["maxCardinality"] == 3
    assert "nullFraction" not in columns["rating"] and "given" not in columns["rating"]
    assert columns["verdict"]["given"] == "rating"
    assert columns["verdict"]["levelWeightsGiven"] == {
        "1": {"keep": "0.2", "return": "0.8"},
        "2": {"keep": "0.5", "return": "0.5"},
        "3": {"keep": "0.9", "return": "0.1"},
    }
    assert _states(result)[VERDICT]["state"] == "used"
    if not support_only:
        assert columns["rating"]["levels"] == {"1": "0.3", "2": "0.5", "3": "0.2"}
    grouped = [p for p in _fidelity(tmp_path)["properties"] if p.get("groupBy") == "rating"]
    assert [(p["property"], p["column"]) for p in grouped] == [("levelHistogram", "verdict")]


@pytest.mark.parametrize("seed", [42, 7])
def test_the_bundle_runs_and_the_run_matches_its_profile(tmp_path: Path, seed: int) -> None:
    _init(tmp_path, _profile())
    simulation = tmp_path / "bundle" / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8").replace("seed: 42", f"seed: {seed}"),
        encoding="utf-8",
    )
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr
    assert "joint" in outcome.stdout
    assert "3 of 3 declared properties within the margin" in outcome.stdout


def test_fluxsim_init_then_run_verify_passes_on_the_written_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.cli import main

    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    bundle = tmp_path / "bundle"
    code = main(
        [
            "init",
            str(bundle),
            "--contract",
            str(tmp_path / "shop.fluid.yaml"),
            "--profile",
            str(tmp_path / "shop.profile.json"),
        ]
    )
    assert code == 0, capsys.readouterr().err
    code = main(
        [
            "run",
            str(bundle),
            "--verify",
            "--profile",
            str(tmp_path / "shop.profile.json"),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "3 of 3 declared properties within the margin" in captured.out


def test_a_number_grouping_init_cannot_use_stays_unused_with_its_reason(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(number_domain=True))
    assert "given" not in _fidelity(tmp_path)["columns"]["verdict"]
    state = _states(result)[VERDICT]
    assert state["state"] == "unused" and state["note"] == BY_NUMBER


def _levels(*texts: str) -> Dict[str, Any]:
    group = {"group": "", "k": 100, "levelCounts": [10] * len(texts), "status": "F"}
    return {"statistics": [_statistic("levelHistogram/1", [group], levels=list(texts))]}


def test_only_whole_numbers_in_canonical_text_and_few_of_them_are_levels() -> None:
    assert few_int_levels(_levels("-1", "0", "10")) == ("-1", "0", "10")
    for bad in (["01", "2"], ["+1", "2"], ["1.5", "2"], ["-0", "1"], ["a", "b"]):
        assert few_int_levels(_levels(*bad)) is None
    many = [str(number) for number in range(MOST_INT_LEVELS + 1)]
    assert few_int_levels(_levels(*many)) is None
    assert few_int_levels(_levels(*many[:MOST_INT_LEVELS])) is not None
    assert few_int_levels(None) is None


def test_a_level_outside_the_contract_range_is_no_level() -> None:
    from flux_engine.author.ranges import parse_range

    rule = parse_range("between 1 and 5")
    assert few_int_levels(_levels("1", "5"), rule) == ("1", "5")
    assert few_int_levels(_levels("1", "6"), rule) is None
    assert few_int_levels(_levels("0", "1"), parse_range("> 0")) is None


def test_a_string_column_is_never_drawn_over_integer_levels() -> None:
    assert ColumnContext.__dataclass_fields__["int_levels"].default is False


def test_a_whole_number_reads_as_the_category_of_its_decimal_text() -> None:
    from flux_engine.concordance.decoded import _label
    from flux_stats.reading import category

    class _Column:
        name = "rating"

    assert category(7, _Column(), row_index=0) == "7"
    assert category(-3, _Column(), row_index=0) == "-3"
    assert category(True, _Column(), row_index=0) == "true"
    assert _label(12) == "12" and _label(False) == "false"
