# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A statistic the ledger calls unused changes nothing the bundle holds (ADR 0014 section 1).

A data owner who releases only the statistics the ledger calls used must get the bundle the
whole profile gave. So, for each profile below, the bundle is written, and then once for each
statistic the ledger lists as unused, with that statistic removed from the profile; the
Simulation document must come out the same.

What is compared is the whole document less the two parts that name the profile by
construction: ``extensions[namespace].profile`` (the profile's digest) and
``extensions[namespace].unused`` (the list of unused statistics, which loses the removed one).
Every emit, declaration, property, tolerance, row count, the time window and the register's
numbers and their citations are compared, so a rule that read a statistic without saying so
shows as a different bundle. A removal the profile cannot do without (the document is
refused, or a statistic whose absence changes how the document is read, such as a row count)
is not a statistic the bundle ignored, and is skipped by pointer, with the reason, in
``SKIPPED``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.author import columns
from flux_engine.cli._init import run_init
from tests.author import test_children, test_ledger
from tests.author.test_binned_quantiles import _DATE_LADDER, _binned
from tests.author.test_columns import _metric as _column_metric

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"

#: Pointers a removal cannot be judged at, by scenario, each with why.
SKIPPED: Dict[str, Dict[str, str]] = {}

_DATE_TABLE = "/tables/orders/columns/placed_on"


def _ledger_scenario() -> Tuple[str, Dict[str, Any]]:
    return test_ledger.CONTRACT, test_ledger._profile()


def _children_scenario() -> Tuple[str, Dict[str, Any]]:
    child = {
        "domain": "number",
        "metrics": {"count": test_children._metric("count/1", 5000)},
        "statistics": [test_children._quantile(test_children.LADDER, 2, 2000, 4000)],
    }
    profile = test_children._profile(
        [
            test_children._histogram(test_children.BINS, test_children.PARENTS, 1000),
            test_children._mean(5000, 1000),
        ],
        child=child,
    )
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    return test_children.CONTRACT, profile


def _dated(maximum: str) -> Tuple[str, Dict[str, Any]]:
    """The ledger shop with a DATE column over two binned quantiles, a withheld min and a max."""
    contract = test_ledger.CONTRACT.replace(
        "        - { name: amount, type: INT, required: true }\n",
        "        - { name: amount, type: INT, required: true }\n"
        "        - { name: placed_on, type: DATE, required: true }\n",
    )
    profile = test_ledger._profile()
    profile["tables"]["orders"]["columns"]["placed_on"] = {
        "domain": "timestamp",
        "metrics": {
            "count": _column_metric("count/1", 500),
            "min": _column_metric("min/1", status="A"),
            "max": _column_metric("max/1", maximum),
        },
        "statistics": [
            {**_binned(_DATE_LADDER, 2, 250, 500), "level": "0.25"},
            {**_binned(_DATE_LADDER, 4, 500, 800), "level": "0.75"},
        ],
    }
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    return contract, profile


_SPREAD = {"0.25": "20", "0.5": "35", "0.75": "50"}


def _clipped(constraint: str, quantiles: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
    """The ledger shop with an INT column whose published quantiles a contract range clips."""
    contract = test_ledger.CONTRACT.replace(
        "        - { name: amount, type: INT, required: true }\n",
        "        - { name: amount, type: INT, required: true }\n"
        "        - name: score\n"
        "          type: INT\n"
        "          required: true\n"
        "          validationRules:\n"
        f'            - {{ type: range, constraint: "{constraint}" }}\n',
    )
    profile = test_ledger._profile()
    profile["tables"]["orders"]["columns"]["score"] = {
        "domain": "number",
        "metrics": {
            "count": _column_metric("count/1", 500),
            "min": _column_metric("min/1", "10"),
            "max": _column_metric("max/1", "100"),
            "quantiles": _column_metric("quantiles/1", quantiles),
        },
    }
    return contract, profile


#: The run window a scenario is authored with, by scenario name.
WINDOWS: Dict[str, Tuple[str, str]] = {
    "date-max-past-the-ladder-windowed": ("2023-06-01", "2027-12-31"),
    "date-max-in-the-ladder-windowed": ("2023-06-01", "2025-12-31"),
}

SCENARIOS = {
    "ledger-shop": _ledger_scenario,
    "children": _children_scenario,
    # The maximum closes the top of a ladder whose next bin lies beyond the run's window.
    "date-max-past-the-ladder": lambda: _dated("1788220800000"),
    "date-max-in-the-ladder": lambda: _dated("1751241600000"),
    # A given window cuts the dated column's bins, and the cut replaces the labels that cite.
    "date-max-past-the-ladder-windowed": lambda: _dated("1788220800000"),
    "date-max-in-the-ladder-windowed": lambda: _dated("1751241600000"),
    # A contract range clips the numeric column's bins, dropping the ones outside it.
    "range-drops-bins": lambda: _clipped("between 20 and 60", _SPREAD),
    "range-moves-an-edge": lambda: _clipped(">= 15", _SPREAD),
    "range-leaves-one-value": lambda: _clipped("between 35 and 35", _SPREAD),
}


def _author(
    root: Path,
    contract: str,
    profile: Dict[str, Any],
    window: Optional[Tuple[str, str]] = None,
) -> Tuple[Any, Dict[str, Any]]:
    root.mkdir(parents=True)
    (root / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (root / "p.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(root / "bundle"),
        contract=str(root / "c.fluid.yaml"),
        profile=str(root / "p.json"),
        window=window,
    )
    if result.exit_code != 0:
        return result, {}
    text = (root / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    return result, yaml.safe_load(text)


def _comparable(simulation: Dict[str, Any]) -> Dict[str, Any]:
    document = copy.deepcopy(simulation)
    block = document["extensions"][NAMESPACE]
    block.pop("profile", None)
    block.pop("unused", None)
    return document


def _without(profile: Dict[str, Any], pointer: str) -> Dict[str, Any]:
    found = copy.deepcopy(profile)
    *parents, last = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    node: Any = found
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, list):
        del node[int(last)]
    else:
        del node[last]
    return found


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_statistic_the_ledger_calls_unused_changes_nothing(name: str, tmp_path: Path) -> None:
    contract, profile = SCENARIOS[name]()
    window = WINDOWS.get(name)
    first, whole = _author(tmp_path / "whole", contract, profile, window)
    assert first.exit_code == 0, first.stderr
    unused: List[str] = [
        entry["pointer"]
        for entry in first.artifacts["ledger"]["statistics"]
        if entry["state"] == "unused"
    ]
    skipped = SKIPPED.get(name, {})
    expected = _comparable(whole)
    differing: List[str] = []
    for position, pointer in enumerate(unused):
        if pointer in skipped:
            continue
        result, written = _author(
            tmp_path / f"without-{position}", contract, _without(profile, pointer), window
        )
        if result.exit_code != 0 or _comparable(written) != expected:
            differing.append(pointer)
    assert not differing, f"the bundle depends on statistics the ledger calls unused: {differing}"


def test_the_guard_lists_the_dated_maximum_as_used(tmp_path: Path) -> None:
    """The case the guard was written for: the published maximum closes the draw's top."""
    contract, profile = _dated("1788220800000")
    result, _ = _author(tmp_path / "x", contract, profile)
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    assert states[f"{_DATE_TABLE}/metrics/max"]["state"] == "used"


def _notes(name: str, tmp_path: Path) -> Dict[str, Tuple[str, str]]:
    contract, profile = SCENARIOS[name]()
    result, _ = _author(tmp_path / "x", contract, profile, WINDOWS.get(name))
    assert result.exit_code == 0, result.stderr
    return {
        entry["pointer"]: (entry["state"], entry.get("note", ""))
        for entry in result.artifacts["ledger"]["statistics"]
    }


def test_the_ledger_lists_what_a_range_clip_still_rests_on(tmp_path: Path) -> None:
    """The one value a range leaves is placed by the quantiles the clip then replaced."""
    notes = _notes("range-leaves-one-value", tmp_path)
    assert notes["/tables/orders/columns/score/metrics/quantiles"] == (
        "used",
        columns._RANGE_SHAPED,
    )


@pytest.mark.parametrize(
    "name", ["date-max-in-the-ladder-windowed", "date-max-past-the-ladder-windowed"]
)
def test_the_ledger_lists_what_a_window_cut_still_rests_on(name: str, tmp_path: Path) -> None:
    notes = _notes(name, tmp_path)
    assert notes[f"{_DATE_TABLE}/metrics/max"] == ("used", columns._WINDOW_SHAPED)
