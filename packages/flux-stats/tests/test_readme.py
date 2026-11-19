# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The examples in this package's README run, and print what the page says.

The README is what PyPI shows, so an example that stopped being true would
be the first thing a reader of the package copies. The page's Python blocks
are written as interactive sessions, and each is run here as a
:mod:`doctest`, from the root of the repository, as the page says to run
them.
"""

from __future__ import annotations

import doctest
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[1] / "README.md"
_BLOCK = re.compile(r"^```python\n(?P<body>.*?)^```$", re.DOTALL | re.MULTILINE)


def _repository_root() -> Path:
    """The flux-engine checkout this package sits in, found by its specification folder."""
    for parent in README.parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent
    raise RuntimeError("the README's examples read docs/spec from the checkout around them")


def _blocks() -> List[Tuple[int, str]]:
    """Each Python block of the page, with the line it starts on."""
    text = README.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, match.start("body")) + 1, match.group("body"))
        for match in _BLOCK.finditer(text)
    ]


def test_the_readme_has_its_examples() -> None:
    assert len(_blocks()) == 3, "the README's Python examples changed; update this test"


def test_the_readme_examples_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_repository_root())
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS)
    globs: Dict[str, Any] = {}
    for line, body in _blocks():
        test = parser.get_doctest(body, globs, f"README.md:{line}", str(README), line - 1)
        assert test.examples, f"README.md:{line} has no example to run"
        runner.run(test, clear_globs=False)
    result = runner.summarize(verbose=False)
    assert result.failed == 0, f"{result.failed} README example(s) failed; see the output above"
