# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose invoices rank themselves among their account's invoices (EXPR-SPEC-2 section 5).

The bundle of ``test_rules_over_links_run`` gains FLUX-side rules on the invoice: ``ROW_NUMBER``,
``RANK`` and ``DENSE_RANK``, and ``FIRST_VALUE``, ``LAST_VALUE``, ``LAG`` and ``LEAD``, over the
invoices of one account (ADR 0022 section 7).
The tests run it through the pipeline and through ``plan``, ``apply`` and ``verify``:

* every row holds each rank, as DuckDB computes it over the written rows, and ``verify`` scores
  each window rule as ``satisfied == total``;
* ``--jobs 1`` and ``--jobs 4`` and a run cut into shards give the same bytes, the invoice table
  large enough to be cut into slices;
* kernel 0.1.0 refuses the bundle with ``FLX-6210``, saying which kernel generates it.
"""

from __future__ import annotations

import shutil
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
from tests.bundles import repin_contract

from . import test_rules_over_links_run as base

duckdb = pytest.importorskip("duckdb")

ORDER = "ORDER BY i_duration DESC, i_id"
VALUES = {
    "fv": ("i_first", f"FIRST_VALUE(i_duration) OVER (PARTITION BY account {ORDER})"),
    "lv": ("i_last", f"LAST_VALUE(i_duration) OVER (PARTITION BY account {ORDER})"),
    "lg": ("i_lag", f"LAG(i_duration, 1, 0) OVER (PARTITION BY account {ORDER})"),
    "ld": ("i_lead", f"LEAD(i_duration, 2) OVER (PARTITION BY account {ORDER})"),
}
RANKS = {
    "rn": ("i_rn", "ROW_NUMBER() OVER (PARTITION BY account ORDER BY i_duration DESC, i_id)"),
    "rk": ("i_rk", "RANK() OVER (PARTITION BY account ORDER BY i_duration DESC)"),
    "dr": ("i_dr", "DENSE_RANK() OVER (PARTITION BY account ORDER BY i_duration DESC)"),
}

RULES = {**RANKS, **VALUES}

_CONTRACT = base._CONTRACT_TEXT.replace(
    "        - { name: i_due, type: DATE, required: false }\n",
    "        - { name: i_due, type: DATE, required: false }\n"
    + "".join(
        f"        - {{ name: {column}, type: INT, required: false }}\n"
        for column, _ in RULES.values()
    ),
)
assert "i_rn" in _CONTRACT


def _extension(rows: int) -> str:
    text = base._extension(rows)
    rules = "".join(
        f"          - id: {rule_id}\n            language: flux-expr/2\n"
        f'            derive: {column}\n            expr: "{expr}"\n'
        for rule_id, (column, expr) in RULES.items()
    )
    return text + rules


def _bundle(tmp_path: Path, name: str = "bundle", rows: int = base.INVOICES) -> Path:
    target = tmp_path / name
    shutil.copytree(base.FIXTURE, target)
    (target / base._CONTRACT).write_text(_CONTRACT, "utf-8")
    simulation = target / base._SIMULATION
    head, _, _ = simulation.read_text("utf-8").partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("customer", "account", "invoice")
    )
    extension = "extensions:\n  com.agentics.flux/1:\n    emits:\n" + _extension(rows)
    simulation.write_text(head + emits + extension, "utf-8")
    repin_contract(target / base._CONTRACT, simulation, "retail.store_star")
    return target


def _value_query(connection: Any) -> List[Any]:
    """DuckDB's value functions over ``t``, the whole-partition frame written out."""
    over = "PARTITION BY i_account ORDER BY i_duration DESC NULLS LAST, i_id ASC NULLS LAST"
    frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
    found: List[Any] = connection.execute(
        "SELECT i_id,"
        f" FIRST_VALUE(i_duration) OVER ({over} {frame}),"
        f" LAST_VALUE(i_duration) OVER ({over} {frame}),"
        f" LAG(i_duration, 1, 0) OVER ({over}), LEAD(i_duration, 2) OVER ({over}) FROM t"
    ).fetchall()
    connection.close()
    return found


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("windows-run"))


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _invoices(outcome: pipeline.PipelineResult) -> List[Dict[str, Any]]:
    emit = next(e for e in outcome.emits if e.resolved.expose_id == "invoice")
    return [dict(zip(list(emit.expose.column_names), row, strict=True)) for row in emit.rows]


@pytest.mark.integration
def test_every_invoice_holds_the_rank_duckdb_computes_over_the_written_rows(
    result: pipeline.PipelineResult,
) -> None:
    rows = _invoices(result)
    assert len(rows) == base.INVOICES
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (i_id VARCHAR, i_account VARCHAR, i_duration INTEGER)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [(r["i_id"], r["i_account"], r["i_duration"]) for r in rows],
    )
    over = "PARTITION BY i_account ORDER BY i_duration DESC NULLS LAST"
    found = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT i_id,"
            f" ROW_NUMBER() OVER ({over}, i_id ASC NULLS LAST),"
            f" RANK() OVER ({over}), DENSE_RANK() OVER ({over}) FROM t"
        ).fetchall()
    }
    assert len(found) == len(rows)
    for row in rows:
        assert (row["i_rn"], row["i_rk"], row["i_dr"]) == found[row["i_id"]], row["i_id"]
    values = {row[0]: row[1:] for row in _value_query(connection)}
    for row in rows:
        assert (row["i_first"], row["i_last"], row["i_lag"], row["i_lead"]) == values[row["i_id"]]
    assert any(row["i_lag"] == 0 for row in rows) and any(row["i_lead"] is None for row in rows)
    assert max(row["i_rn"] for row in rows) > 3
    assert any(row["i_rn"] > 1 for row in rows)


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_bundle_and_says_which_kernel_generates_it(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-6210"
    assert "Kernel 0.2.0 generates" in str(caught.value)


@pytest.mark.integration
def test_validate_accepts_the_bundle(bundle: Path) -> None:
    assert validate(bundle).exit_code == 0


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_hold_for_any_jobs_and_for_shards(
    bundle: Path, tmp_path: Path
) -> None:
    digests = []
    rules = []
    runs = []
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
        rules.append(verified.artifacts["rules"]["emits"]["invoice"])
        runs.append((layout, planned.artifacts["run_id"], planned.artifacts["plan_digest"]))
    assert digests[0] == digests[1]
    assert rules[0] == rules[1]
    by_rule = {entry["rule"]: entry for entry in rules[0]}
    for rule_id in RULES:
        entry = by_rule[rule_id]
        assert entry["satisfied"] == entry["total"] and entry["total"] > 0, rule_id
    # A run cut into three shares: the shares joined in order are the whole run's bytes.
    layout, run_id, plan_digest = runs[0]
    shard_state = tmp_path / "shards"
    shard_plan = plan(bundle, state_dir=shard_state, stage=False)
    assert shard_plan.artifacts["plan_digest"] == plan_digest
    shards = RunLayout.for_state_dir(shard_state, str(shard_plan.artifacts["run_id"]))
    for index in (1, 2, 3):
        part = apply(
            bundle,
            run_id=str(shard_plan.artifacts["run_id"]),
            state_dir=shard_state,
            shard=(index, 3),
        )
        assert part.success, part.stderr
    joined = b"".join(shards.shard_emit_path("invoice", i, 3).read_bytes() for i in (1, 2, 3))
    assert joined == layout.emit_path("invoice").read_bytes()
