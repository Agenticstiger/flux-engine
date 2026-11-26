# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` widens a date column that is declared by bins alone, as it widens one that declares a range.

An enforced ``within`` date whose declared range the plan would refuse (``FLX-6204``) is written
with the range widened just enough to meet the plan's check. A date column whose published
statistics are quantile bins with no published minimum is declared by its bins and no ``range``,
and the plan reads the first and last edges as its range. The widening used to skip such a column,
so the plan asked again for the same span and ``init`` refused with ``FLX-6204``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.stages.apply import apply as apply_stage
from flux_engine.stages.plan import plan as plan_stage
from flux_engine.stages.verify import verify as verify_stage
from tests.cli.test_init_dates import (  # noqa: F401  (the autouse fixture reads the declarations first)
    CONTRACT,
    DAY,
    END,
    START,
    _declarations_are_read_before_any_trial,
    _iso,
    _late_child_profile,
    _levels,
    _link_pair,
    _profile,
    _ranged_contract,
    _size,
    _stamp,
    _written,
)
from tests.cli.test_init_empty_properties import _fidelity

pytestmark = pytest.mark.integration


def _quartiles(low: int, *, width: int = 65) -> List[Dict[str, Any]]:
    """Three quantile statistics on a ladder of four bins that start at *low*."""
    ladder = [str(low + step * width * DAY) for step in range(5)]
    found = []
    for level, bin_index in (("0.25", 0), ("0.5", 1), ("0.75", 2)):
        group = {
            "group": "",
            "k": 400,
            "quantileBin": bin_index,
            "binBelow": bin_index * 100,
            "binThrough": bin_index * 100 + 100,
            "status": "F",
        }
        found.append(
            {
                "definition": "quantile/2",
                "level": level,
                "bins": ladder,
                "sampleSize": _size(400),
                "disclosure": "none",
                "groups": [group],
            }
        )
    return found


def _bins_only(profile: Dict[str, Any], low: int) -> Dict[str, Any]:
    """*profile* whose invoice dates publish a maximum and quantiles from *low*, and no minimum."""
    column = _stamp(low, END, maximum_only=True)
    column["statistics"] = _quartiles(low)
    profile["tables"]["invoice"]["columns"]["issued_at"] = column
    return profile


def _issued_at(written: Any) -> Dict[str, Any]:
    column = _fidelity(written, "invoice")["columns"]["issued_at"]
    assert isinstance(column, dict)
    return column


def test_a_child_date_declared_by_bins_alone_is_widened_to_its_parents_lifetime(
    tmp_path: Path,
) -> None:
    low = START + 100 * DAY
    written = _written(tmp_path, _bins_only(_late_child_profile(), low))
    (entry,) = [item for item in written.within if item.get("date") == "issued_at"]
    assert "target" not in entry  # the fact stays enforced
    column = _issued_at(written)
    # The range is the plan's: the first edge moved to the parents' earliest end with it.
    assert column["range"]["min"].startswith(_iso(START)[:19])
    assert column["bins"][0] == column["range"]["min"]
    assert column["bins"][-1] == column["range"]["max"]
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    verified = verify_stage(
        written.directory,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )
    assert "within:invoice_to_customer.issued_at 400 of 400" in verified.stdout, verified.stdout


def test_a_date_declared_by_bins_that_needs_no_widening_declares_no_range(tmp_path: Path) -> None:
    # The column starts before its parents do: nothing asks for a wider range, so none is written
    # and the declaration is the bins and their weights alone, as it was.
    written = _written(tmp_path, _bins_only(_late_child_profile(), START - 200 * DAY))
    column = _issued_at(written)
    assert "range" not in column
    assert set(column) == {"bins", "binWeights"}


def test_a_bins_only_date_is_not_widened_past_the_range_its_contract_declares(
    tmp_path: Path,
) -> None:
    low = START + 100 * DAY
    contract = _ranged_contract(low, END)
    assert contract != CONTRACT
    written = _written(tmp_path, _bins_only(_late_child_profile(), low), contract)
    assert not [item for item in written.within if item.get("date") == "issued_at"]
    column = _issued_at(written)
    assert "range" not in column
    assert column["bins"][0].startswith(_iso(low + 65 * DAY)[:19])  # the first edge did not move


def test_a_bins_only_date_is_widened_at_its_last_edge_too(tmp_path: Path) -> None:
    # Parents start until a hundred days after the child date's last day: the plan raises the
    # maximum, and the last bin edge, which sat on it, goes with it.
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    profile = _bins_only(_profile(links=[link]), START - 10 * DAY)
    profile["tables"]["invoice"]["columns"]["issued_at"]["metrics"]["max"]["value"] = str(
        END - 100 * DAY
    )
    written = _written(tmp_path, profile)
    (entry,) = [item for item in written.within if item.get("date") == "issued_at"]
    assert "target" not in entry
    column = _issued_at(written)
    assert column["range"]["max"].startswith(_iso(END)[:19])
    assert column["bins"][-1] == column["range"]["max"]
