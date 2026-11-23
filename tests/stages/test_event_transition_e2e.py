# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run of change events that map only some of a history's tracked columns passes its own verify.

Every run is ``fluxsim plan``, ``apply`` and ``verify`` in a subprocess. The bundles:

* ``history-events-partial``: a history that tracks ``plan`` and ``seats`` under ``finalVersion``, whose events
  map ``plan`` alone and give a text to each reason. A ``u`` that moves ``seats`` shows no mapped column moving.
* ``history-events-closelast``: a history that tracks ``firmware`` and ends under ``closeLast`` with an ``end``
  column, whose events map ``firmware`` and neither expose ``status`` nor give a text for ``end``, so the ending
  change shows no mapped column moving.

``event:transition_matches`` passes on both, at several seeds, with every ``u`` counted.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from .conftest import REPO_ROOT
from .test_history_window_e2e import Run, _apply, _lines, _plan, _verify

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "fixtures"
RULE = "event:transition_matches"


def _seeded(name: str, tmp_path: Path, seed: int) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURES / name, target)
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count("seed: 42") == 1
    path.write_text(text.replace("seed: 42", f"seed: {seed}"), "utf-8")
    return target


def _transition_line(bundle: Path, tmp_path: Path, emit: str) -> Dict[str, Any]:
    """Plan, apply and verify *bundle*, and the ``event:transition_matches`` line of *emit*."""
    state = tmp_path / "state"
    layout = _plan(bundle, state)
    _apply(bundle, layout, state)
    verified = _verify(Run(bundle, state, layout))
    assert verified["exitCode"] == 0
    line: Dict[str, Any] = _lines(verified)[emit][RULE]
    assert line["met"] is True and line["enforced"] is True
    assert line["satisfied"] == line["total"] > 0
    return line


@pytest.mark.parametrize(
    ("name", "emit"),
    [
        ("history-events-partial", "subscription_change"),
        ("history-events-closelast", "device_change"),
    ],
)
@pytest.mark.parametrize("seed", [1, 2, 42])
def test_a_run_whose_events_map_some_columns_passes_the_transition_line(
    tmp_path: Path, name: str, emit: str, seed: int
) -> None:
    _transition_line(_seeded(name, tmp_path, seed), tmp_path, emit)


def test_a_hidden_closing_ending_passes_when_on_leaves_the_d_out(tmp_path: Path) -> None:
    bundle = _seeded("history-events-closelast", tmp_path, 42)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    old = "      op: op\n      before:\n        before_fw: firmware"
    assert text.count(old) == 1
    path.write_text(
        text.replace(old, "      op: op\n      'on': [u]\n" + old[len("      op: op\n") :]), "utf-8"
    )
    _transition_line(bundle, tmp_path, "device_change")
