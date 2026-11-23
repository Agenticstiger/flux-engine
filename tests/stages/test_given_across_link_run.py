# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose invoices are drawn given their account's region (ADR 0023 section 3).

``tests/fixtures/related-star`` is copied and its contract and emits are replaced by an ``account``
table and an ``invoice`` table whose ``i_channel`` is drawn ``given: account.a_region``, each region
allowing one channel only, so every row either follows its account or the run is wrong. The tests run
the pipeline every command uses, and then ``plan``, ``apply`` and ``verify``:

* every invoice row's channel is the one its account's region allows;
* ``--jobs 1`` and ``--jobs 4`` give the same output digests, with the invoice table large enough to
  be cut into slices;
* kernel 0.1.0 refuses the bundle with ``FLX-3402``, naming the kernel, and ``validate`` takes it;
* a link the contract does not declare, and a relationship that may reach no row, are refused
  before any row is drawn.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
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
  - exposeId: account
    kind: table
{_BINDING.format(name="account")}    contract:
      schema:
        - {{ name: a_id, type: STRING, required: true }}
        - {{ name: a_region, type: STRING, required: false }}
        - {{ name: a_score, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: account, type: primary, expr: a_id }}
  - exposeId: invoice
    kind: table
{_BINDING.format(name="invoice")}    contract:
      schema:
        - {{ name: i_id, type: STRING, required: true }}
        - {{ name: i_account, type: STRING, required: false }}
        - {{ name: i_channel, type: STRING, required: false }}
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

_ACCOUNT_COLUMNS = (
    "            a_region: { levels: { north: '0.5', south: '0.3', west: '0.2' } }\n"
    "            a_score: { range: { min: '0', max: '100' } }\n"
)
_CHANNEL = (
    "            i_channel:\n"
    "              given: account.a_region\n"
    "              levelWeightsGiven:\n"
    "                north: { web: '1', shop: '0' }\n"
    "                south: { web: '0', shop: '1' }\n"
    "                west: { web: '1', shop: '0' }\n"
)


def _bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    rows: int = INVOICES,
    channel: str = _CHANNEL,
    account_null: str = "",
) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_CONTRACT_TEXT, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("account", "invoice")
    )
    invoice_columns = (
        channel + "            i_amount: { range: { min: '1', max: '30' } }\n" + account_null
    )
    extension = (
        "extensions:\n  com.agentics.flux/1:\n    emits:\n"
        "      account:\n"
        + _FIDELITY.format(rows=60, columns=_ACCOUNT_COLUMNS, mean="a_score")
        + "      invoice:\n"
        + _FIDELITY.format(rows=rows, columns=invoice_columns, mean="i_amount")
    )
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("given-link-run"))


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


@pytest.mark.integration
def test_every_invoice_follows_the_region_of_its_account(bundle: Path) -> None:
    tables = _tables(pipeline.execute(bundle, kernel=KERNEL_V2))
    accounts = {row["a_id"]: row["a_region"] for row in tables["account"]}
    allowed = {"north": "web", "south": "shop", "west": "web"}
    assert len(tables["invoice"]) == INVOICES
    assert {allowed[region] for region in accounts.values()} == {"web", "shop"}
    for row in tables["invoice"]:
        assert row["i_channel"] == allowed[accounts[row["i_account"]]], row["i_id"]


@pytest.mark.integration
def test_the_plan_reads_the_account_and_the_coverage_calls_the_column_declared(
    bundle: Path,
) -> None:
    result = pipeline.execute(bundle, kernel=KERNEL_V2)
    invoice = next(e for e in result.emits if e.resolved.expose_id == "invoice")
    assert invoice.plan is not None
    assert sorted(invoice.plan.as_document()["reads"]) == ["account"]
    coverage = {row.column: (row.origin.value, row.decided_by) for row in invoice.coverage.columns}
    assert coverage["i_channel"] == ("declared", "fidelity")


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_bundle_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3402"
    assert "kernel 0.2.0" in str(caught.value) and "kernel 0.1.0" in str(caught.value)


@pytest.mark.integration
def test_validate_takes_the_bundle(bundle: Path) -> None:
    assert validate(bundle).exit_code == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "kwargs", "needle"),
    [
        (
            "may-reach-no-row",
            {"account_null": "            i_account: { nullFraction: '0.2' }\n"},
            "may reach no row",
        ),
        (
            "no-such-link",
            {"channel": _CHANNEL.replace("account.a_region", "customer.a_region")},
            "no relationship",
        ),
        (
            "a-table-that-misses-a-level",
            {"channel": _CHANNEL.replace("                west: { web: '1', shop: '0' }\n", "")},
            "levelWeightsGiven declares levels",
        ),
    ],
)
def test_what_cannot_be_drawn_is_refused_before_any_row_is_drawn(
    tmp_path: Path, name: str, kwargs: Dict[str, Any], needle: str
) -> None:
    refused = _bundle(tmp_path, name=name, **kwargs)
    state = tmp_path / "state"
    result = plan(refused, state_dir=state)
    assert result.exit_code != 0
    assert result.error_code == "FLX-3402", result.stderr
    assert needle in result.stderr
    assert not list(state.glob("runs/*/plan.json"))


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_give_the_same_digests_for_any_number_of_jobs(
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
                {k: v for k, v in applied.artifacts.items() if "digest" in k.lower()},
            )
        )
    assert digests[0] == digests[1]
