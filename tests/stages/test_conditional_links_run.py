# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A conditional link, end to end (ADR 0022 section 5): the billing star through ``plan``, ``apply`` and ``verify``.

An invoice holds its account only where ``i_amount > 250``. Every other invoice's foreign key is
what the bundle without the condition draws, and the copies the invoice makes through the link
are NULL where the link is cleared. ``--jobs`` does not change the bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify

from .test_shared_columns_run import _bundle

pytestmark = pytest.mark.integration

NAMESPACE = "  com.agentics.flux/1:\n"
LINKS = '    links:\n      - { emit: invoice, relationship: account, when: "i_amount > 250" }\n'
ROWS = 700


def _with_links(tmp_path: Path, name: str) -> Path:
    bundle = _bundle(tmp_path, rows=ROWS, name=name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + LINKS, 1), "utf-8")
    return bundle


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("conditional")
    return {"plain": _bundle(root, rows=ROWS, name="plain"), "linked": _with_links(root, "linked")}


@pytest.fixture(scope="module")
def runs(bundles: Dict[str, Path]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    return {
        name: _tables(pipeline.execute(bundle, kernel=KERNEL_V2))
        for name, bundle in bundles.items()
    }


def test_no_invoice_whose_condition_fails_holds_its_account_and_no_other_emit_moves(
    runs: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> None:
    # The declaration is part of the run's inputs, so this run is a new stream; the generator's
    # own tests hold one plan's foreign keys to the plan without the condition.
    linked = runs["linked"]["invoice"]
    held = cleared = 0
    for row in linked:
        if row["i_amount"] > 250:
            held += row["i_account"] is not None
        else:
            assert row["i_account"] is None
            assert row["region"] is None and row["a_score"] is None
            cleared += 1
    assert held > 0 and cleared > 0


def test_a_copy_through_the_link_is_the_parents_value_where_the_link_holds(
    runs: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> None:
    accounts = {row["a_id"]: row for row in runs["linked"]["account"]}
    held = 0
    for row in runs["linked"]["invoice"]:
        if row["i_account"] is not None:
            assert row["region"] == accounts[row["i_account"]]["region"]
            assert row["a_score"] == accounts[row["i_account"]]["a_score"]
            held += 1
    assert held > 0


def test_kernel_0_1_0_refuses_the_links_key_and_names_the_kernel(bundles: Dict[str, Path]) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundles["linked"], kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_validate_reads_the_declaration(bundles: Dict[str, Path]) -> None:
    assert validate(bundles["linked"]).success


@pytest.mark.slow
def test_plan_apply_and_verify_give_the_same_bytes_for_any_jobs(
    bundles: Dict[str, Path], tmp_path: Path
) -> None:
    digests = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(bundles["linked"], state_dir=state, jobs=jobs)
        assert planned.success, planned.stderr
        applied = apply(
            bundles["linked"], state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs
        )
        assert applied.success, applied.stderr
        layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
        verified = run_verify(
            bundles["linked"],
            run_id=layout.run_id,
            state_dir=state,
            allow_declared_baseline_drift=True,
            jobs=jobs,
        )
        assert verified.success, verified.stderr
        digests.append(
            (
                planned.artifacts.get("plan_digest"),
                {
                    k: v
                    for k, v in applied.artifacts.items()
                    if "digest" in k.lower() and k != "run_id"
                },
            )
        )
    assert digests[0] == digests[1]
