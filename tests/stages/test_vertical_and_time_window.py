# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A World with no vertical, and a Simulation with no time window, are refused by code.

The FLUX schema leaves ``World.spec.vertical`` and ``Simulation.spec.timeBounds``
optional, so the offline validator passes a bundle without them, and so did
``validate``. ``plan`` then stopped with a Python ``KeyError``: "unknown vertical
''" from the pack lookup, or ``'start'`` from the clock. These tests hold both
commands to one refusal per case, with its code and a message that names the
missing key and the fix, in the order ``plan`` meets them, and hold ``plan`` to
writing nothing when it refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from flux_engine.core.clock import FLX_5023, declared_time_bounds
from flux_engine.core.errors import FluxError
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.verticals import FLX_5042, TOWERS, pack_of_world
from tests.bundles import bundle_with_text

from .conftest import FIXTURE_BUNDLE

SIMULATION_ID = "sim-payment-recovery"

#: The fixture's own declarations, each removed or changed by one case below.
VERTICAL = "  vertical: telco\n"
TIME_BOUNDS = (
    "  timeBounds:\n" '    start: "2026-07-01T00:00:00Z"\n' '    end: "2026-09-30T23:59:59Z"\n'
)

NO_VERTICAL = (
    "FLX-5042: the World names no vertical this engine ships -- World 'q3-retention-world' "
    "declares no spec.vertical; add one under its spec, naming a pack this engine ships "
    "(telco, towers), for example 'vertical: telco'"
)
UNKNOWN_VERTICAL = (
    "FLX-5042: the World names no vertical this engine ships -- World 'q3-retention-world' "
    "declares spec.vertical 'banking', which this engine does not ship; use a pack it ships "
    "(telco, towers)"
)
NO_TIME_BOUNDS = (
    "FLX-5023: the Simulation declares no time window -- Simulation 'sim-payment-recovery' "
    "declares no spec.timeBounds; add the window the run covers under its spec, for example "
    '\'timeBounds: {start: "2026-07-01T00:00:00Z", end: "2026-09-30T23:59:59Z"}\''
)

#: ``(file, old text, new text)``: the one edit a case makes to a copy of the fixture.
Edit = Tuple[str, str, str]

CASES = [
    pytest.param(("world.flux.yml", VERTICAL, ""), "FLX-5042", NO_VERTICAL, id="no-vertical"),
    pytest.param(
        ("world.flux.yml", VERTICAL, "  vertical: banking\n"),
        "FLX-5042",
        UNKNOWN_VERTICAL,
        id="unknown-vertical",
    ),
    pytest.param(
        ("simulation.flux.yml", TIME_BOUNDS, ""), "FLX-5023", NO_TIME_BOUNDS, id="no-time-bounds"
    ),
]


def _edited(tmp_path: Path, edit: Edit) -> Path:
    return bundle_with_text(FIXTURE_BUNDLE, tmp_path / "bundle", *edit)


@pytest.mark.parametrize(("edit", "code", "expected"), CASES)
def test_validate_refuses_with_the_code_and_the_fix(
    tmp_path: Path, edit: Edit, code: str, expected: str
) -> None:
    result = validate(_edited(tmp_path, edit))

    assert (result.success, result.error_code) == (False, code)
    assert result.stderr == f"{SIMULATION_ID}: {expected}"


@pytest.mark.parametrize(("edit", "code", "expected"), CASES)
def test_plan_refuses_as_validate_does_and_writes_nothing(
    tmp_path: Path, edit: Edit, code: str, expected: str
) -> None:
    state = tmp_path / ".flux"

    result = plan(_edited(tmp_path, edit), state_dir=state)

    assert (result.success, result.error_code, result.stderr) == (False, code, expected)
    assert not state.exists()


def test_a_bundle_missing_both_is_refused_for_the_vertical_first(tmp_path: Path) -> None:
    """The quickstart's shape: ``validate`` and ``plan`` name the same refusal first."""
    no_vertical = bundle_with_text(FIXTURE_BUNDLE, tmp_path / "one", "world.flux.yml", VERTICAL, "")
    bundle = bundle_with_text(
        no_vertical, tmp_path / "both", "simulation.flux.yml", TIME_BOUNDS, ""
    )

    assert validate(bundle).stderr == f"{SIMULATION_ID}: {NO_VERTICAL}"
    assert plan(bundle, state_dir=tmp_path / ".flux").stderr == NO_VERTICAL


def test_the_lookups_return_what_the_documents_declare() -> None:
    assert pack_of_world({"id": "w", "spec": {"vertical": "towers"}}) is TOWERS
    simulation = {"id": "s", "spec": {"timeBounds": {"start": "2026-01-01T00:00:00Z", "end": "x"}}}
    assert declared_time_bounds(simulation) == ("2026-01-01T00:00:00Z", "x")


def test_the_lookups_refuse_a_document_with_no_spec() -> None:
    with pytest.raises(FluxError) as world:
        pack_of_world({"id": "w"})
    with pytest.raises(FluxError) as simulation:
        declared_time_bounds({"id": "s"})
    assert world.value.code is FLX_5042
    assert simulation.value.code is FLX_5023
