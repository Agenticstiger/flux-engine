# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole number in a categorical column reads as the category of its decimal text, and an old run still verifies.

``flux_stats.reading.category`` (and its mirrors in ``flux_engine.concordance.shape`` and
``decoded``) read a whole number of a categorical column as the category of its decimal text,
as they read a boolean (SEAM-FIDELITY-SPEC section 2). Version 0.9.0 refused it
(``FLX-5005``, ``value class 'int'``). What that left behind, and what this module pins, was found by running
the same bundle on origin/main (v0.9.0, 2739cae) and on this branch:

* A 0.9.0 run whose categorical property reads an INT column exists. ``validate``, ``plan`` and
  ``apply`` accept the bundle, and only ``verify`` refused it: each property that read the
  column was ``refused, not compared (FLX-5005)``, the fidelity leg failed, and the shape of the
  column was ``not read``. The run's own record, its ``runDigest``, is a function of its rows
  and not of any verdict, so it is the same on both versions.
* ``verify``'s reproducibility compares that ``runDigest`` with the golden baseline
  (``<state_dir>/golden/<simulationId>.json``), which holds the kernel version, the baseline
  digest and the run's components and no verdict. Nothing else compares a verdict of an earlier
  verify with this one: ``ship`` forms its scorecard and its shape flags from the rows each time.
  So re-verifying a run made before cannot report it not reproducible because a property now
  scores; the new reading only turns a refusal that was a fidelity failure into a comparison.

The bundle is the one ``fluxsim init`` writes for the profile of ``tests/author/test_given_int``
in format 1-2-0, an INT column ``rating`` drawn over three integer levels with the column
``verdict`` drawn given it, and a ``levelHistogram`` property over each.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.cli._init import run_init
from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.author.test_given_int import CONTRACT, _profile
from tests.overlays import with_the_profile_named, without_replacements

pytestmark = pytest.mark.integration

#: What origin/main (v0.9.0) gives for this bundle, planned and applied with the profile it pins.
MAIN_RUN_ID = "a3fbc5a36c1f48dd"
MAIN_RUN_DIGEST = "sha256:2e8430de6073a20c07063294eb8fd5b24cf1cc94de06a37bfa92870688c86cd9"
MAIN_PLAN_DIGEST = "sha256:a3fbc5a36c1f48dd2e5a9e77a1af989293a4976e1d068a3631ee43a926b618c4"
_FORMAT_1_2_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Path]:
    """The bundle ``init`` writes for a format 1-2-0 profile, and the profile."""
    root = tmp_path_factory.mktemp("int-category")
    document: Dict[str, Any] = copy.deepcopy(_profile())
    document["_schemaURL"] = _FORMAT_1_2_0
    (root / "shop.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (root / "shop.profile.json").write_text(json.dumps(document), encoding="utf-8")
    written = run_init(
        str(root / "bundle"),
        contract=str(root / "shop.fluid.yaml"),
        profile=str(root / "shop.profile.json"),
    )
    assert written.exit_code == 0, written.stderr
    with_the_profile_named(without_replacements(root / "bundle"), "shop.profile.json")
    # The run recorded below is the one version 0.9.0 made, whose tolerances do not follow the row
    # count and whose columns are not allocated exactly: what init now writes for a small table is
    # taken off the bundle, as it was then.
    simulation = root / "bundle" / "simulation.flux.yml"
    lines = simulation.read_text(encoding="utf-8").splitlines(keepends=True)
    simulation.write_text(
        "".join(
            line for line in lines if line.strip() not in ("sampling: true", "allocation: exact")
        ),
        encoding="utf-8",
    )
    return root / "bundle", root / "shop.profile.json"


@pytest.fixture(scope="module")
def applied(made: Tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory) -> RunLayout:
    bundle, profile = made
    state = tmp_path_factory.mktemp("int-category-state") / ".flux"
    planned = plan(bundle, state_dir=state, profile=profile)
    assert planned.success, planned.stderr
    done = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), auto_plan=False)
    assert done.success, done.stderr
    return RunLayout.for_state_dir(state, str(done.artifacts["run_id"]))


def test_the_run_is_the_one_origin_main_makes(applied: RunLayout) -> None:
    plan_document = json.loads(applied.plan_path.read_text(encoding="utf-8"))
    run_digest = json.loads(applied.run_digest_path.read_text(encoding="utf-8"))
    assert applied.run_id == MAIN_RUN_ID
    assert plan_document["planDigest"] == MAIN_PLAN_DIGEST
    assert digest_of_document(run_digest, subject="runDigest").value == MAIN_RUN_DIGEST


def test_verify_compares_the_properties_origin_main_refused(
    made: Tuple[Path, Path], applied: RunLayout
) -> None:
    bundle, profile = made
    verified = run_verify(
        bundle,
        run_id=applied.run_id,
        state_dir=applied.state_dir,
        profile=profile,
        allow_declared_baseline_drift=True,
    )
    assert verified.success, verified.stderr
    assert "3 of 3 declared properties within the margin" in verified.stdout
    assert "FLX-5005" not in verified.stdout and "not read" not in verified.stdout


def test_reproducibility_reads_the_run_digest_and_no_verdict(
    made: Tuple[Path, Path], applied: RunLayout
) -> None:
    """A run recorded as the baseline verifies as reproducible, and the record holds no verdict."""
    bundle, profile = made
    state = applied.state_dir
    recorded = run_verify(
        bundle,
        run_id=applied.run_id,
        state_dir=state,
        profile=profile,
        update_golden=True,
        reason="the baseline of a run made before whole numbers read as categories",
        allow_declared_baseline_drift=True,
    )
    assert recorded.success, recorded.stderr
    snapshot = json.loads(applied.golden_path("sim-rating.shop").read_text(encoding="utf-8"))
    assert snapshot["baselineDigest"] == MAIN_RUN_DIGEST
    body = json.dumps(snapshot)
    for verdict in ("fidelity", "shape", "scorecard", "refus", "FLX-5005"):
        assert verdict not in body, verdict
    again = run_verify(
        bundle,
        run_id=applied.run_id,
        state_dir=state,
        profile=profile,
        allow_declared_baseline_drift=True,
    )
    assert again.success, again.stderr
    assert "golden: reproducible" in again.stdout
