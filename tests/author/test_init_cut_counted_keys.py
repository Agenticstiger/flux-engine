# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A count that leaves an expose no column to declare is read, though the expose is not written.

A table of one foreign key and nothing else is laid out by the counts of its relationship: the
counts entry takes the key off every draw of its own, so the table has no column the fidelity block
could declare and is not written (``FLX-5034``). Without the count the key is drawn over the span of
the key it references, and the table is written. The count decides that, so it is read, and a release
cut by the unused list keeps it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Sequence

import pytest

from ._written import generated
from .test_children import BINS, CONTRACT, PARENTS, _histogram, _mean, _profile
from .test_history_init import _init, _namespace, _states

pytestmark = pytest.mark.integration

#: Orders that hold only the customer they belong to.
ONLY_A_KEY = CONTRACT.replace(
    "        - { name: order_id, type: INT, required: true }\n", "", 1
).replace("      entities:\n        - { name: order, type: primary, expr: order_id }\n", "", 1)
assert ONLY_A_KEY != CONTRACT

COUNT = "/relationships/0/statistics/0"


def _release(beside: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The profile of the orders, with *beside* published next to the histogram of the count."""
    profile = _profile([_histogram(BINS, PARENTS, 1000), *beside])
    del profile["tables"]["orders"]["columns"]["order_id"]
    return profile


def test_the_count_that_leaves_the_table_no_column_is_read_and_not_listed_unused(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _release(), ONLY_A_KEY)
    namespace = _namespace(tmp_path)
    assert "orders" not in namespace["emits"]
    assert COUNT not in [item["statistic"]["pointer"] for item in namespace["unused"]]
    state = _states(result)[COUNT]
    assert state["state"] == "used"
    assert "no column to declare" in state["note"]


def test_without_the_count_the_table_is_written(tmp_path: Path) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    # The count alone decides it: the bundle of the profile without it holds the table.
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, _release(), ONLY_A_KEY)
    alone = tmp_path / "alone"
    alone.mkdir()
    _init(alone, _drop(_release(), [COUNT]), ONLY_A_KEY)
    assert "orders" in _namespace(alone)["emits"]
    assert generated(_namespace(alone)) != generated(_namespace(whole))


def test_a_release_cut_by_the_unused_list_gives_the_bundle_of_the_whole_profile(
    tmp_path: Path,
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, _release(), ONLY_A_KEY)
    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(_release(), listed), ONLY_A_KEY)
    assert generated(_namespace(cut)) == generated(_namespace(whole))


def test_a_mean_beside_the_histogram_of_the_count_stays_unused(tmp_path: Path) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    # The histogram is what leaves the table no column. The mean beside it decides nothing: the
    # bundle of the profile without it is the same, and it stays listed.
    result = _init(tmp_path, _release([_mean(5000, 1000)]), ONLY_A_KEY)
    assert "orders" not in _namespace(tmp_path)["emits"]
    assert _states(result)[COUNT]["state"] == "used"
    mean = "/relationships/0/statistics/1"
    listed = [item["statistic"]["pointer"] for item in _namespace(tmp_path)["unused"]]
    assert mean in listed
    assert _states(result)[mean]["state"] == "unused"
    alone = tmp_path / "alone"
    alone.mkdir()
    _init(alone, _drop(_release([_mean(5000, 1000)]), [mean]), ONLY_A_KEY)
    assert generated(_namespace(alone)) == generated(_namespace(tmp_path))
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(_release([_mean(5000, 1000)]), listed), ONLY_A_KEY)
    assert generated(_namespace(cut)) == generated(_namespace(tmp_path))
