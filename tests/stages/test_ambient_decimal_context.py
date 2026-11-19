# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run is a function of its bundle and its seed, not of the caller's ``decimal`` context.

KERNEL-SPEC §4 pins the context every continuous draw is evaluated in
(``flux_engine.core.rng.kernel_context``) so that "a caller's ambient
``decimal`` context cannot move a hashed value". Three places in a run still
did their arithmetic in whatever context the caller had installed:

* the arrival model's gap rate, ``Decimal(1) / gap_mean_ms``
  (``runtime/signals.py``), so a caller at a precision other than 28 got
  different gaps, different ``events.ndjson`` and a different run digest;
* a scalar Persona trait's quantisation onto ``TRAIT_SCALE``
  (``world/population.py``), which raised ``InvalidOperation`` at a precision
  below the trait's digits;
* the equal Persona share, ``Decimal(1) / n`` (``world/population.py``).

The CLI starts every process at the default context, which equals the
kernel's, so it never showed. A library caller -- the ``engine.py`` facade,
a notebook, a service that sets ``decimal.getcontext()`` for its own money
arithmetic -- got a run that did not replay.

Found by the accuracy program's determinism and property families
(docs/accuracy/DESIGN.md).
"""

from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, ROUND_UP, Context, Decimal, localcontext
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.stages.apply import apply
from flux_engine.world.population import PersonaSpec, _equal_share

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
#: The run files whose bytes are a function of the bundle and the seed alone.
COMPARED = (
    "population.ndjson",
    "events.ndjson",
    "seed-paths.ndjson",
    "emits/payment_recovery_moment.ndjson",
    "run-digest.json",
)

HOSTILE = {
    "precision-5": Context(prec=5),
    "precision-6": Context(prec=6),
    "precision-7-round-up": Context(prec=7, rounding=ROUND_UP),
    "precision-60-round-floor": Context(prec=60, rounding=ROUND_FLOOR),
}


def _run_files(state_dir: Path) -> Dict[str, str]:
    result = apply(BUNDLE, state_dir=state_dir)
    assert result.success, result.stderr
    run_dir = state_dir / "runs" / str(result.artifacts["run_id"])
    return {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest() for name in COMPARED}


@pytest.fixture(scope="module")
def default_run(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    return _run_files(tmp_path_factory.mktemp("default") / ".flux")


def test_the_default_context_is_reproducible(default_run: Dict[str, str], tmp_path: Path) -> None:
    assert _run_files(tmp_path / ".flux") == default_run


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_run_ignores_the_ambient_context(
    name: str, default_run: Dict[str, str], tmp_path: Path
) -> None:
    with localcontext(HOSTILE[name]):
        files = _run_files(tmp_path / ".flux")
    assert files == default_run


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_scalar_trait_quantises_in_the_kernel_context(name: str) -> None:
    persona = {"id": "p", "spec": {"traits": {"digitalFluency": "0.6"}}}
    expected = PersonaSpec.from_document(persona).traits["digitalFluency"]
    with localcontext(HOSTILE[name]):
        actual = PersonaSpec.from_document(persona).traits["digitalFluency"]
    assert actual == expected
    assert str(actual.draw(lambda: None)) == "0.600000"  # type: ignore[arg-type,return-value]


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_the_equal_share_ignores_the_ambient_context(name: str) -> None:
    names = ["a", "b", "c"]
    expected = _equal_share(names, 10)
    with localcontext(HOSTILE[name]):
        actual = _equal_share(names, 10)
    assert actual == expected
    assert sum(expected.values(), Decimal(0)) == 1
