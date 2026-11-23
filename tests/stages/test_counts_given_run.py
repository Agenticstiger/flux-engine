# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent given a parent column, end to end (ADR 0037 decisions 4 and 5).

The bundle is ``tests/fixtures/exact-counts`` whose ``counts`` entry for the invoices of each account
is given the account's ``segment`` (retail, trade and public) with a weight list for ``retail`` and
``trade``; ``public`` has none and takes the shape's weights. Every run is on kernel 0.2.0. Each
test holds one promise the declaration makes:

* **The layout is the table.** The invoices of each account are the layout given the segment of the
  account's final row, and the classes' mean numbers of children follow their weights.
* **Two paths, one answer.** The column path's rows equal the per-row path's, ``--jobs`` and
  ``--shard`` change no byte, and ``--scale`` changes only the count, below 1 too.
* **The bundle.** The entry enters ``H`` and leaves ``G``, the plan states the weights a class took
  from the shape, ``ship`` writes the evidence with the plan's ``G``, kernel 0.1.0 refuses the key
  naming the kernel, and what the layout cannot read is ``FLX-3403``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.counts import count_key, counted_layout
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import ship
from tests.generator.test_rules_over_links import _by_both_paths

from .test_counts_run import (
    ACCOUNTS,
    INVOICES,
    INVOICES_PER_ACCOUNT,
    MANY_LINES_PER_INVOICE,
    _flow,
    _table,
    bundle_with,
)

pytestmark = pytest.mark.integration

_KEY = bytes(range(96, 128))
CLASSES = ["public", "retail", "trade"]
RETAIL = ["0.70", "0.20", "0.10"]
TRADE = ["0.10", "0.30", "0.60"]
BASE_ENTRY = f"emit: invoice, relationship: account, children: {_flow(INVOICES_PER_ACCOUNT)}"


def given_bundle(
    tmp_path: Path,
    name: str = "given",
    *,
    given: str = "segment",
    lists: Optional[Dict[str, List[str]]] = None,
    lines: Optional[Dict[str, Any]] = None,
    extra: str = "",
) -> Path:
    """``exact-counts`` whose invoices per account are given *given*, with *lists* per class."""
    held = {"retail": RETAIL, "trade": TRADE} if lists is None else lists
    target = bundle_with(tmp_path, name, **({} if lines is None else {"lines": lines}))
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count(BASE_ENTRY) == 1
    given_text = f"given: {given}" + (f", childrenGiven: {json.dumps(held)}" if held else "")
    text = text.replace(BASE_ENTRY, f"{BASE_ENTRY}, {given_text}{extra}")
    path.write_text(text, "utf-8")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return given_bundle(tmp_path_factory.mktemp("given"))


@pytest.fixture(scope="module")
def outcome(bundle: Path) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _per_account(outcome: Any) -> List[int]:
    held: Dict[Any, int] = {}
    for row in _table(outcome, "invoice"):
        held[row["account_id"]] = held.get(row["account_id"], 0) + 1
    return [held.get(row["account_id"], 0) for row in _table(outcome, "account")]


def _expected_counts(outcome: Any, shape_weights: Dict[str, List[str]]) -> List[int]:
    accounts = _table(outcome, "account")
    names = sorted({row["segment"] for row in accounts})
    assert names == CLASSES
    per_class = [
        {"levels": INVOICES_PER_ACCOUNT["levels"], "levelWeights": shape_weights[name]}
        for name in names
    ]
    return counted_layout(
        count_key(outcome.root, outcome.emit("invoice").plan.sim_id, "invoice", "account"),
        len(accounts),
        len(_table(outcome, "invoice")),
        class_shapes=per_class,
        groups=[names.index(row["segment"]) for row in accounts],
    ).counts.tolist()


WEIGHTS = {"public": INVOICES_PER_ACCOUNT["levelWeights"], "retail": RETAIL, "trade": TRADE}


def test_every_account_holds_the_invoices_the_layout_given_its_segment_allots_it(
    outcome: Any,
) -> None:
    assert _per_account(outcome) == _expected_counts(outcome, WEIGHTS)
    assert sum(_per_account(outcome)) == INVOICES


def test_the_mean_invoices_of_a_segment_follow_its_weights_and_the_shape_serves_the_rest(
    outcome: Any,
) -> None:
    levels = [int(text) for text in INVOICES_PER_ACCOUNT["levels"]]
    wanted = {
        name: sum(
            level * float(weight) for level, weight in zip(levels, WEIGHTS[name], strict=True)
        )
        for name in CLASSES
    }
    held: Dict[str, List[int]] = {name: [] for name in CLASSES}
    for row, count in zip(_table(outcome, "account"), _per_account(outcome), strict=True):
        held[row["segment"]].append(count)
    # The invoices are spread over the slots, so each mean is the weights' mean times one factor
    # (the factor is the invoices over the slots' total), up to the whole counts of the allotment.
    slots = sum(wanted[name] * len(held[name]) for name in CLASSES)
    for name in CLASSES:
        assert sum(held[name]) / len(held[name]) == pytest.approx(
            wanted[name] * INVOICES / slots, rel=0.08
        ), name
    means = {name: sum(held[name]) / len(held[name]) for name in CLASSES}
    assert means["retail"] < means["public"] < means["trade"]


def test_a_given_entry_with_no_lists_gives_every_class_the_shapes_weights(tmp_path: Path) -> None:
    given = pipeline.execute(given_bundle(tmp_path, "none", lists={}), kernel=KERNEL_V2)
    assert _per_account(given) == _expected_counts(
        given, {name: INVOICES_PER_ACCOUNT["levelWeights"] for name in CLASSES}
    )
    assert sum(_per_account(given)) == INVOICES


@pytest.mark.parametrize("chunk", [4096, 100])
def test_the_column_paths_rows_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, bundle: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "invoice_line", "account", "product"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, count: int) -> None:
    whole = pipeline.execute(bundle, kernel=KERNEL_V2)
    parts = [
        pipeline.execute(bundle, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    for expose_id in ("invoice", "invoice_line"):
        joined = b"".join(part.emit(expose_id).writer.stream_bytes() for part in parts)
        assert joined == whole.emit(expose_id).writer.stream_bytes(), expose_id


def _digests(applied: Any) -> Dict[str, Any]:
    return {k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_digests_or_the_bytes(tmp_path: Path) -> None:
    many = given_bundle(tmp_path, "many", lines=MANY_LINES_PER_INVOICE)
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("invoice_line").processes > 1
    for expose_id in ("invoice", "invoice_line"):
        assert (
            runs[0].emit(expose_id).writer.stream_bytes()
            == runs[1].emit(expose_id).writer.stream_bytes()
        )
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(many, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(many, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]


@pytest.mark.parametrize("scale", ["0.25", "0.5", "2"])
def test_a_scaled_run_keeps_the_layout_given_the_segment(
    bundle: Path, tmp_path: Path, scale: str
) -> None:
    scaled = pipeline.execute(bundle, kernel=KERNEL_V2, scale=Decimal(scale))
    accounts, invoices = _table(scaled, "account"), _table(scaled, "invoice")
    assert len(invoices) == int(INVOICES * Decimal(scale))
    assert len(accounts) == int(ACCOUNTS * Decimal(scale))
    assert _per_account(scaled) == _expected_counts(scaled, WEIGHTS)
    planned = plan(bundle, state_dir=tmp_path / "state", kernel="0.2.0", scale=scale, stage=False)
    assert planned.success, planned.stderr
    applied = apply(bundle, run_id=str(planned.artifacts["run_id"]), state_dir=tmp_path / "state")
    assert applied.success, applied.stderr


# -- the bundle: H, G, the plan, ship, the kernel ----------------------------------------------


def _planned_emits(bundle: Path, state: Path) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    emits: Dict[str, Any] = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    return emits


def test_the_entry_moves_h_of_the_counted_emit_and_leaves_g_and_the_others(
    bundle: Path, tmp_path: Path
) -> None:
    given = _planned_emits(bundle, tmp_path / "given")
    plain = _planned_emits(bundle_with(tmp_path, "plain"), tmp_path / "plain")
    assert given["invoice"]["H"] != plain["invoice"]["H"]
    assert given["invoice"]["generator"]["generatorDigest"] != (
        plain["invoice"]["generator"]["generatorDigest"]
    )
    for expose_id in given:
        assert given[expose_id]["G"] == plain[expose_id]["G"], expose_id
    # another list moves H again
    other = _planned_emits(
        given_bundle(tmp_path, "other", lists={"retail": TRADE, "trade": RETAIL}),
        tmp_path / "other",
    )
    assert other["invoice"]["H"] != given["invoice"]["H"]
    assert other["invoice"]["G"] == given["invoice"]["G"]


def test_the_plan_states_the_weights_a_class_took_from_the_shape(
    bundle: Path, tmp_path: Path
) -> None:
    emits = _planned_emits(bundle, tmp_path / "state")
    (stated,) = emits["invoice"]["countsGiven"]
    assert stated["relationship"] == "account" and stated["given"] == "segment"
    assert stated["childrenGiven"] == {
        "public": INVOICES_PER_ACCOUNT["levelWeights"],
        "retail": RETAIL,
        "trade": TRADE,
    }
    assert stated["childrenGivenDefault"] == ["public"]
    assert all("countsGiven" not in held for name, held in emits.items() if name != "invoice")


def _slice_of(layout: RunLayout, expose: str, target: Path) -> str:
    columns_ = json.loads(layout.seam_attestation_path(expose).read_text("utf-8"))["columns"]
    rows = [
        dict(zip(columns_, json.loads(line), strict=True))
        for line in layout.emit_path(expose).read_text("utf-8").splitlines()
        if line
    ]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return target.as_uri()


def test_the_plans_g_is_the_g_ship_recompiles_and_ship_writes_the_evidence(
    bundle: Path, tmp_path: Path
) -> None:
    planned = _planned_emits(bundle, tmp_path / "planned")
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state, run_id)
    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={
            expose: _slice_of(layout, expose, tmp_path / f"{expose}.ndjson")
            for expose in ("product", "account", "invoice", "invoice_line")
        },
        signing_key=_KEY,
    )
    assert result.success, result.stderr
    assert (layout.run_dir / "EVIDENCE.md").is_file()
    recorded = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    for expose_id, held in recorded.items():
        assert held["G"] == planned[expose_id]["G"] == held["fidelity"]["G"], expose_id
    assert recorded["invoice"]["H"] == planned["invoice"]["H"]


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_validate_reads_the_declaration(bundle: Path) -> None:
    from flux_engine.stages.validate import validate

    assert validate(bundle).success
