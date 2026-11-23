# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim explain`` names the causes a history, an as-of link and a projection raise.

``docs/how-to/give-an-entity-a-history.md`` sends a reader to these three pages for the refusals
of a history, so each page names each family of cause, one assertion for each.
"""

from __future__ import annotations

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit


CAUSES = [
    ("FLX-3403", "A history is refused when its emit has no validity window"),
    ("FLX-3403", "has no transition row for a level a chain can reach"),
    (
        "FLX-3403",
        "An asOf entry is refused when its relationship does not reach the natural entity",
    ),
    ("FLX-3403", "A snapshots, events or periods entry is refused when its of names no emit"),
    ("FLX-3403", "more periods than a run holds"),
    ("FLX-3403", "declares no range or bins, or a range or bins outside timeBounds"),
    ("FLX-3403", "a relationship whose parent is a periodic emit"),
    ("FLX-3403", "a before or after column maps a column the contract requires"),
    ("FLX-3403", "when a when reads a column the version does not hold"),
    ("FLX-3403", "a rule sets a column the contract requires from a row read as of a date"),
    ("FLX-3105", "A history, asOf, snapshots, events or periods entry is refused on kernel 0.1.0"),
    ("FLX-3105", "when an entry carries a key the block does not know"),
    ("FLX-6106", "When the emit that declares the window is a history"),
    ("FLX-6106", "tracked, validity or rule columns only through an `asOf` entry"),
]


@pytest.mark.parametrize(("code", "cause"), CAUSES)
def test_explaining_a_code_names_the_history_cause(
    code: str, cause: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["explain", code]) == 0
    assert cause in " ".join(capsys.readouterr().out.split())
