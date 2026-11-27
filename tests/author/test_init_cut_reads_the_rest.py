# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A release cut by the unused list keeps the statistics a column or a pair of dates rests on.

Dropping the statistics ``init`` lists as unused from a profile is meant to give the same bundle as
the whole profile (ADR 0014, amendment of 2026-11-27). Four shapes gave another bundle:

* a date column a contract rule sets, whose released histogram is noise everywhere: the column is
  still written as one bin over the run's window, which the histogram's noise decides;
* the ``levelHistogram`` property of a column that is then drawn given another column, which still
  scores the column against its own histogram;
* a date pair that gives no usable duration: its order names the start the lifetime keeps;
* a pair a table lists with an order that cannot be read (suppressed, withheld or noisy) claimed the
  two dates, so the contract's own check of them wrote no lifetime. Only a pair whose order is read
  claims them.

Each is pinned by the whole profile and its cut giving one bundle, and by the statistic alone
changing the bundle, so none is read that decides nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.cli import test_init_dates as dated
from tests.cli.test_init_dates import _levels, _pair
from tests.cli.test_init_dates import _profile as dated_profile
from tests.cli.test_init_ledger_entries import _drop

from . import test_given as given
from . import test_init_date_order as cards
from . import test_init_noise_floor as noise
from ._cut import broken_cuts, generated, init_profile, listed_unused

pytestmark = pytest.mark.integration


def _contract(tmp_path: Path, text: str, name: str = "contract.fluid.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _is_read_and_decides(tmp_path: Path, text: str, profile: Dict[str, Any], pointer: str) -> None:
    """*pointer* is read, no cut of *profile* changes its bundle, and dropping *pointer* alone does."""
    contract = _contract(tmp_path, text)
    result = init_profile(tmp_path / "whole", contract, profile)
    assert result.exit_code == 0, result.stderr
    states = {item["pointer"]: item for item in result.artifacts["ledger"]["statistics"]}
    assert states[pointer]["state"] == "used"
    assert pointer not in listed_unused(tmp_path / "whole" / "bundle")
    assert broken_cuts(tmp_path / "cut", contract, profile) == []
    alone = init_profile(tmp_path / "alone", contract, _drop(profile, [pointer]))
    assert alone.exit_code == 0, alone.stderr
    assert generated(tmp_path / "whole" / "bundle") != generated(tmp_path / "alone" / "bundle")


# -- a date a rule sets, whose histogram is noise everywhere -------------------------------------

#: A member's first loan is set by a rule from the day the member signed up.
RULE_SETS_THE_DATE = noise.CONTRACT.replace(
    "        - { name: joined_on, type: DATE, required: true }\n",
    "        - { name: signup_on, type: DATE, required: true }\n"
    "        - { name: joined_on, type: DATE, required: true }\n",
).replace(
    "    semantics:\n",
    "      invariants:\n"
    "        - id: joined_after_signup\n"
    "          derive: joined_on\n"
    '          expr: "ADD_DAYS(signup_on, 14)"\n'
    "    semantics:\n",
    1,
)
NOISE_EVERYWHERE = ["3", "-2", "5", "0", "-4", "6", "1", "-1", "2", "0", "4", "-3"]


def test_a_noisy_histogram_of_a_date_a_rule_sets_is_read(tmp_path: Path) -> None:
    profile = noise._profile(NOISE_EVERYWHERE, "50")
    pointer = "/tables/member/columns/joined_on/statistics/0"
    _is_read_and_decides(tmp_path, RULE_SETS_THE_DATE, profile, pointer)


# -- the histogram a property scores a column against, once the column is drawn given another ---


def test_the_histogram_a_given_column_is_scored_against_is_read(tmp_path: Path) -> None:
    # The groups the column is drawn from contradict its own histogram: its labels cite the groups,
    # and the histogram stays what the property scores the column against.
    groups = {"A": ([450, 50], 500), "B": ([50, 450], 500)}
    profile = given._profile(region_tier=True, tier_groups=groups)
    pointer = "/tables/customer/columns/segment/statistics/0"
    _is_read_and_decides(tmp_path, given._contract(), profile, pointer)


# -- a date pair that gives no usable duration ---------------------------------------------------


def test_the_order_of_a_pair_with_no_usable_duration_is_read(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=10, same=30))
    pair["statistics"] = []
    profile = dated_profile(customer=[pair])
    _is_read_and_decides(tmp_path, dated.CONTRACT, profile, "/tables/customer/datePairs/0/order")


# -- a pair whose order cannot be read does not claim the two dates ------------------------------


def _with_pair(order: Dict[str, Any]) -> Dict[str, Any]:
    profile = cards._profile("card", "issued_on", "cancelled_on", ended=cards.OVERLAPPING)
    profile["tables"]["card"]["datePairs"] = [_pair("issued_on", "cancelled_on", order)]
    return profile


def _lifetime(tmp_path: Path) -> List[Dict[str, Any]]:
    extension: Dict[str, Any] = generated(tmp_path / "bundle")["extension"]
    found: List[Dict[str, Any]] = extension.get("lifetimes", [])
    return found


@pytest.mark.parametrize(
    "order",
    [_levels(after=None, before=None, same=None), _levels(after=None, before=0, same=None)],
    ids=["suppressed", "partly withheld"],
)
def test_a_pair_with_an_unreadable_order_leaves_the_contracts_check_its_lifetime(
    tmp_path: Path, order: Dict[str, Any]
) -> None:
    contract = _contract(tmp_path, cards.CARD)
    result = init_profile(tmp_path, contract, _with_pair(order))
    assert result.exit_code == 0, result.stderr
    (life,) = _lifetime(tmp_path)
    assert (life["emit"], life["start"], life["end"]) == ("card", "issued_on", "cancelled_on")
    assert "duration" in life


def test_a_pair_with_a_readable_order_still_claims_the_dates_and_keeps_its_bytes(
    tmp_path: Path,
) -> None:
    exact = _with_pair(_levels(after=900, before=0, same=100))
    plain = cards.CARD.replace(
        "      invariants:\n"
        "        - id: card_cancelled_after_issued\n"
        '          check: "cancelled_on IS NULL OR cancelled_on >= issued_on"\n'
        "          mustBeLessOrEqualTo: 5\n"
        "          unit: percent\n",
        "",
    )
    assert plain != cards.CARD
    for name, text in (("check", cards.CARD), ("none", plain)):
        init_profile(tmp_path / name, _contract(tmp_path, text, f"{name}.yaml"), exact)
    with_check = generated(tmp_path / "check" / "bundle")
    without = generated(tmp_path / "none" / "bundle")
    # The pair's order is read, so it writes the lifetime and the check adds nothing.
    assert with_check["extension"]["lifetimes"] == without["extension"]["lifetimes"]
    assert with_check["extension"]["emits"] == without["extension"]["emits"]


def test_a_whole_release_with_an_unreadable_pair_order_is_cut_to_the_same_bundle(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path, cards.CARD)
    profile = _with_pair(_levels(after=None, before=None, same=None))
    assert broken_cuts(tmp_path / "cut", contract, profile) == []
