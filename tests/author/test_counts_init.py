# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a ``counts`` entry from a relationship's children per parent (ADR 0016 section 7).

Pinned here, through ``fluxsim init`` and a run:

* a relationship the contract declares, with a published histogram of children per parent, is
  written as a ``counts`` entry whose ``children`` are the histogram's classes, and its foreign
  key declares no draw of its own;
* the open last class's upper edge is the one whose even spread has the mean the published total
  of children gives;
* the class of none is read as published: a zero is its weight, a withheld count is filled from
  the parents the published counts leave, and only ``childless`` makes it empty, with
  ``minChildren: 1``; ``orphan`` is recorded as checked;
* a relationship the layout cannot place gets no entry, so the bundle is one ``plan`` accepts;
* a run of the bundle gives each class of parents the share the profile publishes, and no
  parent is childless where the source enforces a child per parent.
"""

from __future__ import annotations

import collections
import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.author.children import _open_edge
from flux_engine.cli._init import run_init
from flux_engine.generator.counts import classes_of, slot_values
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify

from .test_children import (
    BINS,
    CONTRACT,
    NAMESPACE,
    NULLABLE,
    PARENTS,
    _histogram,
    _init,
    _mean,
    _metric,
    _profile,
    _states,
)

pytestmark = pytest.mark.integration

MANY = "1-3-0"
SHOP = ("shop.fluid.yaml", "shop.profile.json")


def _namespace(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _entries(tmp_path: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = _namespace(tmp_path).get("counts", [])
    return found


def _structural(profile: Dict[str, Any], classes: List[str]) -> Dict[str, Any]:
    """*profile* in format 1-3-0, its relationship marking *classes* structural."""
    found = copy.deepcopy(profile)
    found["_schemaURL"] = found["_schemaURL"].replace("1-0-0", MANY)
    found["relationships"][0]["structural"] = classes
    return found


def _with_cells(
    counts: List[Optional[int]], statuses: Optional[List[str]] = None, *, k: int = 1000
) -> Dict[str, Any]:
    histogram = _histogram(BINS, [0 if count is None else count for count in counts], k)
    group = histogram["groups"][0]
    group["binCounts"] = counts
    if statuses is not None:
        group["cellStatus"] = statuses
    return histogram


def test_the_entry_is_written_from_the_histogram(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]))
    assert _entries(tmp_path) == [
        {
            "emit": "orders",
            "relationship": "order_to_customer",
            "children": {
                # The parents of the last class (50) hold 1,000 of the 5,000 children: a mean of
                # 20, which an even spread over 20 to 21 has.
                "bins": ["0", "1", "2", "3", "5", "10", "20", "21"],
                "binWeights": ["0.15", "0.15", "0.15", "0.2", "0.2", "0.1", "0.05"],
            },
        }
    ]
    columns = _namespace(tmp_path)["emits"]["orders"]["fidelity"]["columns"]
    assert "customer_id" not in columns  # the layout sets the key: it declares no draw
    states = _states(result)
    assert states["/relationships/0/statistics/0"]["state"] == "used"
    assert states["/relationships/0/statistics/1"]["state"] == "used"
    assert "e^-m" not in result.stdout


def test_the_open_class_s_edge_gives_its_even_spread_the_mean_the_total_solves(
    tmp_path: Path,
) -> None:
    # 1,000 of 1,000 parents, the classes below the last holding 4,000 children: a total of
    # 6,000 leaves the 50 parents of the last class a mean of 40.
    _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(6000, 1000)]))
    children = _entries(tmp_path)[0]["children"]
    shape = classes_of(children)
    values = slot_values(1000, shape)
    last = [int(value) for value in values[-50:]]
    assert children["bins"][-2] == "20"
    # One more count on the edge moves the class by about half its parents, so the edge is the
    # nearest to a total of 50 * 40 the class's even spread reaches.
    assert abs(sum(last) - 50 * 40) <= 25
    assert last[0] == 20 and last[-1] <= int(children["bins"][-1])


def test_a_total_that_leaves_the_last_class_at_its_lower_bound_keeps_two_counts(
    tmp_path: Path,
) -> None:
    for statistics in ([_mean(4000, 1000)], []):  # a low total, and no total at all
        folder = tmp_path / str(len(statistics))
        folder.mkdir()
        _init(folder, _profile([_histogram(BINS, PARENTS, 1000), *statistics]))
        assert _entries(folder)[0]["children"]["bins"][-2:] == ["20", "21"]


def test_with_no_mean_the_open_class_s_edge_follows_the_children_the_rows_hold(
    tmp_path: Path,
) -> None:
    # The layout gives every child a parent, so the 6,000 rows of orders are the total of
    # children: with the classes below the last holding 4,000, the 50 parents of the last class
    # hold 2,000, a mean of 40. A lower bound of 20 would leave the layout 1,000 children short
    # of its own rows, and a run would spill them into the middle classes.
    profile = _profile([_histogram(BINS, PARENTS, 1000)])
    profile["tables"]["orders"]["rowCount"] = 6000
    _init(tmp_path, profile)
    children = _entries(tmp_path)[0]["children"]
    last = [int(value) for value in slot_values(1000, classes_of(children))[-50:]]
    assert children["bins"][-2] == "20"
    assert abs(sum(last) - 50 * 40) <= 25


@pytest.mark.parametrize("held", [1, 7, 50, 300])
@pytest.mark.parametrize("low", [0, 3, 20])
@pytest.mark.parametrize("mean", [Fraction(0), Fraction(7, 2), Fraction(33), Fraction(121, 3)])
def test_the_open_edge_is_the_nearest_to_the_mean_by_the_layout_itself(
    held: int, low: int, mean: Fraction
) -> None:
    edge = _open_edge(low, mean, held, low + 1)

    def total(top: int) -> int:
        width = top - low + 1
        return sum(low + (j * width) // held for j in range(held))

    assert edge >= low + 1
    target = mean * held
    best = min(abs(total(top) - target) for top in range(low + 1, low + 400))
    assert abs(total(edge) - target) == best


def test_a_published_zero_is_the_class_of_none_s_weight(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile([_with_cells([0, 300, 250, 200, 150, 60, 40])]))
    assert _entries(tmp_path)[0]["children"]["binWeights"][0] == "0"
    assert "withheld the count" not in result.stdout


def test_a_withheld_class_of_none_is_filled_and_never_assumed_empty(tmp_path: Path) -> None:
    cells: List[Optional[int]] = [None, 150, 150, 200, 200, 100, 50]
    statuses = ["S", "F", "F", "F", "F", "F", "F"]
    result = _init(tmp_path, _profile([_with_cells(cells, statuses)]))
    weights = _entries(tmp_path)[0]["children"]["binWeights"]
    assert weights == ["0.15", "0.15", "0.15", "0.2", "0.2", "0.1", "0.05"]  # the 150 left over
    assert "withheld the count of parents with 0 children" in result.stdout
    assert "ADR 0014 section 3" in result.stdout


def test_childless_writes_a_minimum_and_the_class_of_none_takes_no_weight(tmp_path: Path) -> None:
    cells: List[Optional[int]] = [None, 400, 250, 200, 100, 30, 20]
    statuses = ["S", "F", "F", "F", "F", "F", "F"]
    profile = _structural(_profile([_with_cells(cells, statuses)]), ["childless"])
    result = _init(tmp_path, profile)
    entry = _entries(tmp_path)[0]
    assert entry["minChildren"] == 1
    assert entry["children"]["binWeights"] == ["0", "0.4", "0.25", "0.2", "0.1", "0.03", "0.02"]
    state = _states(result)["/relationships/0/structural"]
    assert state["state"] == "used"
    assert "minChildren 1" in state["note"]


def test_childless_empties_the_class_of_none_even_where_it_is_published(tmp_path: Path) -> None:
    cells: List[Optional[int]] = [150, 150, 150, 200, 200, 100, 50]
    profile = _structural(_profile([_with_cells(cells)]), ["childless"])
    _init(tmp_path, profile)
    weights = _entries(tmp_path)[0]["children"]["binWeights"]
    assert weights[0] == "0"
    assert sum(Fraction(weight) for weight in weights) == 1


def test_orphan_is_recorded_as_checked_and_writes_no_minimum(tmp_path: Path) -> None:
    profile = _structural(_profile([_histogram(BINS, PARENTS, 1000)]), ["orphan"])
    result = _init(tmp_path, profile)
    assert "minChildren" not in _entries(tmp_path)[0]
    state = _states(result)["/relationships/0/structural"]
    assert state["state"] == "used"
    assert state["note"].startswith("orphan: checked")


def test_a_structural_class_of_a_relationship_that_is_not_read_is_not_listed_as_used(
    tmp_path: Path,
) -> None:
    profile = _structural(
        _profile([_histogram(BINS, PARENTS, 1000)], columns=["order_id"]), ["childless", "orphan"]
    )
    result = _init(tmp_path, profile)
    states = _states(result)
    assert "/relationships/0/structural" not in states  # no rule read it
    assert states["/relationships/0/statistics/0"]["state"] == "unused"
    assert _entries(tmp_path) == []


def test_childless_on_a_relationship_the_layout_does_not_place_is_said_not_to_be_written(
    tmp_path: Path,
) -> None:
    profile = _structural(_profile([_histogram(BINS, PARENTS, 1000)]), ["childless"])
    result = _init(tmp_path, profile, counted=False)  # a nullable key the layout cannot place
    assert _entries(tmp_path) == []
    assert "/relationships/0/structural" not in _states(result)
    assert "childless: not written, because the relationship is not laid out by counts" in (
        result.stdout
    )


def test_orphan_alone_is_checked_on_a_relationship_the_layout_does_not_place(
    tmp_path: Path,
) -> None:
    profile = _structural(_profile([_histogram(BINS, PARENTS, 1000)]), ["orphan"])
    result = _init(tmp_path, profile, counted=False)
    assert _entries(tmp_path) == []
    state = _states(result)["/relationships/0/structural"]
    assert state["state"] == "used" and state["note"].startswith("orphan: checked")


def test_a_histogram_with_no_published_edge_the_layout_reads_is_laid_out_as_before(
    tmp_path: Path,
) -> None:
    bins = ["0", "1", "2.5", "10"]  # a fraction is no count
    result = _init(tmp_path, _profile([_histogram(bins, [200, 300, 500], 1000)]))
    assert _entries(tmp_path) == []
    assert _states(result)["/relationships/0/statistics/0"]["state"] == "used"


# -- relationships the layout cannot place -----------------------------------------------------


def _planned(tmp_path: Path) -> None:
    """The bundle written under *tmp_path* validates and plans, so no counts entry is refused."""
    bundle = tmp_path / "bundle"
    outcome = plan(
        bundle,
        state_dir=tmp_path / "state",
        profile=tmp_path / SHOP[1],
        kernel="0.2.0",
        stage=False,
    )
    assert outcome.success, outcome.stderr


def test_a_nullable_foreign_key_with_a_published_null_share_gets_no_entry(tmp_path: Path) -> None:
    _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]), counted=False)
    assert _entries(tmp_path) == []
    assert "customer_id" in _namespace(tmp_path)["emits"]["orders"]["fidelity"]["columns"]
    _planned(tmp_path)


def test_a_nullable_foreign_key_with_no_published_null_gets_an_entry(tmp_path: Path) -> None:
    # No nullCount: the null share would be assumed zero, which the layout holds already, so the
    # assumed share is dropped with the rest of the key's own draw.
    (tmp_path / SHOP[0]).write_text(NULLABLE, encoding="utf-8")
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)])
    (tmp_path / SHOP[1]).write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / SHOP[0]),
        profile=str(tmp_path / SHOP[1]),
    )
    assert result.exit_code == 0, result.stderr
    assert len(_entries(tmp_path)) == 1
    assert "customer_id" not in _namespace(tmp_path)["emits"]["orders"]["fidelity"]["columns"]
    _planned(tmp_path)


def _one_to_one(profile: Dict[str, Any]) -> Dict[str, Any]:
    found = copy.deepcopy(profile)
    orders = found["tables"]["orders"]
    orders["rowCount"] = 1000
    keys = orders["columns"]
    keys["order_id"]["metrics"]["count"]["value"] = 1000
    keys["order_id"]["metrics"]["max"]["value"] = "1000"
    keys["customer_id"]["metrics"]["count"]["value"] = 1000
    return found


def _init_one_to_one(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    """Init over a contract whose ``orders.customer_id`` is ``one_to_one``."""
    contract = CONTRACT.replace("cardinality: many_to_one", "cardinality: one_to_one")
    (tmp_path / SHOP[0]).write_text(contract, encoding="utf-8")
    (tmp_path / SHOP[1]).write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / SHOP[0]),
        profile=str(tmp_path / SHOP[1]),
    )
    assert result.exit_code == 0, result.stderr
    return result


def test_a_one_to_one_relationship_takes_the_levels_none_and_one(tmp_path: Path) -> None:
    # The last class closes at 2, and no parent of a one_to_one relationship has two children,
    # so it is the class of one.
    profile = _one_to_one(_profile([_histogram(["0", "1", "2"], [100, 900], 1000)]))
    result = _init_one_to_one(tmp_path, profile)
    assert _entries(tmp_path) == [
        {
            "emit": "orders",
            "relationship": "order_to_customer",
            "children": {"levels": ["0", "1"], "levelWeights": ["0.1", "0.9"]},
        }
    ]
    columns = _namespace(tmp_path)["emits"]["orders"]["fidelity"]["columns"]
    assert "customer_id" not in columns
    assert _states(result)["/relationships/0/statistics/0"]["state"] == "used"
    _planned(tmp_path)


def test_a_one_to_one_relationship_with_a_mean_leaves_the_mean_unused(tmp_path: Path) -> None:
    profile = _one_to_one(
        _profile([_histogram(["0", "1", "2"], [100, 900], 1000), _mean(900, 1000)])
    )
    result = _init_one_to_one(tmp_path, profile)
    states = _states(result)
    assert states["/relationships/0/statistics/0"]["state"] == "used"
    assert states["/relationships/0/statistics/1"]["state"] == "unused"
    assert len(_entries(tmp_path)) == 1


def test_childless_on_a_one_to_one_relationship_writes_a_minimum_and_no_weight_for_none(
    tmp_path: Path,
) -> None:
    profile = _structural(
        _one_to_one(_profile([_histogram(["0", "1", "2"], [0, 1000], 1000)])), ["childless"]
    )
    _init_one_to_one(tmp_path, profile)
    entry = _entries(tmp_path)[0]
    assert entry["minChildren"] == 1
    assert entry["children"] == {"levels": ["0", "1"], "levelWeights": ["0", "1"]}
    _planned(tmp_path)


def test_a_withheld_class_of_none_of_a_one_to_one_relationship_is_filled_and_said(
    tmp_path: Path,
) -> None:
    histogram = _histogram(["0", "1", "2"], [0, 800], 1000)
    group = histogram["groups"][0]
    group["binCounts"] = [None, 800]
    group["cellStatus"] = ["S", "F"]
    result = _init_one_to_one(tmp_path, _one_to_one(_profile([histogram])))
    assert _entries(tmp_path)[0]["children"]["levelWeights"] == ["0.2", "0.8"]
    assert "withheld the count of parents with 0 children" in result.stdout


def test_a_class_above_one_leaves_a_one_to_one_relationship_to_the_spread_and_says_so(
    tmp_path: Path,
) -> None:
    histogram = _histogram(["0", "1", "2", "3"], [100, 800, 100], 1000)
    result = _init_one_to_one(tmp_path, _one_to_one(_profile([histogram])))
    assert _entries(tmp_path) == []
    assert "customer_id" in _namespace(tmp_path)["emits"]["orders"]["fidelity"]["columns"]
    assert "not laid out by counts: its histogram has a class of parents with more than one" in (
        result.stdout
    )
    _planned(tmp_path)


def test_a_class_above_one_with_no_parent_is_dropped(tmp_path: Path) -> None:
    histogram = _histogram(["0", "1", "2", "3"], [100, 900, 0], 1000)
    _init_one_to_one(tmp_path, _one_to_one(_profile([histogram])))
    assert _entries(tmp_path)[0]["children"] == {
        "levels": ["0", "1"],
        "levelWeights": ["0.1", "0.9"],
    }


def _one_to_one_run(tmp_path: Path, scale: str) -> None:
    profile = _structural(
        _one_to_one(_profile([_histogram(["0", "1", "2"], [100, 900], 1000)])), ["orphan"]
    )
    tables = profile["tables"]
    tables["customer"]["columns"]["customer_id"]["statistics"] = [_mean(500500, 1000)]
    tables["orders"]["columns"]["order_id"]["statistics"] = [_mean(500500, 1000)]
    _init_one_to_one(tmp_path, profile)
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    profile_path = tmp_path / SHOP[1]
    planned = plan(bundle, state_dir=state, profile=profile_path, kernel="0.2.0", scale=scale)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    # A scaled run's keys span fewer values than the profile's, so only a full run is scored
    # against the profile; a scaled one is checked for its own rules and integrity.
    against = profile_path if scale == "1" else None
    checked = verify(bundle, state_dir=state, run_id=run_id, profile=against)
    assert checked.success, checked.stdout + checked.stderr
    text = (state / "runs" / run_id / "emits" / "orders.ndjson").read_text(encoding="utf-8")
    keys = [json.loads(line)[1] for line in text.splitlines() if line]
    assert len(keys) == len(set(keys))  # no parent has a second child
    rows = len(keys)
    assert rows == int(Fraction(scale) * 1000 + Fraction(1, 2))


@pytest.mark.parametrize("scale", ["1", "0.37"])
def test_init_plan_apply_verify_a_one_to_one_relationship(tmp_path: Path, scale: str) -> None:
    _one_to_one_run(tmp_path, scale)


SELF_CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: children.staff
name: Staff
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: staff
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/staff.parquet }
    contract:
      schema:
        - { name: staff_id, type: INT, required: true }
        - { name: manager_id, type: INT, required: true }
    semantics:
      entities:
        - { name: staff, type: primary, expr: staff_id }
      relationships:
        - name: staff_to_manager
          to: staff
          fromColumns: [manager_id]
          toColumns: [staff_id]
          cardinality: many_to_one
"""


def test_a_self_relationship_gets_no_entry(tmp_path: Path) -> None:
    base = _profile([_histogram(BINS, PARENTS, 1000)])
    key = base["tables"]["customer"]["columns"]["customer_id"]
    foreign = base["tables"]["orders"]["columns"]["customer_id"]
    profile = copy.deepcopy(base)
    profile["tables"] = {
        "staff": {
            "rowCount": 1000,
            "columns": {
                "staff_id": key,
                "manager_id": {"domain": "number", "metrics": {"count": _metric("count/1", 1000)}},
            },
        }
    }
    profile["relationships"][0]["parent"] = {"table": "staff", "columns": ["staff_id"]}
    profile["relationships"][0]["child"] = {"table": "staff", "columns": ["manager_id"]}
    del foreign
    (tmp_path / SHOP[0]).write_text(SELF_CONTRACT, encoding="utf-8")
    (tmp_path / SHOP[1]).write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / SHOP[0]),
        profile=str(tmp_path / SHOP[1]),
    )
    assert result.exit_code == 0, result.stderr
    assert _entries(tmp_path) == []
    _planned(tmp_path)


def _hold(
    *,
    declaration: Optional[Dict[str, Any]] = None,
    others: Optional[Dict[str, Dict[str, Any]]] = None,
    cardinality: str = "many_to_one",
    target: str = "customer",
    keyed: bool = False,
    primary: bool = False,
    twin: bool = False,
    derived: str = "",
) -> Optional[str]:
    """Why ``_counting_hold`` keeps a relationship of ``orders.customer_id`` off the layout."""
    from flux_engine.author.bundle import AuthoredExpose, _counting_hold
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.seam.keys import ContractKeys, EntityKey, ExposeKeys, Relationship

    relationship = Relationship(
        "order_to_customer",
        "orders",
        target,
        ("customer_id",),
        ("customer_id",),
        cardinality,
        "left",
    )
    twins = (
        [
            Relationship(
                "other",
                "orders",
                "account",
                ("customer_id",),
                ("account_id",),
                "many_to_one",
                "left",
            )
        ]
        if twin
        else []
    )
    expose_keys = ExposeKeys(
        expose_id="orders",
        relationships=(relationship, *twins),
        entities=(EntityKey("own", "primary", "customer_id"),) if primary else (),
        keys=(("customer_id",),) if keyed or primary else (),
    )
    keys = ContractKeys("0.7.7", {"orders": expose_keys})
    columns = [ColumnPlan(name="customer_id", declaration=dict(declaration or {}))]
    for name, declared in (others or {}).items():
        columns.append(ColumnPlan(name=name, declaration=declared))
    expose = AuthoredExpose(expose_id="orders", columns=columns)
    if derived == "derives":
        expose.derives["customer_id"] = (0, "x")
    elif derived == "dependents":
        expose.dependents.add("customer_id")
    elif derived == "copies":
        expose.copies["customer_id"] = (("a",), "b", "c")
    return _counting_hold(keys, expose, columns[0], relationship)


@pytest.mark.parametrize(
    "arguments, reason",
    [
        (dict(target="orders"), "its own expose"),
        (dict(keyed=True), "part of a key"),
        (dict(twin=True), "another relationship"),
        (dict(declaration={"nullFraction": "0.1"}), "nullFraction"),
        (dict(declaration={"given": "region"}), "drawn given another"),
        (dict(declaration={"binWeightsGiven": {"a": ["1"]}}), "drawn given another"),
        (dict(others={"region": {"given": "customer_id"}}), "is drawn given it"),
        (dict(others={"region": {"given": ["day", "customer_id"]}}), "is drawn given it"),
        (dict(derived="derives"), "a rule of the contract"),
        (dict(derived="dependents"), "a rule of the contract"),
        (dict(derived="copies"), "a rule of the contract"),
    ],
)
def test_the_layout_is_not_asked_for_a_relationship_plan_would_refuse(
    arguments: Dict[str, Any], reason: str
) -> None:
    held = _hold(**arguments)
    assert held is not None and reason in held


def test_a_one_to_one_relationship_is_placed_unless_its_column_is_a_key() -> None:
    assert _hold(cardinality="one_to_one") is None
    held = _hold(cardinality="one_to_one", primary=True)
    assert held is not None and "part of a key" in held


def test_a_key_with_its_own_levels_bins_range_and_a_null_share_of_none_is_placed() -> None:
    declaration = {
        "bins": ["1", "1000"],
        "binWeights": ["1"],
        "range": {"min": "1", "max": "1000"},
        "nullFraction": "0",
    }
    assert _hold(declaration=declaration) is None


# -- the run -----------------------------------------------------------------------------------


def _shop(tmp_path: Path) -> Dict[str, Any]:
    """A profile of 1,000 customers and 3,000 orders whose source gives every customer an order."""
    cells: List[Optional[int]] = [None, 400, 250, 200, 150]
    statuses = ["S", "F", "F", "F", "F"]
    histogram = _histogram(["0", "1", "2", "3", "5", "10"], [0, 400, 250, 200, 150], 1000)
    histogram["groups"][0]["binCounts"] = cells
    histogram["groups"][0]["cellStatus"] = statuses
    profile = _structural(_profile([histogram, _mean(3000, 1000)]), ["childless", "orphan"])
    tables = profile["tables"]
    tables["orders"]["rowCount"] = 3000
    keys = tables["orders"]["columns"]
    keys["order_id"]["metrics"]["count"]["value"] = 3000
    keys["order_id"]["metrics"]["max"]["value"] = "3000"
    keys["customer_id"]["metrics"]["count"]["value"] = 3000
    # A mean to score each key's property against.
    tables["customer"]["columns"]["customer_id"]["statistics"] = [_mean(500500, 1000)]
    keys["order_id"]["statistics"] = [_mean(4501500, 3000)]
    return profile


def _children_of(state: Path, run_id: str) -> List[int]:
    text = (state / "runs" / run_id / "emits" / "orders.ndjson").read_text(encoding="utf-8")
    counts = collections.Counter(json.loads(line)[1] for line in text.splitlines() if line)
    return [counts.get(key, 0) for key in range(1, 1001)]


def test_init_plan_apply_verify_hold_the_histogram_and_leave_no_parent_childless(
    tmp_path: Path,
) -> None:
    profile = _shop(tmp_path)
    _init(tmp_path, profile)
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    profile_path = tmp_path / SHOP[1]
    planned = plan(bundle, state_dir=state, profile=profile_path, kernel="0.2.0")
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    checked = verify(bundle, state_dir=state, run_id=run_id, profile=profile_path)
    assert checked.success, checked.stdout + checked.stderr
    counts = _children_of(state, run_id)
    assert sum(counts) == 3000
    assert min(counts) >= 1  # `childless`: no parent without a child
    # The histogram of children per parent is the profile's, class for class.
    classes = [(1, 1), (2, 2), (3, 4), (5, 10**9)]
    held = [sum(1 for count in counts if low <= count <= high) for low, high in classes]
    assert held == [400, 250, 200, 150]
    # Nothing in the run draws the foreign key: every order names a customer.
    assert all(count >= 0 for count in counts)


def test_the_run_command_agrees_with_its_stages(tmp_path: Path) -> None:
    _init(tmp_path, _shop(tmp_path))
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / SHOP[1],
        verify=True,
    )
    assert outcome.success, outcome.stdout + outcome.stderr
    assert "integrity orders.order_to_customer -> customer: found 3000 of 3000" in outcome.stdout


def test_init_writes_the_same_bundle_twice(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for folder in (first, second):
        _init(folder, _shop(folder))
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        assert (first / "bundle" / name).read_bytes() == (second / "bundle" / name).read_bytes()
