# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``--help`` says about scoring a run against a profile.

``run --help`` named neither the property kinds a profile can score (``rankCorrelation``, a
property grouped by the bins of a column) nor the children-per-parent row of a ``counts`` entry,
though ``verify --help`` named the last; and ``forecast score --help`` gave one of the two rules
for ``--observed-at``.
"""

from __future__ import annotations

from typing import List

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit


def _help(argv: List[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as stop:
        main([*argv, "--help"])
    assert stop.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def test_run_help_names_the_children_per_parent_row_and_the_newer_property_kinds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _help(["run"], capsys)
    assert "childrenPerParent row" in text
    assert "rankCorrelation" in text and "groupByBins" in text
    assert "outside the entry's tolerance" in text


def test_forecast_score_help_gives_the_horizon_rule_beside_the_registration_rule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _help(["forecast", "score"], capsys)
    assert "(FLX-7001)" in text and "(FLX-7013)" in text
    assert text.index("FLX-7001") < text.index("FLX-7013")
