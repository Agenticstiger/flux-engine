# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The list that keeps the pull-request gate fast cannot rot into a lie.

``tests/conftest.py`` names the modules outside ``golden/`` and
``acceptance/`` that materialise a 25,000-subject world. A hand-kept list
is only as good as the check on it: a renamed or deleted module would
leave an entry that silences nothing, and a marker that stopped being
applied would leave the entry looking like it still did.

Both are asserted here -- the paths exist, and the marker really lands on
the tests they hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Package-qualified, never the bare name ``conftest``: seven test
# directories share that basename and a bare import resolves to whichever
# one the run collected first (see tests/governance/__init__.py).
from tests.conftest import SLOW_DIRS, SLOW_MODULES

TESTS_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("relative", SLOW_MODULES)
def test_every_listed_slow_module_still_exists(relative: str) -> None:
    assert (
        TESTS_ROOT / relative
    ).is_file(), f"{relative} is listed as slow in tests/conftest.py but is not there any more"


@pytest.mark.parametrize("name", SLOW_DIRS)
def test_every_slow_directory_still_exists(name: str) -> None:
    assert (TESTS_ROOT / name).is_dir()


@pytest.mark.parametrize("relative", SLOW_MODULES)
def test_a_listed_module_collects_only_slow_tests(relative: str, pytestconfig) -> None:
    """The marker lands. Asserted by collecting the module, not by reading the rule."""
    result = pytest.main(
        [
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not slow",
            str(TESTS_ROOT / relative),
        ]
    )
    # Exit code 5 is "no tests collected": every test in the module was
    # deselected as slow, which is the whole point of listing it.
    assert (
        result == pytest.ExitCode.NO_TESTS_COLLECTED
    ), f"{relative} still contributes tests to the `not slow` gate"
