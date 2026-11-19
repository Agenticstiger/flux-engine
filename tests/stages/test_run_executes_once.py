# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run`` executes the pipeline once, not twice.

A plan *is* an execution of the whole pipeline
(``stages/plan.py``'s own docstring says so, and argues for exactly this
reuse: a caller "can take that execution rather than paying to materialise
25 000 subjects a second time"). ``run`` planned and then let ``apply``
re-execute the identical bundle bytes a millisecond later, so the flagship
demo, CI, and every ``fluxsim apply <bundle>`` with no ``--run-id`` cost two
full pipeline runs. Measured on the public example: plan 42.0s + apply
42.1s = 83.0s for one ``fluxsim run``.

Counting ``pipeline.execute`` calls is the assertion, not wall clock: a
timing threshold is a flake, and the defect is a count.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

# `import flux_engine.stages.plan as plan_module` would bind the *function*
# `flux_engine.stages.plan`, which the package __init__ rebinds over the
# submodule of the same name; import_module gets the module itself.
apply_module = import_module("flux_engine.stages.apply")
pipeline_module = import_module("flux_engine.stages.pipeline")
plan_module = import_module("flux_engine.stages.plan")

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"


@pytest.fixture()
def execute_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    real = pipeline_module.execute

    def counting(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("bundle_directory"))
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "execute", counting)
    return calls


def test_run_executes_the_pipeline_exactly_once(tmp_path: Path, execute_calls: list) -> None:
    from flux_engine.stages.run import run

    result = run(SMALL_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    assert len(execute_calls) == 1, execute_calls


def test_auto_planning_apply_executes_the_pipeline_exactly_once(
    tmp_path: Path, execute_calls: list
) -> None:
    result = apply_module.apply(SMALL_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    assert len(execute_calls) == 1, execute_calls


def test_apply_against_a_recorded_plan_still_executes_once(
    tmp_path: Path, execute_calls: list
) -> None:
    """The plan-then-apply path is unchanged: one execution each."""
    state = tmp_path / ".flux"
    planned = plan_module.plan(SMALL_BUNDLE, state_dir=state)
    assert planned.success, planned.stderr
    assert len(execute_calls) == 1

    applied = apply_module.apply(SMALL_BUNDLE, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr
    assert len(execute_calls) == 2


def test_a_precomputed_result_for_a_different_bundle_is_still_refused(tmp_path: Path) -> None:
    """The inputDigest equality check still runs against a passed-in result.

    Reuse must not become a way to apply an execution of some *other*
    bundle under this bundle's plan.
    """
    import shutil

    state = tmp_path / ".flux"
    other = tmp_path / "other"
    shutil.copytree(SMALL_BUNDLE, other)
    # A byte in a document the bundle declares. This used to be a stray
    # `README-extra.md`, which moved the inputDigest under the rule ADR
    # 0006 replaced. A file that is neither a document nor a pack product
    # is now refused at the loader (FLX-3108) rather than hashed, so that
    # bundle never reached a plan at all.
    world = other / "world.flux.yml"
    world.write_bytes(world.read_bytes() + b"\n# moves the inputDigest\n")

    planned, _ = plan_module.plan_with_result(SMALL_BUNDLE, state_dir=state)
    assert planned.success, planned.stderr
    _, foreign = plan_module.plan_with_result(other, state_dir=tmp_path / ".other-flux")
    assert foreign is not None

    result = apply_module.apply(
        SMALL_BUNDLE,
        run_id=planned.artifacts["run_id"],
        state_dir=state,
        precomputed=foreign,
    )
    assert result.exit_code == 1
    assert result.error_code == "FLX-2101"
