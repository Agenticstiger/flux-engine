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


def test_plan_then_apply_executes_the_pipeline_exactly_once(
    tmp_path: Path, execute_calls: list
) -> None:
    """A plan stages its execution, and apply commits it without generating."""
    state = tmp_path / ".flux"
    planned = plan_module.plan(SMALL_BUNDLE, state_dir=state)
    assert planned.success, planned.stderr
    assert len(execute_calls) == 1
    assert planned.artifacts["staged"] is True

    applied = apply_module.apply(SMALL_BUNDLE, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr
    assert applied.artifacts["committed_from_stage"] is True
    assert len(execute_calls) == 1, execute_calls


def test_a_plan_without_a_stage_is_executed_again_by_apply(
    tmp_path: Path, execute_calls: list
) -> None:
    """``--no-stage`` keeps the plan that writes only ``plan.json``: apply generates."""
    state = tmp_path / ".flux"
    planned = plan_module.plan(SMALL_BUNDLE, state_dir=state, stage=False)
    assert planned.success, planned.stderr
    assert planned.artifacts["staged"] is False
    applied = apply_module.apply(SMALL_BUNDLE, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr
    assert len(execute_calls) == 2
