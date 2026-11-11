# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The documentation, run as tests.

Every page that a reader is told to follow is executed: the commands run, the
output they print is compared with what the page shows, and the files the page
tells the reader to create are created. ``harness.py`` says what each kind of
block means. A page that stops being true fails here, on the line of the block.

These tests are slow (a page starts ``fluxsim`` once per command it shows, and
most pages take more than five seconds), so ``tests/conftest.py`` names this
module in ``SLOW_MODULES``: a pull request runs the pages once, in the slow
shard, instead of once per cell of the test matrix. (The scheduled and release
runs, which run every slow test on each of their cells, run them there too.)
``make docs-test`` runs only these, for someone writing a page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import run_page, skips, tested_pages

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES = tested_pages(REPO_ROOT)

pytestmark = pytest.mark.integration


def _name(page: Path) -> str:
    return page.relative_to(REPO_ROOT).as_posix()


@pytest.mark.parametrize("page", PAGES, ids=_name)
def test_the_page_does_what_it_says(page: Path, tmp_path: Path) -> None:
    report = run_page(page, tmp_path)
    named = len(skips(page.read_text(encoding="utf-8")))
    assert len(report.skipped) == named, (
        f"{_name(page)} has {named} skip directive(s) but {len(report.skipped)} block(s) were "
        "left out; a skip leaves out the one block after it"
    )
