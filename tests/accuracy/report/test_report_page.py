# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 9: the committed report page is exactly what its artefact renders to.

Pass criteria:

* ``docs/accuracy/REPORT.md`` equals ``render(docs/accuracy/latest.json)``
  byte for byte, so the page cannot drift from the numbers or be edited by
  hand;
* the rendered page carries none of the reserved vocabulary the register lint
  bans from public surfaces (``flux_engine._vocabulary``), checked here as
  well as by ``make register-lint`` so the failure names the renderer;
* the page ends with its "What was not tested" section;
* rendering is deterministic, and a failed night renders as failed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

from flux_engine._vocabulary import find_banned_words
from tests.accuracy.report.render import NOT_TESTED, render

DOCS = Path(__file__).resolve().parents[3] / "docs" / "accuracy"
LATEST = DOCS / "latest.json"
REPORT = DOCS / "REPORT.md"


def _latest() -> Dict[str, Any]:
    assert LATEST.is_file(), f"{LATEST} is missing: commit a nightly artefact as the snapshot"
    document: Dict[str, Any] = json.loads(LATEST.read_text(encoding="utf-8"))
    assert document.get("schema") == "flux-accuracy-nightly/1", document.get("schema")
    return document


def test_the_committed_page_is_the_rendered_artefact() -> None:
    expected = render(_latest())
    actual = REPORT.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/accuracy/REPORT.md is not what docs/accuracy/latest.json renders to; run "
        "`python -m tests.accuracy.report.render --artefact docs/accuracy/latest.json "
        "--out docs/accuracy/REPORT.md`"
    )


def test_the_page_carries_no_reserved_vocabulary() -> None:
    found = find_banned_words(render(_latest()), mask_module_paths=True)
    assert found == (), found


def test_the_page_ends_with_what_was_not_tested() -> None:
    page = render(_latest())
    heading = page.rindex("## What was not tested")
    assert all(item in page[heading:] for item in NOT_TESTED)
    assert "##" not in page[heading + 2 :]


def test_rendering_is_deterministic_and_reports_a_failed_night() -> None:
    artefact = _latest()
    assert render(artefact) == render(copy.deepcopy(artefact))
    failed = copy.deepcopy(artefact)
    failed["verdict"] = {
        "passed": False,
        "reasons": ["gof-kernel: 1 case(s) failed (poisson-20)"],
        "missing": [],
        "flagged": [],
    }
    page = render(failed)
    assert "Verdict: **failed**" in page
    assert "poisson-20" in page


def test_the_page_and_the_design_note_name_the_same_untested_things() -> None:
    """The report's "What was not tested" and DESIGN.md's list cannot drift apart."""
    import re

    lead = re.compile(r"^\*\*(.+?)\.?\*\*")
    page = {lead.match(item).group(1) for item in NOT_TESTED if lead.match(item)}
    design = (DOCS / "DESIGN.md").read_text(encoding="utf-8")
    section = design[design.index("## What this program does not test") :]
    section = section[: section.find("\n## ", 3)] if "\n## " in section[3:] else section
    noted = {
        match.group(1).rstrip(".")
        for match in re.finditer(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE)
    }
    assert len(page) == len(NOT_TESTED)
    assert page == noted, (sorted(page - noted), sorted(noted - page))
