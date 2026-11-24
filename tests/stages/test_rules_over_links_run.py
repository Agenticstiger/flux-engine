# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose invoices copy their account's and customer's columns through links.

``tests/fixtures/related-star`` is copied and its contract and emits are replaced by three
related tables, customer to account to invoice, with rules that read through the links
(EXPR-SPEC-2 section 6, ADR 0022). The tests run the bundle through the pipeline every command
uses, and then through ``plan``, ``apply`` and ``verify``:

* every invoice row holds every rule, and ``verify`` scores each as ``satisfied == total``;
* ``--jobs 1`` and ``--jobs 4`` give the same output digests, with the invoice table large
  enough to be cut into slices;
* kernel 0.1.0 refuses the same bundle with ``FLX-6210``, saying which kernel generates it;
* ``validate`` refuses a path through an expose the Simulation does not generate.
"""

from __future__ import annotations

import re
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from tests.bundles import repin_contract

from ._jobs import plan_apply_verify
from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
INVOICES = 2 * parallel.MIN_ROWS_PER_JOB + 301

_BINDING = (
    "    binding:\n      platform: aws\n      format: glue_table\n"
    "      location: {{ database: retail, table: {name}, region: eu-west-1 }}\n"
)

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
        - {{ name: cu_segment, type: STRING, required: false }}
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
        - {{ name: a_region, type: STRING, required: false }}
        - {{ name: a_score, type: INT, required: false }}
        - {{ name: a_band, type: STRING, required: false }}
        - {{ name: a_opened, type: DATE, required: false }}
      invariants:
        - id: band_of_score
          derive: a_band
          expr: "CASE WHEN a_score >= 50 THEN 'high' ELSE 'low' END"
    semantics:
      entities:
        - {{ name: account, type: primary, expr: a_id }}
      relationships:
        - name: customer
          to: customer
          fromColumns: [a_customer]
          toColumns: [cu_id]
          cardinality: many_to_one
  - exposeId: invoice
    kind: table
{_BINDING.format(name="invoice")}    contract:
      schema:
        - {{ name: i_id, type: STRING, required: true }}
        - {{ name: i_account, type: STRING, required: false }}
        - {{ name: i_region, type: STRING, required: false }}
        - {{ name: i_band, type: STRING, required: false }}
        - {{ name: i_segment, type: STRING, required: false }}
        - {{ name: i_duration, type: INT, required: false }}
        - {{ name: i_due, type: DATE, required: false }}
      invariants:
        - id: region_copy
          derive: i_region
          expr: "account.a_region"
        - id: band_copy
          derive: i_band
          expr: "account.a_band"
        - id: due_of_opened
          check: "i_duration = DAYS_BETWEEN(account.a_opened, i_due)"
    semantics:
      entities:
        - {{ name: invoice, type: primary, expr: i_id }}
      relationships:
        - name: account
          to: account
          fromColumns: [i_account]
          toColumns: [a_id]
          cardinality: many_to_one
"""

_FIDELITY = """\
        rows: {rows}
        fidelity:
          encoding: {{ fractionalBits: 16, rounding: floor }}
          records: {{ min: 1, max: 1000000 }}
          columns:
{columns}          properties:
            - property: mean
              column: {mean}
              tolerance: {{ type: absolute, value: "100" }}
          budget: {{ maxChecksPerState: 20, maxStateAge: P30D }}
          failureAction: withholdBundle
"""

_EMITS = """\
      customer:
{customer}      account:
{account}      invoice:
{invoice}"""


def _extension(rows: int, *, accounts_generated: bool = True) -> str:
    customer = _FIDELITY.format(
        rows=40,
        columns="            cu_segment: { support: { values: [retail, sme, corp] }, maxCardinality: 3 }\n"
        "            cu_age: { range: { min: '18', max: '90' } }\n",
        mean="cu_age",
    )
    account_columns = (
        "            a_region: { levels: { north: '0.5', south: '0.3', west: '0.2' } }\n"
        "            a_score: { range: { min: '0', max: '100' } }\n"
        "            a_opened: { range: { min: '2026-01-01', max: '2026-06-30' } }\n"
    )
    account = _FIDELITY.format(rows=120, columns=account_columns, mean="a_score")
    invoice_columns = (
        "            i_duration: { range: { min: '1', max: '30' } }\n"
        "            i_due: { range: { min: '2026-01-01', max: '2027-12-31' } }\n"
        "            i_region: { levels: { x: '0.5', y: '0.5' } }\n"
        "            i_band: { levels: { x: '0.5', y: '0.5' } }\n"
        "            i_segment: { levels: { x: '0.5', y: '0.5' } }\n"
    )
    invoice = _FIDELITY.format(rows=rows, columns=invoice_columns, mean="i_duration")
    invoice += (
        "        invariants:\n          due_of_opened:\n            generates: i_due\n"
        "        rules:\n"
        "          - id: segment_copy\n"
        "            language: flux-expr/2\n"
        "            derive: i_segment\n"
        '            expr: "account.customer.cu_segment"\n'
    )
    return _EMITS.format(customer=customer, account=account, invoice=invoice)


def _bundle(
    tmp_path: Path, *, rows: int = INVOICES, name: str = "bundle", contract: str = _CONTRACT_TEXT
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
    extension = "extensions:\n  com.agentics.flux/1:\n    emits:\n" + _extension(rows)
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("links-run"))


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
def test_every_invoice_row_holds_every_rule(result: pipeline.PipelineResult) -> None:
    tables = _tables(result)
    accounts = {row["a_id"]: row for row in tables["account"]}
    segments = {row["cu_id"]: row["cu_segment"] for row in tables["customer"]}
    assert len(tables["invoice"]) == INVOICES
    linked = 0
    for row in tables["invoice"]:
        if row["i_account"] is None:
            assert row["i_region"] is None and row["i_band"] is None
            assert row["i_segment"] is None
            continue
        parent = accounts[row["i_account"]]
        assert row["i_region"] == parent["a_region"]
        assert row["i_band"] == parent["a_band"] == ("high" if parent["a_score"] >= 50 else "low")
        assert row["i_segment"] == segments[parent["a_customer"]]
        assert row["i_due"] == parent["a_opened"] + timedelta(days=row["i_duration"])
        linked += 1
    assert linked > 0.85 * INVOICES


@pytest.mark.integration
def test_the_plan_names_the_paths_and_the_plans_each_reads(result: pipeline.PipelineResult) -> None:
    invoice = next(e for e in result.emits if e.resolved.expose_id == "invoice")
    assert invoice.plan is not None
    document = invoice.plan.as_document()
    assert sorted(document["reads"]) == ["account", "customer"]
    coverage = {row.column: (row.origin.value, row.decided_by) for row in invoice.coverage.columns}
    assert coverage["i_region"] == ("copied", "rule")
    assert coverage["i_segment"] == ("copied", "rule")
    assert result.emits[0].plan is not None and "reads" not in result.emits[0].plan.as_document()


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_bundle_and_says_which_kernel_generates_it(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-6210"
    assert "Kernel 0.2.0 generates" in str(caught.value)


@pytest.mark.integration
def test_validate_accepts_the_bundle_as_plan_reads_it(bundle: Path) -> None:
    assert validate(bundle).exit_code == 0


@pytest.mark.integration
def test_validate_refuses_a_path_through_an_expose_the_simulation_does_not_generate(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    simulation = bundle / _SIMULATION
    text = simulation.read_text("utf-8")
    # The account is no longer emitted from statistics: its fidelity block goes.
    text, count = re.subn(
        r"      account:\n        rows: 120\n.*?(?=      invoice:\n)", "", text, flags=re.S
    )
    assert count == 1
    simulation.write_text(text, "utf-8")
    result = validate(bundle)
    assert result.exit_code != 0
    assert "FLX-6210" in str(result.artifacts.get("problems")) + result.stderr


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_give_the_same_digests_for_any_number_of_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    digests, emits = plan_apply_verify(bundle, tmp_path)
    rules = [emit["invoice"] for emit in emits]
    assert digests[0] == digests[1]
    assert rules[0] == rules[1]
    by_rule = {entry["rule"]: entry for entry in rules[0]}
    assert {"region_copy", "band_copy", "due_of_opened", "segment_copy"} <= set(by_rule)
    for rule_id in ("region_copy", "band_copy", "segment_copy"):
        entry = by_rule[rule_id]
        assert entry["satisfied"] == entry["total"] and entry["total"] > 0, rule_id
