# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose customers draw a hidden column (ADR 0024 section 2).

``tests/fixtures/related-star`` is copied and its contract and emits are replaced by two related
tables, customer to account. The customer declares a hidden DATE column ``signup`` with its own
statistics, and two FLUX-side rules read it: one copies it into a contract column of the customer,
the other copies it into the account through the link. The tests run the bundle through the
pipeline every command uses, and then through ``plan``, ``apply`` and ``verify``:

* the written stream holds no hidden column, and the contract columns equal what it drew;
* the column path of kernel 0.2.0 holds the rows the per-row path holds, with the column present;
* ``--jobs 1`` and ``--jobs 4`` give the same digests;
* kernel 0.1.0 refuses the key (``FLX-3105``), and a bundle without it is the bundle it was.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import _fidelity_summary, plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
ACCOUNTS = 2 * parallel.MIN_ROWS_PER_JOB + 301

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
        - {{ name: cu_signup, type: DATE, required: false }}
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
        - {{ name: a_since, type: DATE, required: false }}
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
"""

_BLOCK = """\
        rows: {rows}
{hidden}        fidelity:
          encoding: {{ fractionalBits: 16, rounding: floor }}
          records: {{ min: 1, max: 1000000 }}
          columns:
{columns}          properties:
            - property: mean
              column: {mean}
              tolerance: {{ type: absolute, value: "100" }}
          budget: {{ maxChecksPerState: 20, maxStateAge: P30D }}
          failureAction: withholdBundle
{rules}"""

_HIDDEN = "        hiddenColumns:\n          - { name: signup, type: DATE }\n"
_SIGNUP = "            signup: { range: { min: '2026-01-01', max: '2026-06-30' } }\n"
_CUSTOMER_RULES = (
    "        rules:\n"
    "          - id: signup_copy\n"
    "            language: flux-expr/3\n"
    "            derive: cu_signup\n"
    '            expr: "ADD_DAYS(signup, 0)"\n'
)
_ACCOUNT_RULES = (
    "        rules:\n"
    "          - id: since_copy\n"
    "            language: flux-expr/3\n"
    "            derive: a_since\n"
    '            expr: "ADD_DAYS(customer.signup, 0)"\n'
)


def _extension(*, hidden: bool) -> str:
    customer = _BLOCK.format(
        rows=40,
        hidden=_HIDDEN if hidden else "",
        columns="            cu_age: { range: { min: '18', max: '90' } }\n"
        + (
            _SIGNUP
            if hidden
            else "            cu_signup: { range: { min: '2026-01-01', max: '2026-06-30' } }\n"
        ),
        mean="cu_age",
        rules=_CUSTOMER_RULES if hidden else "",
    )
    account = _BLOCK.format(
        rows=ACCOUNTS,
        hidden="",
        columns="            a_score: { range: { min: '0', max: '100' } }\n"
        "            a_since: { range: { min: '2026-01-01', max: '2026-06-30' } }\n",
        mean="a_score",
        rules=_ACCOUNT_RULES if hidden else "",
    )
    return f"      customer:\n{customer}      account:\n{account}"


def make_bundle(tmp_path: Path, *, hidden: bool = True, name: str = "bundle") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_CONTRACT_TEXT, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("customer", "account")
    )
    extension = "extensions:\n  com.agentics.flux/1:\n    emits:\n" + _extension(hidden=hidden)
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_bundle(tmp_path_factory.mktemp("hidden-run"))


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _outcome(outcome: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in outcome.emits if e.resolved.expose_id == expose_id)


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


def _rewrite(bundle: Path, old: str, new: str) -> None:
    path = bundle / _SIMULATION
    text = path.read_text("utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), "utf-8")


# ---------------------------------------------------------------------------
# What is written and what is drawn
# ---------------------------------------------------------------------------


def _drawn(result: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    """Every row of *expose_id* as the generator draws it, hidden columns included."""
    outcome = _outcome(result, expose_id)
    assert outcome.plan is not None
    rows = generate_rows(outcome.plan, root=result.root)
    return [dict(zip(outcome.plan.expose.column_names, row, strict=True)) for row in rows]


@pytest.mark.integration
def test_the_written_rows_hold_no_hidden_column(result: pipeline.PipelineResult) -> None:
    customer = _outcome(result, "customer")
    assert customer.expose.column_names == ("cu_id", "cu_signup", "cu_age")
    assert all(len(row) == 3 for row in customer.rows)
    assert customer.plan is not None
    assert customer.plan.expose.column_names == ("cu_id", "cu_signup", "cu_age", "signup")
    assert customer.writer.result().rows == 40


@pytest.mark.integration
def test_a_contract_column_equals_what_the_hidden_column_drew(
    result: pipeline.PipelineResult,
) -> None:
    drawn = _drawn(result, "customer")
    written = _tables(result)["customer"]
    assert len(drawn) == len(written) == 40
    for full, row in zip(drawn, written, strict=True):
        assert isinstance(full["signup"], date)
        assert date(2026, 1, 1) <= full["signup"] <= date(2026, 6, 30)
        assert row["cu_signup"] == full["signup"]
        assert {name: full[name] for name in row} == row


@pytest.mark.integration
def test_a_path_through_a_link_reads_the_parents_hidden_column(
    result: pipeline.PipelineResult,
) -> None:
    signup = {row["cu_id"]: row["signup"] for row in _drawn(result, "customer")}
    linked = 0
    for row in _tables(result)["account"]:
        if row["a_customer"] is None:
            assert row["a_since"] is None
            continue
        assert row["a_since"] == signup[row["a_customer"]]
        linked += 1
    assert linked > 0.8 * ACCOUNTS


@pytest.mark.integration
def test_the_plan_lists_the_hidden_column_apart_and_the_coverage_leaves_it_out(
    result: pipeline.PipelineResult,
) -> None:
    customer = _outcome(result, "customer")
    assert customer.plan is not None
    summary = _fidelity_summary(customer)
    assert summary is not None
    assert summary["columns"] == ["cu_id", "cu_signup", "cu_age"]
    assert summary["hiddenColumns"] == ["signup"]
    assert [row.column for row in customer.coverage.columns] == ["cu_id", "cu_signup", "cu_age"]
    document = customer.plan.as_document()
    assert "signup" in json.dumps(document["columns"])  # planned: the generatorDigest covers it
    account = _fidelity_summary(_outcome(result, "account"))
    assert account is not None and "hiddenColumns" not in account


@pytest.mark.integration
def test_the_hidden_column_is_in_h_and_not_in_g(tmp_path: Path) -> None:
    with_hidden = pipeline.execute(make_bundle(tmp_path, name="a"), kernel=KERNEL_V2)
    without = pipeline.execute(make_bundle(tmp_path, hidden=False, name="b"), kernel=KERNEL_V2)
    first, second = _outcome(with_hidden, "customer"), _outcome(without, "customer")
    assert first.digests.h != second.digests.h
    # The block is hashed whole, so the key moves H; G leaves the member out, and the one
    # statistic the hidden column adds is the only other thing the two bundles differ in.
    assert "hiddenColumns" in str(first.resolved.extension)
    assert first.digests.g != second.digests.g


@pytest.mark.integration
def test_g_does_not_move_when_only_the_hidden_declaration_does(tmp_path: Path) -> None:
    one = make_bundle(tmp_path, name="one")
    other = make_bundle(tmp_path, name="other")
    # A second hidden column with no statistics of its own takes the time bounds: it moves H and
    # no statistic G reads.
    _rewrite(other, _HIDDEN, _HIDDEN + "          - { name: spare, type: DATE }\n")
    first = pipeline.execute(one, kernel=KERNEL_V2)
    second = pipeline.execute(other, kernel=KERNEL_V2)
    assert _outcome(first, "customer").digests.g == _outcome(second, "customer").digests.g
    assert _outcome(first, "customer").digests.h != _outcome(second, "customer").digests.h


# ---------------------------------------------------------------------------
# The column path, the slices and the kernels
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_column_path_gives_the_rows_of_the_per_row_path(
    result: pipeline.PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    outputs: Dict[str, str] = {}
    for name, patched in (("columns", spy), ("rows", lambda *args: None)):
        clear_memos()
        with monkeypatch.context() as patch:
            patch.setattr(columns, "column_rows", patched)
            outputs[name] = repr([_drawn(result, "customer"), _drawn(result, "account")])
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]
    assert "signup" in outputs["rows"]


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_key_and_says_which_kernel_generates_it(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105"
    assert "hiddenColumns" in str(caught.value) and "kernel 0.2.0" in str(caught.value)


@pytest.mark.integration
def test_validate_refuses_the_key_under_kernel_0_1_0_and_accepts_it_otherwise(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert validate(bundle).exit_code == 0
    monkeypatch.setattr(
        sys.modules["flux_engine.stages.validate"], "new_plan_is_counter_based", lambda: False
    )
    result = validate(bundle)
    assert result.exit_code == 1
    assert result.error_code == "FLX-3105"
    assert "kernel 0.1.0" in result.stderr


@pytest.mark.integration
def test_a_bundle_without_the_key_is_the_bundle_it_was(tmp_path: Path) -> None:
    plain = pipeline.execute(make_bundle(tmp_path, hidden=False), kernel=KERNEL_V2)
    for outcome in plain.emits:
        assert outcome.plan is not None
        assert outcome.plan.expose.hidden_count == 0
        assert outcome.plan.expose.column_names == outcome.expose.column_names
        assert "hiddenColumns" not in (_fidelity_summary(outcome) or {})


@pytest.mark.integration
def test_a_hidden_column_needs_a_fidelity_block(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    text = (bundle / _SIMULATION).read_text("utf-8")
    start = text.index("      customer:\n")
    end = text.index("      account:\n")
    (bundle / _SIMULATION).write_text(
        text[:start] + "      customer:\n        rows: 40\n" + _HIDDEN + text[end:], "utf-8"
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3105"


# ---------------------------------------------------------------------------
# plan, apply and verify
# ---------------------------------------------------------------------------


def _loop(bundle: Path, state: Path, jobs: int) -> Dict[str, Any]:
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
    return {
        "layout": layout,
        "plan": planned.artifacts.get("plan_digest"),
        "digests": {
            k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"
        },
        "verify": verified,
    }


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_give_the_same_digests_for_any_number_of_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    runs = [_loop(bundle, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for run in runs:
        layout = run["layout"]
        lines = layout.emit_path("customer").read_text("utf-8").splitlines()
        assert len(lines) == 40
        assert all(len(json.loads(line)) == 3 for line in lines)
        attested = json.loads(layout.seam_attestation_path("customer").read_text("utf-8"))
        assert attested["columns"] == ["cu_id", "cu_signup", "cu_age"]
        account_lines = layout.emit_path("account").read_text("utf-8").splitlines()
        assert all("signup" not in line for line in account_lines)
        assert '"signup"' not in layout.column_provenance_path.read_text("utf-8")


@pytest.mark.slow
@pytest.mark.integration
def test_the_command_line_runs_plan_apply_and_verify_on_a_bundle_with_a_hidden_column(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state]) == 0
    planned = json.loads(capsys.readouterr().out)
    run_id = planned["artifacts"]["run_id"]
    document = json.loads(
        RunLayout.for_state_dir(state, run_id).plan_path.read_text(encoding="utf-8")
    )
    customer = document["emits"]["customer"]["generator"]
    assert customer["columns"] == ["cu_id", "cu_signup", "cu_age"]
    assert customer["hiddenColumns"] == ["signup"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "-j",
                "verify",
                str(bundle),
                "--state-dir",
                state,
                "--run-id",
                run_id,
                "--allow-declared-baseline-drift",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["stage"] == "verify"
    assert "signup" not in json.dumps(verified["artifacts"].get("rules", {}))


@pytest.mark.integration
def test_a_hidden_column_that_is_a_contract_column_is_refused_in_the_cli(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken"
    shutil.copytree(bundle, broken)
    _rewrite(broken, "{ name: signup, type: DATE }", "{ name: cu_age, type: DATE }")
    assert main(["-j", "plan", str(broken), "--state-dir", str(tmp_path / ".flux")]) == 1
    assert "FLX-3105" in capsys.readouterr().err
