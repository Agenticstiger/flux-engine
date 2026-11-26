# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` declares one exposure through relationships of one name to one parent (ADR 0014).

The parent's counts are kept by the relationship's name and the parent alone, so two exposes that
declare an exposure through relationships of the same name to the same parent could not be told
apart, and ``plan`` refuses the bundle (``FLX-3403``). Of such exposures init declares the one
whose published children are the most, and the lowest expose id when they are equal, whichever
order the profile lists them in; the others' statistics are unused, with the reason.

The fixture is the exposure chain, its customer's two children renamed so they share the name of
the relationship that reaches the customer.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "exposure-chain"
NAMESPACE = "com.agentics.flux/1"


def _write(
    directory: Path,
    *,
    rename: str = "shop_order",
    reverse: bool = False,
    names: Tuple[str, ...] = ("order_to_customer", "subscription_to_customer"),
) -> Path:
    """The chain's contract with the relationships *names* all called ``customer``."""
    contract = (FIXTURE / "contract.fluid.yaml").read_text(encoding="utf-8")
    for name in names:
        contract = contract.replace(name, "customer")
    profile = (FIXTURE / "profile.json").read_text(encoding="utf-8")
    contract = contract.replace("shop_order", rename)
    profile = profile.replace("shop_order", rename)
    if reverse:
        document = json.loads(profile)
        document["relationships"].reverse()
        profile = json.dumps(document)
    (directory / "contract.fluid.yaml").write_text(contract, encoding="utf-8")
    (directory / "profile.json").write_text(profile, encoding="utf-8")
    return directory


def _init(tmp_path: Path, **options: Any) -> Dict[str, Any]:
    source = _write(tmp_path, **options)
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(source / "contract.fluid.yaml"),
        profile=str(source / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    document = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))
    return dict(document["extensions"][NAMESPACE])


def _declaring(block: Dict[str, Any]) -> List[str]:
    return sorted(
        entry["emit"]
        for entry in block["within"]
        if entry["relationship"] == "customer" and entry.get("exposure") is True
    )


def test_two_exposes_through_one_relationship_name_declare_one_exposure(tmp_path: Path) -> None:
    block = _init(tmp_path)
    # The order publishes its exposure over 2354 children, the other child over 811.
    assert _declaring(block) == ["shop_order"]


def test_the_most_children_decide_before_the_lowest_expose_id(tmp_path: Path) -> None:
    # The renamed order sorts after the other child, so the lowest id would pick the other child.
    block = _init(tmp_path, rename="zeta_order")
    assert _declaring(block) == ["zeta_order"]


def test_the_profile_order_does_not_decide(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    forward = _declaring(_init(tmp_path / "a", rename="zeta_order"))
    backward = _declaring(_init(tmp_path / "b", rename="zeta_order", reverse=True))
    assert forward == backward == ["zeta_order"]


def test_the_exposure_it_does_not_declare_is_unused_and_names_the_one_that_does(
    tmp_path: Path,
) -> None:
    block = _init(tmp_path)
    reasons = [
        item["reason"]
        for item in block["unused"]
        if item["statistic"]["pointer"] == "/relationships/0/exposure/0"
    ]
    assert len(reasons) == 1
    assert "'shop_order' declares the exposure through the relationship of this name" in reasons[0]
    assert "the parent's counts cannot tell two apart" in reasons[0]
    # The statistic of the exposure that is declared stays used.
    assert not [
        item
        for item in block["unused"]
        if item["statistic"]["pointer"] == "/relationships/2/exposure/0"
    ]


def test_the_bundle_it_writes_plans_and_passes_its_own_verify(tmp_path: Path) -> None:
    _init(tmp_path)
    result = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "profile.json",
        verify=True,
    )
    assert result.exit_code == 0, result.stderr
    lines = result.artifacts["verify"]["exposure"]["lines"]
    assert [line["line"] for line in lines] == ["exposure:customer"]
    assert all(line["outcome"] == "within" for line in lines)


def _without_counts(directory: Path, relationship: int) -> None:
    """The profile written by :func:`_write`, with one relationship's children per parent withheld."""
    document = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    document["relationships"][relationship]["statistics"] = []
    (directory / "profile.json").write_text(json.dumps(document), encoding="utf-8")


def _unused(block: Dict[str, Any], pointer: str) -> List[str]:
    return [item["reason"] for item in block["unused"] if item["statistic"]["pointer"] == pointer]


def test_a_rival_whose_key_is_drawn_on_its_own_does_not_compete(tmp_path: Path) -> None:
    # The order has the most children, but with no children per parent laid out for it, its
    # foreign key is drawn on its own, and the counted layout an exposure makes would refuse that.
    _write(tmp_path)
    _without_counts(tmp_path, 2)
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "contract.fluid.yaml"),
        profile=str(tmp_path / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    assert _declaring(block) == ["subscription"]
    (reason,) = _unused(block, "/relationships/2/exposure/0")
    assert "foreign key of this relationship is drawn on its own" in reason


def test_an_exposure_over_a_key_drawn_on_its_own_is_left_out(tmp_path: Path) -> None:
    # Before, init wrote the exposure and the plan refused the bundle (FLX-3403).
    source = tmp_path / "source"
    source.mkdir()
    contract = (FIXTURE / "contract.fluid.yaml").read_text(encoding="utf-8")
    (source / "contract.fluid.yaml").write_text(contract, encoding="utf-8")
    shutil.copy(FIXTURE / "profile.json", source / "profile.json")
    _without_counts(source, 0)
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(source / "contract.fluid.yaml"),
        profile=str(source / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    within = {entry["relationship"]: entry for entry in block["within"]}
    assert "exposure" not in within["subscription_to_customer"]
    assert within["order_to_customer"]["exposure"] is True
    (reason,) = _unused(block, "/relationships/0/exposure/0")
    assert "drawn on its own" in reason


@pytest.mark.parametrize(
    ("first", "second", "winner"),
    [
        ((Decimal(5), "b"), (Decimal(9), "z"), "z"),
        ((Decimal(9), "z"), (Decimal(5), "b"), "z"),
        ((Decimal(7), "b"), (Decimal(7), "a"), "a"),
        ((Decimal(7), "a"), (Decimal(7), "b"), "a"),
    ],
)
def test_the_rank_is_most_children_then_lowest_expose_id(
    first: Any, second: Any, winner: str
) -> None:
    from flux_engine.author.dates import _exposure_rank, _Weighed

    def weighed(children: Decimal, emit: str) -> _Weighed:
        return _Weighed(0, {"emit": emit}, "customer", children=children)

    group = [weighed(*first), weighed(*second)]
    assert str(min(group, key=_exposure_rank).first["emit"]) == winner  # type: ignore[index]


def test_exposes_that_reach_one_parent_by_different_names_both_declare_theirs(
    tmp_path: Path,
) -> None:
    block = _init(tmp_path, names=())
    assert sorted(
        entry["emit"]
        for entry in block["within"]
        if entry["relationship"] in ("order_to_customer", "subscription_to_customer")
        and entry.get("exposure") is True
    ) == ["shop_order", "subscription"]


def test_exposures_to_different_parents_are_not_rivals(tmp_path: Path) -> None:
    # The invoice reaches the subscription through a relationship of the same name, to another
    # parent: its exposure is its own, and the trial run is what leaves it out (the lifetime it
    # reads is cut to its parent's), not the rule above.
    block = _init(
        tmp_path,
        names=("order_to_customer", "subscription_to_customer", "invoice_to_subscription"),
    )
    assert _declaring(block) == ["shop_order"]
    (reason,) = _unused(block, "/relationships/1/exposure/0")
    assert "the run cannot reproduce this exposure's rate" in reason


def test_an_exposure_of_the_same_relationship_that_does_not_fit_keeps_its_own_reason(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path)
    document = json.loads((source / "profile.json").read_text(encoding="utf-8"))
    second = json.loads(json.dumps(document["relationships"][0]["exposure"][0]))
    second["start"], second["end"] = "closed_on", "signed_up"
    document["relationships"][0]["exposure"].append(second)
    (source / "profile.json").write_text(json.dumps(document), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(source / "contract.fluid.yaml"),
        profile=str(source / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    assert (
        "declares the exposure through the relationship of this name"
        in _unused(block, "/relationships/0/exposure/0")[0]
    )
    assert (
        "its lifetime columns are not the lifetime the bundle declares"
        in _unused(block, "/relationships/0/exposure/1")[0]
    )


@pytest.mark.parametrize(
    "key", ["levels", "binWeights", "range", "nullFraction", "given", "levelWeightsGiven"]
)
def test_a_column_that_declares_a_draw_is_one_the_layout_would_refuse(key: str) -> None:
    from types import SimpleNamespace

    from flux_engine.author.children import draws_itself

    assert draws_itself(SimpleNamespace(declaration={key: []})) is True  # type: ignore[arg-type]
    assert draws_itself(SimpleNamespace(declaration={"unrelated": 1})) is False  # type: ignore[arg-type]
