# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parents picked by a weight column, end to end (ADR 0023 section 4): the billing star through ``plan``, ``apply`` and ``verify``.

An invoice picks its account by the account's ``a_score``: an account that scores zero or NULL is
never picked, and the others are picked in proportion to their score. ``--jobs`` does not change
the bytes, and kernel 0.1.0 refuses the entry.
"""

from __future__ import annotations

from collections import Counter
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
LINKS = "    links:\n      - { emit: invoice, relationship: account, weight: a_score }\n"
ROWS = 1500


def _weighted(tmp_path: Path, name: str, links: str = LINKS) -> Path:
    bundle = _bundle(tmp_path, rows=ROWS, name=name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + links, 1), "utf-8")
    return bundle


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _weighted(tmp_path_factory.mktemp("weighted"), "weighted")


@pytest.fixture(scope="module")
def tables(bundle: Path) -> Dict[str, List[Dict[str, Any]]]:
    return _tables(pipeline.execute(bundle, kernel=KERNEL_V2))


def test_an_account_that_scores_nothing_is_never_picked_and_the_others_follow_their_score(
    tables: Dict[str, List[Dict[str, Any]]],
) -> None:
    score = {row["a_id"]: row["a_score"] or 0 for row in tables["account"]}
    picked = Counter(row["i_account"] for row in tables["invoice"] if row["i_account"] is not None)
    assert sum(picked.values()) > 0.85 * ROWS
    assert all(picked[key] == 0 for key, value in score.items() if value == 0)
    total = sum(score.values())
    drawn = sum(picked.values())
    chi = sum(
        (picked[key] - drawn * value / total) ** 2 / (drawn * value / total)
        for key, value in score.items()
        if value
    )
    # Past the 99.99th percentile of the distribution for the accounts' degrees of freedom.
    assert chi < 2 * len(score) + 60, chi


def test_a_copy_through_the_weighted_link_is_still_the_parents_value(
    tables: Dict[str, List[Dict[str, Any]]],
) -> None:
    accounts = {row["a_id"]: row for row in tables["account"]}
    held = 0
    for row in tables["invoice"]:
        if row["i_account"] is not None:
            assert row["a_score"] == accounts[row["i_account"]]["a_score"]
            held += 1
    assert held > 0


def test_a_weight_that_names_no_number_column_is_refused_at_plan_time(tmp_path: Path) -> None:
    bad = _weighted(
        tmp_path,
        "bad",
        "    links:\n      - { emit: invoice, relationship: account, weight: region }\n",
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bad, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "number column" in str(caught.value)


def test_kernel_0_1_0_refuses_the_entry_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_validate_reads_the_declaration(bundle: Path) -> None:
    assert validate(bundle).success


@pytest.mark.slow
def test_plan_apply_and_verify_give_the_same_bytes_for_any_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    digests = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(bundle, state_dir=state, jobs=jobs)
        assert planned.success, planned.stderr
        applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
        verified = run_verify(
            bundle,
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
