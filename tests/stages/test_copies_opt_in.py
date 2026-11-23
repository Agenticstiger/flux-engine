# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy generates only where the Simulation asks for copies: ``copies: true`` (ADR 0022 section 4).

KERNEL-SPEC-2 section 12 makes a change to what a plan generates additive only when a new
declaration reads it. A contract that lists ``sharedColumns`` or ``sharedAttribute`` was read on
origin/main (v0.9.0) as declared and not generated, so a kernel 0.2.0 plan written before copies
existed must replay to its digests:

* without ``copies``, the bundles of ``test_shared_columns_run`` and ``test_shared_attribute_run``
  give, under kernel 0.2.0, the row hashes origin/main gave (computed by running the same bundles
  on origin/main's code and pinned here), list each declaration as declared with no reason, plan
  no copy rule and score or report no copy in ``verify``;
* with ``copies: true``, kernel 0.2.0 generates the copies and ``verify`` scores them;
* kernel 0.1.0 refuses the key (``FLX-3105``), and so does a value other than ``true``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify

from .test_shared_attribute_run import _ATTRIBUTES
from .test_shared_columns_run import _CONTRACT_TEXT, _bundle

pytestmark = pytest.mark.integration

SIMULATION = "sim-store-star"

#: The first 16 hex digits of the SHA-256 of each emit's rows, as ``_row_hashes`` takes them,
#: that origin/main gives for the bundle under kernel 0.2.0 (the same bundle, the same kernel,
#: run on origin/main's code at 2739cae).
MAIN_VERIFY_ARTIFACTS = [
    "artifactComponents",
    "declaredBaselineDrift",
    "fidelity",
    "golden",
    "headline",
    "integrity",
    "reproducibility",
    "rfc04",
    "rowConformance",
    "validity",
    "verdict",
]
MAIN_SHARED_COLUMNS = {
    "customer": "9f9ca148b0859b1d",
    "account": "989c62dbde024041",
    "invoice": "cb8d7b83a33588df",
}
#: The plan and run digests origin/main gives the ``test_shared_columns_run`` bundle, planned and
#: applied with ``--kernel 0.2.0``.
MAIN_RUN_ID = "40e859fc1766822d"
MAIN_PLAN_DIGEST = "sha256:40e859fc1766822d5fb67e214ea453bbe6ab1bf56d4379426ad40f0f9f76e5fd"
MAIN_INPUT_DIGEST = "sha256:64153d6b8fe9502febd6c6c8b3f41aba79a13228e279a9f5abd18c3bf10b2add"
MAIN_PROVENANCE_DIGEST = "sha256:0b9e93b9a408132819e346a957de7c536b98d8ea07278bb737ae3155d2b2c48f"
MAIN_RUN_DIGEST = "sha256:9e019fdc3136dad67db7c768dda262545743e3018eb5300c865d77bcfc27d0b7"
MAIN_GENERATOR_DIGESTS = {
    "account": "sha256:898b689a420cb8fb7e37d2b568d14046c106cc78b13ec0535115735dde289636",
    "customer": "sha256:cc58230d0ee8bb18362a0e6bf593936448c6553ec2addf8dfe90cc5be2a464f3",
    "invoice": "sha256:a6ee7910aae188f3235909fd9e58e285943e89941aa408ecfa05f1837fcf7705",
}
MAIN_SHARED_ATTRIBUTE = {
    "customer": "0cbb53399a0c5770",
    "account": "1722531727a44ece",
    "invoice": "88a8fb40b68198e1",
}


def _row_hashes(outcome: pipeline.PipelineResult) -> Dict[str, str]:
    found = {}
    for emit in outcome.emits:
        rows = [dict(zip(list(emit.expose.column_names), row, strict=True)) for row in emit.rows]
        text = json.dumps(rows, sort_keys=True, default=str)
        found[emit.resolved.expose_id] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return found


@pytest.fixture(scope="module")
def columns(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("opt-in-columns"), copies=False)


@pytest.fixture(scope="module")
def attributes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("opt-in-attributes"), copies=False, contract=_ATTRIBUTES)


@pytest.fixture(scope="module")
def asked(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("opt-in-asked"), copies=True)


def test_a_bundle_without_copies_gives_main_its_rows_under_kernel_0_2_0(
    columns: Path, attributes: Path
) -> None:
    assert _row_hashes(pipeline.execute(columns, kernel=KERNEL_V2)) == MAIN_SHARED_COLUMNS
    assert _row_hashes(pipeline.execute(attributes, kernel=KERNEL_V2)) == MAIN_SHARED_ATTRIBUTE


def test_a_bundle_without_copies_lists_every_declaration_and_plans_no_copy(columns: Path) -> None:
    outcome = pipeline.execute(columns, kernel=KERNEL_V2)
    by_expose = {emit.resolved.expose_id: emit for emit in outcome.emits}
    assert [row.names for row in by_expose["invoice"].not_generated] == [
        ("region", "a_score", "i_account")
    ]
    assert [row.names for row in by_expose["account"].not_generated] == [("region",)]
    assert all(row.reason == "" for emit in outcome.emits for row in emit.not_generated)
    for emit in outcome.emits:
        assert emit.plan is not None and emit.plan.rules is None
        assert all(column.decided_by != "rule" for column in emit.coverage.columns)


def test_copies_true_generates_the_copies_under_kernel_0_2_0(asked: Path, columns: Path) -> None:
    asking = pipeline.execute(asked, kernel=KERNEL_V2)
    assert _row_hashes(asking)["account"] != MAIN_SHARED_COLUMNS["account"]
    invoice = next(emit for emit in asking.emits if emit.resolved.expose_id == "invoice")
    assert invoice.plan is not None and invoice.plan.rules is not None
    (row,) = invoice.not_generated
    assert (row.kind, row.names) == ("shared-columns", ("i_account",)) and row.reason


def test_copies_true_is_bound_into_h_of_the_emits_that_declare_something_to_copy(
    asked: Path, columns: Path
) -> None:
    held = {
        emit.expose_id: emit.extension
        for emit in resolve_all_emits(load_bundle(columns), simulation_id=SIMULATION)
    }
    assert all("copies" not in (extension or {}) for extension in held.values())
    asking = {
        emit.expose_id: emit.extension
        for emit in resolve_all_emits(load_bundle(asked), simulation_id=SIMULATION)
    }
    # The customer lists nothing to copy, so the key changes nothing in it.
    assert "copies" not in asking["customer"]
    assert asking["account"]["copies"] is True and asking["invoice"]["copies"] is True


def test_a_plan_without_copies_has_the_plan_digests_origin_main_gives(
    columns: Path, tmp_path: Path
) -> None:
    state = tmp_path / ".flux"
    planned = plan(columns, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    assert layout.run_id == MAIN_RUN_ID
    assert document["planDigest"] == MAIN_PLAN_DIGEST
    assert document["inputDigest"] == MAIN_INPUT_DIGEST
    assert document["columnProvenanceDigest"] == MAIN_PROVENANCE_DIGEST
    assert {
        expose_id: entry["generator"]["generatorDigest"]
        for expose_id, entry in document["emits"].items()
    } == MAIN_GENERATOR_DIGESTS
    assert "reason" not in json.dumps(document)
    applied = apply(columns, state_dir=state, run_id=layout.run_id)
    assert applied.success, applied.stderr
    assert str(applied.artifacts["run_digest"]) == MAIN_RUN_DIGEST


def test_verify_scores_and_reports_no_copy_without_copies(columns: Path, tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    planned = plan(columns, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(columns, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        columns, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    assert verified.success, verified.stderr
    # No rule line at all, scored or reported: the artifacts are the keys origin/main gives.
    assert sorted(verified.artifacts) == MAIN_VERIFY_ARTIFACTS
    assert "shared" not in verified.stdout and "not enforced" not in verified.stdout


def test_copies_true_is_refused_on_kernel_0_1_0_naming_the_kernel(
    asked: Path, tmp_path: Path
) -> None:
    with pytest.raises(FluxError) as raised:
        pipeline.execute(asked, kernel=KERNEL_V1)
    assert raised.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(raised.value) and "copies" in str(raised.value)
    planned = plan(asked, state_dir=tmp_path / ".flux", kernel="0.1.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "kernel 0.1.0" in planned.stderr


def test_copies_true_is_refused_on_kernel_0_1_0_even_where_nothing_declares_a_copy(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, copies=True, contract=_NO_COPIES)
    with pytest.raises(FluxError, match="kernel 0.1.0"):
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert validate(bundle).exit_code == 0
    assert pipeline.execute(bundle, kernel=KERNEL_V2) is not None


_NO_COPIES = re.sub(r"\n *sharedColumns: \[[^\]]*\]", "", _CONTRACT_TEXT)
_NO_KEYS = _CONTRACT_TEXT.replace(
    "sharedColumns: [region, a_score, i_account]", "sharedColumns: [region, a_score]"
)


@pytest.mark.parametrize("value", ["false", "'yes'", "1", "[true]", "{ on: true }"])
def test_a_value_other_than_true_is_refused(value: str, tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, copies=True)
    path = bundle / "simulation.flux.yml"
    path.write_text(
        path.read_text("utf-8").replace("    copies: true\n", f"    copies: {value}\n"), "utf-8"
    )
    with pytest.raises(FluxError) as raised:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert raised.value.code.code == "FLX-3105" and "copies" in str(raised.value)


def test_strict_coverage_refuses_the_declarations_without_copies_and_accepts_them_with_it(
    tmp_path: Path,
) -> None:
    without = _bundle(tmp_path, copies=False, contract=_NO_KEYS, name="without")
    with pytest.raises(FluxError) as raised:
        pipeline.execute(without, kernel=KERNEL_V2, strict_coverage=True)
    assert raised.value.code.code == "FLX-3305"
    assert "copies: true" in str(raised.value)
    asking = _bundle(tmp_path, copies=True, contract=_NO_KEYS, name="asking")
    pipeline.execute(asking, kernel=KERNEL_V2, strict_coverage=True)
