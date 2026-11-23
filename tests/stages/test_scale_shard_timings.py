# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--scale``, ``--shard`` and ``--timings``: the size of a run, a share of it, and where its time went."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.core.sizing import parse_scale, parse_shard, share_of
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.scaling import key_space_note, scale_emit

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"


# --- the values -------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("text", ["1", "2", "0.5", "10", "1000000", "0.000001"])
def test_a_usable_scale_is_read_exactly(text: str) -> None:
    assert parse_scale(text) == Decimal(text)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["0", "-1", "nan", "inf", "1e3", "1000001", "", "abc", " ", "1.", ".5", "1,5", True, None, [2]]
    + [float("nan"), float("inf"), 0, -2, 10**7],
)
def test_a_scale_that_is_not_usable_is_refused_by_code(value: Any) -> None:
    with pytest.raises(FluxError) as refused:
        parse_scale(value)
    assert refused.value.code.code == "FLX-3405"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "shard"), [("1/1", (1, 1)), ("2/4", (2, 4)), ("4/4", (4, 4)), (" 3/7 ", (3, 7))]
)
def test_a_share_is_i_of_n(text: str, shard: Any) -> None:
    assert parse_shard(text) == shard
    assert parse_shard(shard) == shard


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["0/4", "5/4", "1/0", "0/0", "-1/4", "1.5/4", "1/2/3", "a/b", "2", "", "1/", "/4", True, (1,)]
    + [(True, 2), (1.0, 2)],
)
def test_a_share_that_is_not_one_is_refused_by_code(value: Any) -> None:
    with pytest.raises(FluxError) as refused:
        parse_shard(value)
    assert refused.value.code.code == "FLX-3404"


@pytest.mark.unit
@pytest.mark.parametrize("rows", [0, 1, 5, 4096, 12299, 400_000])
@pytest.mark.parametrize("count", [1, 2, 3, 7, 16])
def test_the_shares_of_a_row_range_join_into_it_in_order(rows: int, count: int) -> None:
    shares = [share_of(rows, (index, count)) for index in range(1, count + 1)]
    assert shares[0][0] == 0 and shares[-1][1] == rows
    assert all(a[1] == b[0] for a, b in zip(shares, shares[1:], strict=False))
    assert sum(stop - start for start, stop in shares) == rows


# --- scale ------------------------------------------------------------------------------------


def _emits(directory: Path) -> Any:
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits

    return {
        emit.expose_id: emit
        for emit in resolve_all_emits(load_bundle(directory), simulation_id="sim-store-star")
    }


@pytest.mark.unit
def test_a_scale_of_one_changes_nothing() -> None:
    for emit in _emits(FIXTURE).values():
        assert scale_emit(emit, Decimal(1)) is emit


@pytest.mark.unit
def test_a_scale_multiplies_rows_records_and_key_spans_from_their_low_end() -> None:
    emits = _emits(FIXTURE)
    sales = scale_emit(emits["store_sales"], Decimal(10))
    assert sales.declared_row_count == 40000
    block = sales.fidelity
    assert block["records"] == {"min": 10, "max": 10_000_000}
    columns = block["columns"]
    # A column of the composite key grows from its minimum: 1..1000 held 1000 values.
    assert columns["ss_ticket_number"]["range"] == {"min": "1", "max": "10000"}
    # A column that is no key keeps its range.
    assert columns["ss_quantity"]["range"] == {"min": "1", "max": "10"}
    assert sales.emit is emits["store_sales"].emit  # H is taken over the emit as authored
    half = scale_emit(emits["item"], Decimal("0.5"))
    assert half.declared_row_count == 100
    assert half.fidelity["columns"]["i_item_sk"]["range"] == {"min": "1", "max": "100"}


@pytest.mark.unit
def test_a_scale_that_leaves_an_emit_too_many_rows_is_refused() -> None:
    with pytest.raises(FluxError) as refused:
        scale_emit(_emits(FIXTURE)["store_sales"], Decimal(1_000_000))
    assert refused.value.code.code == "FLX-3405"


@pytest.mark.integration
def test_a_scale_of_one_keeps_every_digest_and_any_other_is_recorded(tmp_path: Path) -> None:
    state = tmp_path / "state"
    plain = plan_module.plan(FIXTURE, state_dir=state / "a", kernel="0.2.0", stage=False)
    one = plan_module.plan(FIXTURE, state_dir=state / "b", kernel="0.2.0", scale="1", stage=False)
    ten = plan_module.plan(FIXTURE, state_dir=state / "c", kernel="0.2.0", scale="10", stage=False)
    assert plain.artifacts["plan_digest"] == one.artifacts["plan_digest"]
    assert "scale" not in plain.artifacts["plan_document"]
    assert ten.artifacts["plan_document"]["scale"] == "10"
    assert ten.artifacts["plan_digest"] != plain.artifacts["plan_digest"]


@pytest.mark.integration
def test_apply_runs_the_scale_its_plan_recorded(tmp_path: Path) -> None:
    state = tmp_path / "state"
    planned = plan_module.plan(FIXTURE, state_dir=state, kernel="0.2.0", scale="3", stage=False)
    applied = apply_module.apply(FIXTURE, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr
    assert "store_sales: 12000 rows" in applied.stdout
    refused = apply_module.apply(
        FIXTURE, run_id=planned.artifacts["run_id"], state_dir=state, scale="2"
    )
    assert refused.exit_code == 2


@pytest.mark.integration
def test_a_scale_and_a_shard_run_on_kernel_0_2_0_only(tmp_path: Path) -> None:
    refused = plan_module.plan(FIXTURE, state_dir=tmp_path / "a", scale="2", kernel="0.1.0")
    assert (refused.exit_code, refused.error_code) == (1, "FLX-3405")
    old = plan_module.plan(FIXTURE, state_dir=tmp_path / "b", kernel="0.1.0")
    shard = apply_module.apply(
        FIXTURE,
        run_id=old.artifacts["run_id"],
        state_dir=tmp_path / "b",
        shard="1/2",
        auto_plan=False,
    )
    assert shard.exit_code == 1 and shard.error_code == "FLX-3404"


@pytest.mark.unit
@pytest.mark.parametrize("scale", ["0.5", "0.25", "0.05", "0.01"])
def test_a_scale_below_one_cuts_the_bin_edges_inside_the_span_it_cuts_the_range_to(
    scale: str,
) -> None:
    """Every edge is held to the new range end, the last one at it, whatever the rounding."""
    sales = scale_emit(_emits(FIXTURE)["store_sales"], Decimal(scale))
    declared = sales.fidelity["columns"]["ss_sold_date_sk"]
    low, high = int(declared["range"]["min"]), int(declared["range"]["max"])
    edges = [int(edge) for edge in declared["bins"]]
    assert edges[0] == low and edges[-1] == high
    assert all(later > earlier for earlier, later in zip(edges, edges[1:], strict=False))
    # Held to the same rule, the range end of 365 values at scale F is 365 * F values long.
    assert high - low + 1 == int((Decimal(365) * Decimal(scale)).to_integral_value("ROUND_HALF_UP"))


@pytest.mark.integration
@pytest.mark.parametrize("scale", ["0.25", "0.05"])
def test_a_scale_below_one_plans_and_applies_a_valid_bundle(tmp_path: Path, scale: str) -> None:
    state = tmp_path / "state"
    planned = plan_module.plan(FIXTURE, state_dir=state, kernel="0.2.0", scale=scale, stage=False)
    assert planned.exit_code == 0, planned.stderr
    applied = apply_module.apply(FIXTURE, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr


@pytest.mark.integration
def test_a_scale_that_leaves_a_composite_key_too_few_values_names_the_scale(
    tmp_path: Path,
) -> None:
    """A key over two columns cut to a hundredth holds a ten-thousandth of its pairs."""
    planned = plan_module.plan(
        FIXTURE, state_dir=tmp_path / "state", kernel="0.2.0", scale="0.01", stage=False
    )
    assert planned.exit_code == 1 and planned.error_code == "FLX-3403"
    assert "--scale 0.01" in planned.stderr and "larger --scale" in planned.stderr


@pytest.mark.unit
@pytest.mark.parametrize(("scale", "way"), [("0.5", "larger --scale"), ("2", "smaller --scale")])
def test_a_key_left_short_by_a_scale_is_told_which_way_to_move_it(scale: str, way: str) -> None:
    note = key_space_note(Decimal(scale))
    assert f"--scale {scale}" in note and way in note


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scale", "column"), [("0.002", "i_item_sk"), ("0.005", "i_item_sk"), ("0.0001", "d_date_sk")]
)
def test_a_scale_that_leaves_a_key_column_under_two_values_is_refused_naming_scale_and_column(
    scale: str, column: str
) -> None:
    refusals = []
    for emit in _emits(FIXTURE).values():
        try:
            scale_emit(emit, Decimal(scale))
        except FluxError as refused:
            refusals.append(refused)
    assert {refused.code.code for refused in refusals} == {"FLX-3405"}
    assert any("--scale" in str(r) and f"key column {column} " in str(r) for r in refusals)


@pytest.mark.unit
def test_a_scale_that_merges_bins_is_refused_as_a_scale_not_as_a_bundle() -> None:
    with pytest.raises(FluxError) as refused:
        scale_emit(_emits(FIXTURE)["store_sales"], Decimal("0.003"))
    assert refused.value.code.code == "FLX-3405"
    assert "--scale" in str(refused.value) and "ss_sold_date_sk" in str(refused.value)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "1000001"])
def test_the_refusal_of_a_scale_out_of_bounds_says_what_it_allows(value: str) -> None:
    with pytest.raises(FluxError) as refused:
        parse_scale(value)
    assert "above 0 and at most 1000000" in str(refused.value)


# --- shard ------------------------------------------------------------------------------------


def _joined(layout: RunLayout, expose_id: str, count: int) -> bytes:
    return b"".join(
        layout.shard_emit_path(expose_id, index, count).read_bytes()
        for index in range(1, count + 1)
    )


@pytest.mark.integration
@pytest.mark.parametrize("count", [1, 3, 5])
def test_the_shares_joined_in_order_are_the_bytes_of_one_run(tmp_path: Path, count: int) -> None:
    whole_state = tmp_path / "whole"
    planned = plan_module.plan(FIXTURE, state_dir=whole_state, kernel="0.2.0", stage=False)
    run_id = planned.artifacts["run_id"]
    assert apply_module.apply(FIXTURE, run_id=run_id, state_dir=whole_state).success
    layout = RunLayout.for_state_dir(whole_state, run_id)

    shard_state = tmp_path / "shards"
    shard_plan = plan_module.plan(FIXTURE, state_dir=shard_state, kernel="0.2.0", stage=False)
    assert shard_plan.artifacts["plan_digest"] == planned.artifacts["plan_digest"]
    shards = RunLayout.for_state_dir(shard_state, run_id)
    for index in range(1, count + 1):
        part = apply_module.apply(
            FIXTURE, run_id=run_id, state_dir=shard_state, shard=(index, count)
        )
        assert part.success, part.stderr
    assert not shards.receipt_path.exists() and not shards.run_digest_path.exists()
    for expose_id in ("date_dim", "item", "customer", "store_sales", "store_returns"):
        assert _joined(shards, expose_id, count) == layout.emit_path(expose_id).read_bytes()
    record = json.loads(shards.shard_manifest_path(1, count).read_text(encoding="utf-8"))
    assert record["planDigest"] == planned.artifacts["plan_digest"]
    assert [e["exposeId"] for e in record["emits"]]


@pytest.mark.integration
def test_verify_and_ship_name_a_run_directory_that_holds_only_shares(tmp_path: Path) -> None:
    from flux_engine.stages.ship import ship
    from flux_engine.stages.verify import verify

    state = tmp_path / "shards"
    planned = plan_module.plan(FIXTURE, state_dir=state, kernel="0.2.0", stage=False)
    run_id = planned.artifacts["run_id"]
    for index in (1, 2):
        part = apply_module.apply(FIXTURE, run_id=run_id, state_dir=state, shard=(index, 2))
        assert part.success, part.stderr
    for result in (
        verify(FIXTURE, run_id=run_id, state_dir=state),
        ship(FIXTURE, run_id=run_id, state_dir=state, out_dir=tmp_path / "out"),
    ):
        assert result.error_code == "FLX-2109"
        assert "holds only shares (shard-1-of-2.json and others)" in result.stderr
        assert "without --shard" in result.stderr
    # A run directory that holds nothing keeps the plain message.
    nothing = plan_module.plan(FIXTURE, state_dir=tmp_path / "none", kernel="0.2.0", stage=False)
    plain = verify(FIXTURE, run_id=nothing.artifacts["run_id"], state_dir=tmp_path / "none")
    assert plain.error_code == "FLX-2109" and "holds only shares" not in plain.stderr
    assert "run `fluxsim apply` first" in plain.stderr


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_the_shares_made_by_worker_processes_are_the_same_bytes(tmp_path: Path) -> None:
    target = tmp_path / "star"
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("rows: 4000\n", "rows: 30000\n"),
        encoding="utf-8",
    )
    whole = pipeline.execute(target, kernel=KERNEL_V2)
    parts = [pipeline.execute(target, kernel=KERNEL_V2, jobs=3, shard=(i, 2)) for i in (1, 2)]
    sales = [r.emit("store_sales") for r in (whole, *parts)]
    assert parts[0].emit("store_sales").processes > 1
    joined = b"".join(p.writer.stream_bytes() for p in sales[1:])
    assert joined == sales[0].writer.stream_bytes()


# --- timings ----------------------------------------------------------------------------------


@pytest.mark.integration
def test_timings_name_where_the_time_went_and_move_no_digest(tmp_path: Path) -> None:
    plain = plan_module.plan(FIXTURE, state_dir=tmp_path / "a")
    timed = plan_module.plan(FIXTURE, state_dir=tmp_path / "b", timings=True)
    assert plain.artifacts["plan_digest"] == timed.artifacts["plan_digest"]
    assert "timings" not in plain.artifacts and "timings (seconds" not in plain.stdout
    names = {span["name"] for span in timed.artifacts["timings"]["spans"]}
    assert {"load", "compile", "simulate", "emit store_sales", "digests", "attestation"} <= names
    assert "timings (seconds" in timed.stdout
    for run in (tmp_path / "a", tmp_path / "b"):
        assert not list(run.rglob("*timings*"))
    applied = apply_module.apply(
        FIXTURE,
        run_id=timed.artifacts["run_id"],
        state_dir=tmp_path / "b",
        timings=True,
    )
    assert applied.success and "commit" in {
        s["name"] for s in applied.artifacts["timings"]["spans"]
    }


@pytest.mark.integration
def test_a_share_labels_its_counts_as_the_share_s_own(tmp_path: Path) -> None:
    state = tmp_path / "shards"
    planned = plan_module.plan(FIXTURE, state_dir=state, kernel="0.2.0", stage=False)
    part = apply_module.apply(
        FIXTURE, run_id=planned.artifacts["run_id"], state_dir=state, shard=(1, 4)
    )
    assert part.success, part.stderr
    assert "(this share: " in part.stdout
    rows = "; ".join(
        f"{entry['exposeId']}: {entry['rows']} rows" for entry in part.artifacts["emits"]
    )
    assert f"(this share: {rows})" in part.stdout


@pytest.mark.unit
@pytest.mark.parametrize("scale", ["2", "3", "7.5"])
def test_a_scale_above_one_ends_the_last_bin_at_the_end_of_the_range(scale: str) -> None:
    """The range counts its last value, so the last edge, declared at that value, is the new end."""
    sales = scale_emit(_emits(FIXTURE)["store_sales"], Decimal(scale))
    declared = sales.fidelity["columns"]["ss_sold_date_sk"]
    low, high = int(declared["range"]["min"]), int(declared["range"]["max"])
    edges = [int(edge) for edge in declared["bins"]]
    assert edges[0] == low and edges[-1] == high
    assert all(later > earlier for earlier, later in zip(edges, edges[1:], strict=False))
    assert high - low + 1 == int((Decimal(365) * Decimal(scale)).to_integral_value("ROUND_HALF_UP"))


@pytest.mark.integration
@pytest.mark.parametrize("scale", ["2", "3", "7.5"])
def test_a_scaled_run_of_an_init_written_key_still_fits_its_range(
    tmp_path: Path, scale: str
) -> None:
    """``init`` writes an integer key as bins [min, max] over a range; scaled, its rows still fit."""
    small = Path(__file__).resolve().parents[1] / "fixtures" / "small-catalogue"
    contract, profile = small / "catalogue.fluid.yaml", small / "catalogue.profile.json"
    bundle, state = tmp_path / "bundle", tmp_path / "state"

    def fluxsim(*args: str) -> str:
        argv = [sys.executable, "-m", "flux_engine.cli", *args]
        done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stderr or done.stdout
        return done.stdout

    fluxsim("init", str(bundle), "--contract", str(contract), "--profile", str(profile))
    planned = json.loads(
        fluxsim(
            *("-j", "plan", str(bundle), "--state-dir", str(state)),
            *("--profile", str(profile), "--scale", scale),
        )
    )
    run_id = str(planned["artifacts"]["run_id"])
    fluxsim("apply", str(bundle), "--state-dir", str(state), "--run-id", run_id)
    if scale == "2":  # verify refuses groups too small to compare once the rows grow unevenly
        fluxsim(
            *("verify", str(bundle), "--state-dir", str(state), "--run-id", run_id),
            *("--profile", str(profile)),
        )
    rows = int((Decimal(26) * Decimal(scale)).to_integral_value("ROUND_HALF_UP"))
    layout = RunLayout.for_state_dir(state, run_id)
    assert len(layout.emit_path("catalogue").read_text("utf-8").splitlines()) == rows
