# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Whole runs under kernel 0.2.0 (ADR 0010, step 3).

The conformance module holds each primitive to the vector files. This one holds what a run
promises under kernel 0.2.0, as it does under kernel 0.1.0: a generator plan that names its
generator, a plan and root that agree on their kernel, and a run digest that names it. The
same bytes whatever ``--jobs`` is: tests/stages/test_related_tables.py, whose run is large
enough for the pool to start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.rows import GENERATOR_VERSION, GENERATOR_VERSION_2, generate_rows
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan

REPO_ROOT = Path(__file__).resolve().parents[2]
RELATED_STAR = REPO_ROOT / "tests" / "fixtures" / "related-star"

pytestmark = pytest.mark.integration


def test_the_generator_plans_name_their_generator() -> None:
    v1 = pipeline.execute(RELATED_STAR)
    v2 = pipeline.execute(RELATED_STAR, kernel=pipeline._kernels.KERNELS["0.2.0"])
    for result, version in ((v1, GENERATOR_VERSION), (v2, GENERATOR_VERSION_2)):
        plans = [outcome.plan for outcome in result.emits if outcome.plan is not None]
        assert plans
        assert {plan_of.generator_version for plan_of in plans} == {version}
        assert all(plan_of.as_document()["generatorVersion"] == version for plan_of in plans)
    assert v1.root.seed_tree_version == "flux-seed/1"
    assert v2.root.seed_tree_version == kernel2.SEED_TREE_VERSION
    assert v1.input_digest.ref.value != v2.input_digest.ref.value


def test_a_plan_and_a_root_of_two_kernels_are_refused_together() -> None:
    v2 = pipeline.execute(RELATED_STAR, kernel=pipeline._kernels.KERNELS["0.2.0"])
    plan_of = next(outcome.plan for outcome in v2.emits if outcome.plan is not None)
    v1_root = derive_root(v2.root.seam_bytes, v2.root.effective_seed)
    with pytest.raises(FluxError) as refused:
        generate_rows(plan_of, root=v1_root)
    assert refused.value.code.code == "FLX-2202"
    assert "flux-gen/2" in str(refused.value) and "flux-seed/1" in str(refused.value)


def test_the_run_digest_names_kernel_0_2_0(tmp_path: Path) -> None:
    planned = plan(RELATED_STAR, state_dir=tmp_path, kernel="0.2.0")
    run_id = str(planned.artifacts["run_id"])
    applied = apply(RELATED_STAR, run_id=run_id, state_dir=tmp_path, auto_plan=False)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(tmp_path, run_id)
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    receipt = json.loads(layout.receipt_path.read_text(encoding="utf-8"))
    assert document["kernelVersion"] == receipt["kernelVersion"] == "0.2.0"
    assert receipt["seedTreeVersion"] == "flux-seed/2"
