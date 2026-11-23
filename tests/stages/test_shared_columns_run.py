# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose accounts and invoices copy their parent's columns by ``sharedColumns``.

``tests/fixtures/related-star`` is copied and its contract and emits are replaced by three related
tables, customer to account to invoice, whose relationships list ``sharedColumns`` (EXPR-SPEC-2
section 9, ADR 0022 section 4). The tests run the bundle through the pipeline every command uses, and
then through ``plan``, ``apply`` and ``verify``:

* every row that reaches a parent holds the parent's value, and a NULL key gives NULL;
* ``plan`` lists nothing as declared and not generated, and ``verify`` scores each copy as a rule;
* kernel 0.1.0 plans the same bundle with its declaration listed and no copy;
* ``--jobs 1`` and ``--jobs 4`` give the same output digests.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify
from tests.bundles import repin_contract

from .conftest import REPO_ROOT
from .test_rules_over_links_run import _BINDING, _EMITS, _FIDELITY

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
INVOICES = 2 * parallel.MIN_ROWS_PER_JOB + 301

_CONTRACT_TEXT = f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: retail.store_star
name: Billing
metadata:
  owner:
    team: growth-lab
exposes:
  - exposeId: customer
    kind: table
{_BINDING.format(name="customer")}    contract:
      schema:
        - {{ name: cu_id, type: STRING, required: true }}
        - {{ name: region, type: STRING, required: false }}
        - {{ name: cu_age, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: customer, type: primary, expr: cu_id }}
  - exposeId: account
    kind: table
{_BINDING.format(name="account")}    contract:
      schema:
        - {{ name: a_id, type: STRING, required: true }}
        - {{ name: a_customer, type: STRING, required: false }}
        - {{ name: region, type: STRING, required: false }}
        - {{ name: a_score, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: account, type: primary, expr: a_id }}
      relationships:
        - name: customer
          to: customer
          fromColumns: [a_customer]
          toColumns: [cu_id]
          cardinality: many_to_one
          sharedColumns: [region]
  - exposeId: invoice
    kind: table
{_BINDING.format(name="invoice")}    contract:
      schema:
        - {{ name: i_id, type: STRING, required: true }}
        - {{ name: i_account, type: STRING, required: false }}
        - {{ name: region, type: STRING, required: false }}
        - {{ name: a_score, type: INT, required: false }}
        - {{ name: i_amount, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: invoice, type: primary, expr: i_id }}
      relationships:
        - name: account
          to: account
          fromColumns: [i_account]
          toColumns: [a_id]
          cardinality: many_to_one
          sharedColumns: [region, a_score, i_account]
"""


def _extension(rows: int) -> str:
    customer = _FIDELITY.format(
        rows=40,
        columns="            region: { levels: { north: '0.5', south: '0.3', west: '0.2' } }\n"
        "            cu_age: { range: { min: '18', max: '90' } }\n",
        mean="cu_age",
    )
    account = _FIDELITY.format(
        rows=120,
        columns="            region: { levels: { x: '0.5', y: '0.5' } }\n"
        "            a_customer: { nullFraction: '0.1' }\n"
        "            a_score: { range: { min: '0', max: '100' } }\n",
        mean="a_score",
    )
    invoice = _FIDELITY.format(
        rows=rows,
        columns="            region: { levels: { x: '0.5', y: '0.5' } }\n"
        "            i_account: { nullFraction: '0.1' }\n"
        "            a_score: { range: { min: '0', max: '100' } }\n"
        "            i_amount: { range: { min: '1', max: '500' } }\n",
        mean="i_amount",
    )
    return _EMITS.format(customer=customer, account=account, invoice=invoice)


def _bundle(
    tmp_path: Path,
    *,
    rows: int = INVOICES,
    name: str = "bundle",
    contract: str = _CONTRACT_TEXT,
    copies: bool = True,
) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(contract, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("customer", "account", "invoice")
    )
    asked = "    copies: true\n" if copies else ""
    extension = "extensions:\n  com.agentics.flux/1:\n" + asked + "    emits:\n" + _extension(rows)
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("shared-run"))


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


@pytest.mark.integration
def test_every_row_holds_its_parents_value_and_a_null_key_gives_null(
    result: pipeline.PipelineResult,
) -> None:
    tables = _tables(result)
    customers = {row["cu_id"]: row for row in tables["customer"]}
    accounts = {row["a_id"]: row for row in tables["account"]}
    linked = nulls = 0
    for row in tables["account"]:
        if row["a_customer"] is None:
            assert row["region"] is None
        else:
            assert row["region"] == customers[row["a_customer"]]["region"] is not None
    for row in tables["invoice"]:
        if row["i_account"] is None:
            assert row["region"] is None and row["a_score"] is None
            nulls += 1
        else:
            parent = accounts[row["i_account"]]
            assert row["region"] == parent["region"]
            assert row["a_score"] == parent["a_score"]
            linked += 1
    assert linked > 0.85 * INVOICES and nulls > 0
    # An invoice's region is its customer's, two links away.
    by_account = {row["a_id"]: customers.get(row["a_customer"]) for row in tables["account"]}
    assert all(
        by_account[row["i_account"]] is None
        or row["region"] == by_account[row["i_account"]]["region"]
        for row in tables["invoice"]
        if row["i_account"] is not None
    )


@pytest.mark.integration
def test_plan_lists_the_key_column_only_and_names_each_copy_in_its_coverage(
    result: pipeline.PipelineResult,
) -> None:
    invoice = next(e for e in result.emits if e.resolved.expose_id == "invoice")
    (row,) = invoice.not_generated
    assert (row.kind, row.names) == ("shared-columns", ("i_account",))
    assert row.reason
    coverage = {
        c.column: (c.origin.value, c.decided_by, c.source) for c in invoice.coverage.columns
    }
    assert coverage["region"] == (
        "copied",
        "rule",
        "semantics.relationships.account.sharedColumns.region",
    )
    assert coverage["a_score"][:2] == ("copied", "rule")
    assert not next(e for e in result.emits if e.resolved.expose_id == "account").not_generated


@pytest.mark.integration
def test_kernel_0_1_0_lists_every_declaration_and_copies_nothing(tmp_path: Path) -> None:
    old = pipeline.execute(_bundle(tmp_path, copies=False), kernel=KERNEL_V1)
    listed = {e.resolved.expose_id: e.not_generated for e in old.emits}
    assert [row.names for row in listed["invoice"]] == [("region", "a_score", "i_account")]
    assert [row.names for row in listed["account"]] == [("region",)]
    assert all(row.reason == "" for rows in listed.values() for row in rows)
    for emit in old.emits:
        assert emit.plan is not None and emit.plan.rules is None


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_score_each_copy_and_give_the_same_digests_for_any_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    digests = []
    rules = []
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
        rules.append(verified.artifacts["rules"]["emits"])
    assert digests[0] == digests[1]
    assert rules[0] == rules[1]
    by_rule = {entry["rule"]: entry for entry in rules[0]["invoice"]}
    assert {"account.region", "account.a_score"} <= set(by_rule)
    for rule_id in ("account.region", "account.a_score"):
        entry = by_rule[rule_id]
        assert entry["origin"] == "shared" and entry["enforced"] is True
        assert entry["satisfied"] == entry["total"] and entry["total"] > 0, rule_id
    assert {entry["rule"] for entry in rules[0]["account"]} == {"customer.region"}


@pytest.mark.integration
def test_a_copied_column_that_declares_a_null_fraction_is_refused_by_every_stage(
    tmp_path: Path,
) -> None:
    """The column's own NULLs come first (EXPR-SPEC-2 section 6) and would fail the copy in verify.

    With ``nullFraction: '0.2'`` on the invoice's ``region``, 309 of 367 rows whose key is not NULL
    held the account's value and the run failed verify. The rule allows no violating row, so plan,
    apply and validate refuse it, naming the emit, the column and the rule, before any row exists.
    """
    bundle = _bundle(tmp_path)
    simulation = bundle / _SIMULATION
    text = simulation.read_text("utf-8")
    declared = "            region: { levels: { x: '0.5', y: '0.5' } }\n            i_account:"
    assert text.count(declared) == 1
    simulation.write_text(
        text.replace(
            declared,
            "            region: { levels: { x: '0.5', y: '0.5' }, nullFraction: '0.2' }\n"
            "            i_account:",
        ),
        "utf-8",
    )
    state = tmp_path / ".flux"
    planned = plan(bundle, state_dir=state)
    assert not planned.success and planned.error_code == "FLX-6204", planned.stderr
    for named in ("emit 'invoice'", "column 'region'", "rule 'account.region'", "NULLs come first"):
        assert named in planned.stderr, planned.stderr
    assert not state.exists(), "a refused plan writes no run directory"
    applied = apply(bundle, state_dir=state)
    assert not applied.success and applied.error_code == "FLX-6204", applied.stderr
    refused = validate(bundle)
    assert refused.exit_code == 1 and refused.error_code == "FLX-6204", refused.stderr
    with pytest.raises(Exception) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6204"  # type: ignore[attr-defined]
    # The same bundle without the declaration is accepted, as it was.
    clean = _bundle(tmp_path, name="clean")
    assert plan(clean, state_dir=tmp_path / ".flux-clean").success
