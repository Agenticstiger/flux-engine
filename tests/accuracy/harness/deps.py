# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the accuracy program needs beyond ``.[dev]``, and what happens without it.

The program's statistics come from scipy, its schema-driven property tests
from hypothesis-jsonschema and its timings from pytest-benchmark. None of them
is a runtime dependency of the engine, so they live in the ``accuracy``
dependency group in ``pyproject.toml`` and an ordinary ``pip install -e
".[dev]"`` does not have them.

Without the group a program module skips, with a reason that says how to
install it. That is the right answer for a contributor running the fast
suite. It is the wrong answer for the accuracy workflow, which must not be
able to go green because its own dependencies were missing: with
``FLUX_ACCURACY_REQUIRED=1`` the same call raises instead, the module fails
to collect, and the run fails loudly. This mirrors ``FLUX_EXAMPLES_REQUIRED``
for the sibling FLUX checkout (``tests/flux_examples.py``).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

__all__ = ["REQUIRED_ENV", "accuracy_group_is_required", "require"]

#: Set to ``1`` to turn a missing accuracy dependency into a failure.
REQUIRED_ENV = "FLUX_ACCURACY_REQUIRED"

_INSTALL_HINT = (
    "install it with `uv sync --extra dev --group accuracy` "
    'or `pip install -e ".[dev]" --group accuracy` (pip >= 25.1)'
)


def accuracy_group_is_required() -> bool:
    """Whether a missing accuracy dependency must fail rather than skip."""
    return os.environ.get(REQUIRED_ENV, "").strip() not in ("", "0")


def require(*modules: str) -> None:
    """Skip the calling module unless every one of *modules* is importable.

    Call it at module level, before the imports it guards. Under
    ``FLUX_ACCURACY_REQUIRED=1`` it raises :class:`ImportError` instead of
    skipping, so the module fails to collect.
    """
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    message = (
        f"the accuracy dependency group is not installed (missing: {', '.join(missing)}); "
        f"{_INSTALL_HINT}"
    )
    if accuracy_group_is_required():
        raise ImportError(f"{message}. {REQUIRED_ENV}=1 makes this a failure.")
    pytest.skip(message, allow_module_level=True)
