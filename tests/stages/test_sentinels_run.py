# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy from a missing link is NULL or its declared sentinel, end to end (ADR 0022 section 6).

An invoice holds its account only where ``i_amount > 250``, and its copies of ``region`` and
``a_score`` hold ``none`` and ``-1`` where the link reaches no row, whether the foreign key drew
NULL or the condition cleared it. ``fluxsim verify`` covers a copy with the rows whose link
reaches a row, so a sentinel row is not a violation of it.
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
from flux_engine.stages.verify import verify as run_verify

from .test_shared_columns_run import _CONTRACT_TEXT, _bundle

pytestmark = pytest.mark.integration

NAMESPACE = "  com.agentics.flux/1:\n"
LINKS = (
    "    links:\n"
    "      - emit: invoice\n"
    "        relationship: account\n"
    '        when: "i_amount > 250"\n'
    "        sentinels: { region: none, a_score: '-1' }\n"
)
ROWS = 700


def _bundle_with(tmp_path: Path, links: str, name: str) -> Path:
    bundle = _bundle(tmp_path, rows=ROWS, name=name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + links, 1), "utf-8")
    return bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle_with(tmp_path_factory.mktemp("sentinels"), LINKS, "sentinels")


@pytest.fixture(scope="module")
def tables(bundle: Path) -> Dict[str, List[Dict[str, Any]]]:
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


def test_every_row_with_no_link_holds_the_sentinel_and_the_others_their_parents_value(
    tables: Dict[str, List[Dict[str, Any]]],
) -> None:
    accounts = {row["a_id"]: row for row in tables["account"]}
    from_key = from_condition = linked = 0
    for row in tables["invoice"]:
        if row["i_account"] is None:
            assert (row["region"], row["a_score"]) == ("none", -1)
            if row["i_amount"] > 250:
                from_key += 1
            else:
                from_condition += 1
        else:
            parent = accounts[row["i_account"]]
            assert (row["region"], row["a_score"]) == (parent["region"], parent["a_score"])
            linked += 1
    assert from_key > 0 and from_condition > 0 and linked > 0


def test_kernel_0_1_0_refuses_a_sentinel_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


@pytest.mark.parametrize(
    ("sentinels", "problem"),
    [
        ("{ i_amount: '1' }", "does not copy"),
        ("{ a_score: abc }", "not a number"),
    ],
)
def test_a_sentinel_the_plan_cannot_hold_is_refused_before_any_row(
    tmp_path: Path, sentinels: str, problem: str
) -> None:
    links = (
        f"    links:\n      - {{ emit: invoice, relationship: account, sentinels: {sentinels} }}\n"
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle_with(tmp_path, links, "bad"), kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3105" and problem in str(caught.value)


def test_a_sentinel_on_a_copy_whose_derive_has_a_when_is_refused_at_plan(tmp_path: Path) -> None:
    contract = _CONTRACT_TEXT.replace(
        "sharedColumns: [region, a_score, i_account]", "sharedColumns: [region, i_account]"
    )
    anchor = "        - { name: i_amount, type: INT, required: false }\n"
    assert contract.count(anchor) == 1
    contract = contract.replace(
        anchor,
        anchor
        + "      invariants:\n        - id: score_copy\n          derive: a_score\n"
        + '          when: "i_amount > 100"\n          expr: "account.a_score"\n',
    )
    bundle = _bundle(tmp_path, rows=ROWS, name="when", contract=contract)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + LINKS, 1), "utf-8")
    planned = plan(bundle, state_dir=tmp_path / "state")
    assert not planned.success
    message = planned.stderr
    assert "FLX-3105" in message
    assert "a_score" in message and "i_amount > 100" in message and "when" in message


@pytest.mark.slow
def test_plan_apply_and_verify_cover_a_copy_with_the_rows_that_reach_a_parent(
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
        lines = {e["rule"]: e for e in verified.artifacts["rules"]["emits"]["invoice"]}
        for rule_id in ("account.region", "account.a_score"):
            line = lines[rule_id]
            assert line["satisfied"] == line["total"] > 0
            assert line["total"] < 700  # the rows with no link are not the copy's to decide
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


def test_a_string_sentinel_written_decomposed_plans_applies_and_verifies(tmp_path: Path) -> None:
    decomposed = "café"
    links = (
        "    links:\n      - emit: invoice\n        relationship: account\n"
        f'        when: "i_amount > 250"\n        sentinels: {{ region: "{decomposed}" }}\n'
    )
    bundle = _bundle_with(tmp_path, links, "nfd")
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state)
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    verified = run_verify(
        bundle,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        allow_declared_baseline_drift=True,
    )
    assert verified.success, verified.stderr
    line = {e["rule"]: e for e in verified.artifacts["rules"]["emits"]["invoice"]}["account.region"]
    assert line["satisfied"] == line["total"] > 0
