# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose customers are a hidden table (ADR 0024 section 3).

The bundle of ``test_hidden_columns_run`` is used with the customer made ``hidden: true`` and the
account deriving its ``a_since`` from the customer's ``cu_signup`` over the link. The customer is
planned and generated, and a path reads its rows, but the run has no stream, file, receipt entry
or output digest for it. The tests run the bundle through the pipeline, then through ``plan``,
``apply`` and ``verify`` at ``--jobs 1`` and ``--jobs 4``, and through the command line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator.rows import generate_rows
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.validate import validate

from .test_hidden_columns_run import _SIMULATION, _loop, _rewrite, make_bundle

_RULES = (
    "        rules:\n"
    "          - id: since_copy\n"
    "            language: flux-expr/3\n"
    "            derive: a_since\n"
    '            expr: "ADD_DAYS(customer.cu_signup, 0)"\n'
)


def hidden_bundle(tmp_path: Path, *, hidden: bool = True, name: str = "bundle") -> Path:
    bundle = make_bundle(tmp_path, hidden=False, name=name)
    path = bundle / _SIMULATION
    text = path.read_text("utf-8")
    assert text.endswith("failureAction: withholdBundle\n")
    text += _RULES
    if hidden:
        assert text.count("      customer:\n") == 1
        text = text.replace("      customer:\n", "      customer:\n        hidden: true\n")
    path.write_text(text, "utf-8")
    return bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return hidden_bundle(tmp_path_factory.mktemp("hidden-table"))


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


@pytest.mark.integration
def test_the_run_has_no_outcome_stream_or_digest_for_the_hidden_table(
    result: pipeline.PipelineResult,
) -> None:
    assert [e.resolved.expose_id for e in result.emits] == ["account"]
    assert list(result.components.emits) == ["account"]
    assert [h.resolved.expose_id for h in result.hidden_emits] == ["customer"]
    hidden = result.hidden_emits[0]
    assert hidden.plan.rows == 40 and hidden.digests.h.value.startswith("sha256:")


@pytest.mark.integration
def test_a_path_reads_the_hidden_tables_rows(result: pipeline.PipelineResult) -> None:
    hidden = result.hidden_emits[0]
    drawn = generate_rows(hidden.plan, root=result.root)
    names = hidden.plan.expose.column_names
    signup = {row[names.index("cu_id")]: row[names.index("cu_signup")] for row in drawn}
    account = result.emits[0]
    columns = account.expose.column_names
    linked = 0
    for row in account.rows:
        parent, since = row[columns.index("a_customer")], row[columns.index("a_since")]
        if parent is None:
            assert since is None
            continue
        assert since == signup[parent]
        linked += 1
    assert linked > 0.8 * len(account.rows)


@pytest.mark.integration
def test_the_plan_says_the_table_is_hidden_and_keeps_its_generator_digest(
    bundle: Path, tmp_path: Path
) -> None:
    from flux_engine.stages.plan import plan

    planned = plan(bundle, state_dir=tmp_path / "state")
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(tmp_path / "state", str(planned.artifacts["run_id"]))
    emits = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    assert emits["customer"]["hidden"] is True
    assert emits["customer"]["generator"]["generatorDigest"].startswith("sha256:")
    assert "hidden" not in emits["account"]


@pytest.mark.slow
@pytest.mark.integration
def test_no_stream_or_receipt_entry_exists_and_jobs_agree(bundle: Path, tmp_path: Path) -> None:
    runs = [_loop(bundle, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for run in runs:
        layout = run["layout"]
        assert not layout.emit_path("customer").exists()
        assert layout.emit_path("account").exists()
        assert sorted(p.name for p in layout.emits_dir.iterdir()) == [
            layout.emit_path("account").name
        ]
        receipt = json.loads(layout.receipt_path.read_text("utf-8"))
        assert list(receipt["emits"]) == ["account"]
        assert "customer" not in layout.run_digest_path.read_text("utf-8")


@pytest.mark.slow
@pytest.mark.integration
def test_the_command_line_runs_plan_apply_and_verify(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    code = main(
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
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "customer" not in captured.err


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kernel_0_1_0_refuses_a_hidden_table(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105"
    assert "hidden" in str(caught.value) and "kernel 0.1.0" in str(caught.value)


@pytest.mark.integration
@pytest.mark.parametrize("value", ["false", "'true'", "1", "{}", "null"])
def test_hidden_takes_only_true(tmp_path: Path, value: str) -> None:
    bundle = hidden_bundle(tmp_path)
    _rewrite(bundle, "        hidden: true\n", f"        hidden: {value}\n")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3105"


@pytest.mark.integration
def test_a_hidden_table_no_written_emit_reads_is_refused(tmp_path: Path) -> None:
    bundle = hidden_bundle(tmp_path, hidden=False)
    # The account is the one that is hidden, and the customer reads nothing of it.
    _rewrite(bundle, "      account:\n", "      account:\n        hidden: true\n")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3105"
    assert "'account'" in str(caught.value) and "no written emit reads it" in str(caught.value)
    assert validate(bundle).error_code == "FLX-3105"


@pytest.mark.integration
def test_a_hidden_table_without_a_fidelity_block_is_refused(tmp_path: Path) -> None:
    bundle = hidden_bundle(tmp_path)
    path = bundle / _SIMULATION
    text = path.read_text("utf-8")
    start = text.index("      customer:\n")
    end = text.index("      account:\n")
    path.write_text(
        text[:start] + "      customer:\n        hidden: true\n        rows: 40\n" + text[end:],
        "utf-8",
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3105"
    assert "fidelity" in str(caught.value)


@pytest.mark.integration
def test_validate_refuses_a_hidden_table_under_kernel_0_1_0(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    assert validate(bundle).exit_code == 0
    monkeypatch.setattr(
        sys.modules["flux_engine.stages.validate"], "new_plan_is_counter_based", lambda: False
    )
    result = validate(bundle)
    assert result.exit_code == 1 and result.error_code == "FLX-3105"


@pytest.mark.integration
def test_a_bundle_without_the_key_has_no_hidden_emit(tmp_path: Path) -> None:
    plain = pipeline.execute(hidden_bundle(tmp_path, hidden=False), kernel=KERNEL_V2)
    assert plain.hidden_emits == ()
    assert [e.resolved.expose_id for e in plain.emits] == ["customer", "account"]
