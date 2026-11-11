# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The repository's prose carries nothing that was only meant for the people building it.

A banner saying the repository is private, wording about a freeze on some
vocabulary, a job title, and a reference to a plan nobody outside the project can
read were all true or useful once, and they are wrong or confusing in a public
repository. Each pattern below is a leak this repository has had. A pattern here
turns the next one into a failing test instead of the next audit.

The files checked are the ones git tracks, plus any new file git does not ignore,
so a developer's virtual environment or build output is never read. A phrase
wrapped across two lines is still found, because the patterns run over the whole
text and let any white space separate the words.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
_SUFFIXES = frozenset({".md", ".py", ".toml", ".yml", ".yaml", ".cff"})

#: What each pattern catches. Keys read as the finding in a failure message.
LEAKS: Dict[str, "re.Pattern[str]"] = {
    "a banner saying the repository is private": re.compile(
        r"(?i)private\s+repo(?:sitory)?\s+only"
    ),
    "wording about a freeze on vocabulary": re.compile(r"(?i)freeze\s+list|lifts?\s+the\s+freeze"),
    "a job title in prose": re.compile(r"\bCEO\b"),
    "a reference to a plan nobody outside the project can read": re.compile(
        r"(?i)\b(?:approved|build)\s+plan\b"
    ),
}


def _text_files() -> Iterator[Path]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout, so there is no list of files to check")
    for name in sorted(filter(None, listing.split("\0"))):
        path = REPO_ROOT / name
        # This file names every pattern, so it would match itself.
        if path.suffix in _SUFFIXES and path.is_file() and path != Path(__file__).resolve():
            yield path


def test_there_is_prose_to_check() -> None:
    assert sum(1 for _ in _text_files()) > 100


@pytest.mark.parametrize("finding", sorted(LEAKS))
def test_the_prose_has_no_leak_of(finding: str) -> None:
    pattern = LEAKS[finding]
    hits: List[str] = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {match.group(0)!r}")
    assert hits == [], f"{finding}:\n" + "\n".join(hits)


def test_a_phrase_wrapped_across_two_lines_is_found() -> None:
    pattern = LEAKS["a reference to a plan nobody outside the project can read"]
    assert pattern.search("The build\nplan requires a frozen grammar")
    assert not pattern.search("The plan is built in the plan stage")
