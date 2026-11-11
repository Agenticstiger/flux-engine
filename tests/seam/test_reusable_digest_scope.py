# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``G`` is blind to exactly three declarations -- by POSITION, not by name.

SEAM-FIDELITY-SPEC §8's change table: "tolerance / minVariance /
maxStateAge only -> H moves, G unchanged"; "any other declaration in the
block or the contract -> G moves -> new committed state required".

The stripper used to remove any mapping key literally named ``tolerance``
or ``minVariance`` at arbitrary depth. A contract with a *column* called
``tolerance`` -- a plausible name in a measurement or engineering
contract -- therefore had its entire ``fidelity.columns.tolerance`` entry
(range, support, null policy, generator family) erased from ``G``, so
editing that column's declared range left ``G`` unmoved and kept reusing
a committed state formed under the old range. §8 says that must force a
new committed state.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from flux_engine.seam.emit import seam_digest, seam_reusable_digest

pytestmark = pytest.mark.unit

_CONTRACT: Dict[str, Any] = {"apiVersion": "fluid/v1", "kind": "DataProduct"}
_COLUMNS = ["tolerance", "amount"]


def _extension(*, tolerance_max: str = "100", property_tolerance: str = "50") -> Dict[str, Any]:
    return {
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 100000},
            "columns": {
                "tolerance": {
                    "kind": "continuous",
                    "range": {"min": "0", "max": tolerance_max},
                    "nullPolicy": "forbidden",
                },
                "amount": {
                    "kind": "continuous",
                    "range": {"min": "0", "max": "1000"},
                    "nullPolicy": "forbidden",
                },
            },
            "properties": [
                {
                    "property": "mean",
                    "columns": ["amount"],
                    "tolerance": {"type": "absolute", "value": property_tolerance},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
        }
    }


def _digests(extension: Dict[str, Any]):
    emit: Dict[str, Any] = {"exposeId": "e", "productRef": "p"}
    return (
        seam_digest(_CONTRACT, emit, _COLUMNS, extension=extension).value,
        seam_reusable_digest(_CONTRACT, emit, _COLUMNS, extension=extension).value,
    )


def test_editing_a_column_named_tolerance_moves_g() -> None:
    base_h, base_g = _digests(_extension())
    moved_h, moved_g = _digests(_extension(tolerance_max="999999"))
    assert moved_h != base_h, "H must move on any declaration change"
    assert moved_g != base_g, (
        "G must move when a column's declared range changes -- even a column called " "'tolerance'"
    )


def test_editing_a_property_tolerance_leaves_g_unchanged() -> None:
    """The positive control for the exclusion §8 does grant."""
    base_h, base_g = _digests(_extension())
    moved_h, moved_g = _digests(_extension(property_tolerance="75"))
    assert moved_h != base_h
    assert moved_g == base_g


def test_editing_budget_max_state_age_leaves_g_unchanged() -> None:
    extension = _extension()
    edited = copy.deepcopy(extension)
    edited["fidelity"]["budget"]["maxStateAge"] = "P7D"
    base_h, base_g = _digests(extension)
    moved_h, moved_g = _digests(edited)
    assert moved_h != base_h
    assert moved_g == base_g


def test_editing_budget_max_checks_per_state_moves_g() -> None:
    extension = _extension()
    edited = copy.deepcopy(extension)
    edited["fidelity"]["budget"]["maxChecksPerState"] = 21
    assert _digests(edited)[1] != _digests(extension)[1]
