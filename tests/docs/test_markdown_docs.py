# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which Markdown pages the harness runs, and that none is left to chance.

The pages themselves run in ``test_run_pages.py``, which is slow. This module is the
cheap half and runs with the fast suite. It checks that every Markdown file in the
repository is either run or left out for a stated reason, that a page which is run
uses only the kinds of block the harness understands, and that skipping a block
stays a decision with a reason instead of a habit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import (
    ACCOUNTED_LANGUAGES,
    LEFT_OUT,
    OWNED_ELSEWHERE,
    RUN_LANGUAGES,
    TESTED,
    all_markdown,
    fence_languages,
    skips,
    tested_pages,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES = tested_pages(REPO_ROOT)

#: Blocks left out with ``<!-- skip: next "reason" -->`` across all tested pages. This
#: is the number there are today, not a margin: a block that cannot run in CI (it
#: needs the network, or only exists to show a command a reader runs on their own
#: machine) is a decision, so adding one means raising this number in the same change
#: and saying why.
MAX_SKIPPED_BLOCKS = 2

pytestmark = pytest.mark.unit


def _name(page: Path) -> str:
    return page.relative_to(REPO_ROOT).as_posix()


def _is_inside(page: Path, entry: Path) -> bool:
    return page == entry or entry in page.parents


def test_there_are_pages_to_run() -> None:
    assert len(PAGES) >= 1


def test_every_page_is_tested_or_left_out_on_purpose() -> None:
    covered = {path.resolve() for path in PAGES}
    left_out = tuple((REPO_ROOT / entry).resolve() for entry in (*LEFT_OUT, *OWNED_ELSEWHERE))
    strays = []
    for page in all_markdown(REPO_ROOT):
        resolved = page.resolve()
        if resolved in covered:
            continue
        if any(_is_inside(resolved, entry) for entry in left_out):
            continue
        strays.append(_name(page))
    assert strays == [], (
        "these pages are neither run nor listed in LEFT_OUT with a reason "
        f"(tests/docs/harness.py): {strays}"
    )


def test_nothing_is_both_run_and_left_out() -> None:
    covered = {path.resolve() for path in PAGES}
    for entry in (*LEFT_OUT, *OWNED_ELSEWHERE):
        folder = (REPO_ROOT / entry).resolve()
        inside = sorted(_name(page) for page in covered if _is_inside(page, folder))
        assert inside == [], f"{entry} is left out, yet these pages of it are run: {inside}"


def test_the_left_out_list_names_things_that_exist() -> None:
    for entry in [*LEFT_OUT, *TESTED]:
        assert (REPO_ROOT / entry).exists(), entry


def test_every_entry_has_a_reason() -> None:
    for entry, reason in {**LEFT_OUT, **OWNED_ELSEWHERE}.items():
        assert len(reason.split()) >= 3, f"{entry}: say why it is left out"


@pytest.mark.parametrize("page", PAGES, ids=_name)
def test_every_fence_on_the_page_is_one_the_harness_runs_or_checks(page: Path) -> None:
    unknown = {
        language
        for language in fence_languages(page.read_text(encoding="utf-8"))
        if language not in ACCOUNTED_LANGUAGES
    }
    assert unknown == set(), (
        f"{_name(page)} uses fenced blocks nobody runs: {sorted(unknown)}; use console, bash, "
        "python, yaml, json or text"
    )


def test_skipped_blocks_are_few_and_each_has_a_reason() -> None:
    found = [(page, skip) for page in PAGES for skip in skips(page.read_text(encoding="utf-8"))]
    assert len(found) <= MAX_SKIPPED_BLOCKS, [(_name(page), skip.line) for page, skip in found]
    for page, skip in found:
        where = f"{_name(page)}:{skip.line}"
        # `start` and `end` would leave out every block between them, and `if(...)` leaves
        # one out only sometimes. One form is allowed: the next block, with a reason.
        assert skip.reason is not None, f'{where}: a skip must read <!-- skip: next "why" -->'
        assert len(skip.reason.split()) >= 3, f"{where}: say why in words, not {skip.reason!r}"
        # `skip: next` leaves out the next block Sybil runs, which is not always the next
        # fence: it hops over a `text` block and takes the console block after it.
        assert (
            skip.before in RUN_LANGUAGES
        ), f"{where}: a skip must come directly before the block it leaves out"
