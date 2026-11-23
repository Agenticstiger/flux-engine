# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The pages that introduce ``verify`` name ``fluxsim run --verify`` as the one-pass path.

``plan`` stages its run and ``apply --run-id`` commits the stage (ADR 0015), and
``verify`` reads each emit once, so ``run --verify`` is the way to draw the rows
once and check them in one command. A reader who meets ``verify`` on one of these
pages should be told so there, in the paragraph that shows the command, rather
than find it only in the changelog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The README, and the tutorial and how-to pages that first show ``run`` with ``--verify``.
PAGES = (
    "README.md",
    "docs/tutorial/first-evidence-pack.md",
    "docs/how-to/score-a-run-against-a-profile.md",
    "docs/how-to/write-a-bundle-from-a-profile.md",
    "docs/how-to/ship-with-evidence.md",
)


def _paragraphs(page: str) -> list[str]:
    return (REPO_ROOT / page).read_text(encoding="utf-8").split("\n\n")


@pytest.mark.parametrize("page", PAGES)
def test_the_page_names_run_verify_as_the_one_pass_path(page: str) -> None:
    named = [paragraph for paragraph in _paragraphs(page) if "one-pass path" in paragraph]
    assert named, f"{page} never calls `run --verify` the one-pass path"
    assert any(
        "run" in paragraph and "--verify" in paragraph for paragraph in named
    ), f"{page} says 'one-pass path' in a paragraph that does not name `run` with `--verify`"
