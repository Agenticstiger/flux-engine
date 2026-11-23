# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Where the sibling FLUX specification repository is, and whether it may be absent.

Thirteen conftests and test modules gate on the FLUX example bundle being
checked out beside this repository. CI checks out only ``flux-engine``, so
every one of those gates used to disappear silently -- 226 skipped tests,
including the entire real-binary six-step acceptance success path, the
public evidence-archive verify/tamper tests, the public golden regression
and the whole runtime telco end-to-end. The suite still reported green and
the coverage gate still passed at 95%, because coverage is measured over
what ran.

A skip is the right answer for a developer who has not cloned the sibling
repo. It is the wrong answer for CI, which must not be able to lose a
whole class of tests by omitting a checkout step. Setting
``FLUX_EXAMPLES_REQUIRED=1`` turns every one of those skips into a
failure; the CI workflow sets it.

Eight of those thirteen gates hand-rolled their own ``skipif`` instead of
using the helpers here, so under ``FLUX_EXAMPLES_REQUIRED=1`` and no
sibling checkout they still SKIPPED -- about 40 tests, the real-binary
six-step acceptance success path and the public evidence-archive
verify/tamper tests among them. Rewriting eight call sites would have
closed eight instances of the hole, not the hole: a ninth hand-rolled
gate reopens it. So the guarantee is enforced once, at the reporting
boundary -- ``tests/conftest.py``'s ``pytest_runtest_makereport`` turns
any skip whose reason names the sibling checkout
(:func:`is_flux_examples_skip_reason`) into a failure while
:func:`examples_are_required` is true, whichever gate produced it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

__all__ = [
    "FLUX_REPO",
    "REQUIRED_ENV_VAR",
    "examples_are_required",
    "flux_example_dir",
    "is_flux_examples_skip_reason",
    "requires_flux_examples",
    "skip_or_fail_without_flux_examples",
]

#: The FLUX specification repository, checked out beside this one.
FLUX_REPO = Path(__file__).resolve().parents[1].parent / "FLUX"

#: Set this in any environment where a missing checkout is a bug, not a
#: developer convenience.
REQUIRED_ENV_VAR = "FLUX_EXAMPLES_REQUIRED"


#: A skip reason belongs to a sibling-FLUX gate when it names the FLUX
#: repository *and* says it is absent. Every such gate in this suite is
#: phrased that way -- "sibling FLUX spec checkout not found at ...",
#: "the FLUX example bundle is not checked out at ...", "the FLUX spec
#: repo's telco example is not checked out beside this repo".
_ABSENCE_PHRASES = ("not checked out", "not found", "checkout not", "is not a directory")


def is_flux_examples_skip_reason(reason: str) -> bool:
    """Is *reason* a skip because the sibling FLUX checkout is missing?

    Deliberately phrase-based rather than a registry of call sites: the
    hole this closes was eight gates that did not use the helpers in this
    module, and a registry only ever lists the gates someone remembered
    to register.
    """
    lowered = reason.lower()
    if "flux" not in lowered:
        return False
    if str(FLUX_REPO).lower() in lowered:
        return True
    return any(phrase in lowered for phrase in _ABSENCE_PHRASES)


def examples_are_required() -> bool:
    return os.environ.get(REQUIRED_ENV_VAR, "").strip().lower() not in ("", "0", "false", "no")


def flux_example_dir(name: str = "telco-payment-recovery") -> Optional[Path]:
    """The named example bundle directory, or ``None`` when it is not checked out."""
    candidate = FLUX_REPO / "examples" / name
    return candidate if candidate.is_dir() else None


def skip_or_fail_without_flux_examples(name: str = "telco-payment-recovery") -> Path:
    """Return the example directory, skipping -- or FAILING under CI -- without it."""
    found = flux_example_dir(name)
    if found is not None:
        return found
    message = f"the FLUX example bundle is not checked out at {FLUX_REPO / 'examples' / name}"
    if examples_are_required():
        pytest.fail(f"{REQUIRED_ENV_VAR} is set but {message}")
    pytest.skip(message)


#: A ``skipif`` marker for the collection-time gates. Under
#: ``FLUX_EXAMPLES_REQUIRED`` the condition is never true, so the tests are
#: collected and fail on the missing bundle rather than vanishing.
requires_flux_examples = pytest.mark.skipif(
    not (FLUX_REPO / "examples" / "telco-payment-recovery").is_dir()
    and not examples_are_required(),
    reason=(
        "the FLUX example bundle is not checked out beside this repo "
        f"(set {REQUIRED_ENV_VAR}=1 to make this a failure instead)"
    ),
)
