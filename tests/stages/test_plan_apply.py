# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.core import seedtree as st_mod
from flux_engine.core.errors import FluxError
from flux_engine.stages.apply import apply
from flux_engine.stages.errors import FLX_2101
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan, planned_seed_tree, read_seed_paths

from .conftest import FIXTURE_BUNDLE, read_json

#: KERNEL-SPEC §3 moved the path list out of the plan so the plan stays small
#: enough to read and to pin as a golden. The fixture bundle materialises six
#: figures' worth of paths; a plan that inlined them was 16 MB.
PLAN_SIZE_CEILING_BYTES = 200 * 1024


def test_plan_is_deterministic_across_two_calls(bundle_dir: Path, state_dir: Path) -> None:
    first = plan(bundle_dir, state_dir=state_dir)
    second = plan(bundle_dir, state_dir=state_dir)
    assert first.success and second.success
    assert first.artifacts["plan_digest"] == second.artifacts["plan_digest"]
    assert first.artifacts["run_id"] == second.artifacts["run_id"]


def test_plan_writes_planjson_under_the_state_dir(bundle_dir: Path, state_dir: Path) -> None:
    result = plan(bundle_dir, state_dir=state_dir)
    plan_path = Path(result.artifacts["plan_path"])
    assert plan_path.is_file()
    document = read_json(plan_path)
    assert document["planDigest"] == result.artifacts["plan_digest"]
    assert document["emits"]["payment_recovery_moment"]["declaresFidelity"] is True


def test_apply_without_a_run_id_plans_first(bundle_dir: Path, state_dir: Path) -> None:
    result = apply(bundle_dir, state_dir=state_dir)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state_dir, result.artifacts["run_id"])
    assert layout.receipt_path.is_file()
    assert layout.run_digest_path.is_file()
    assert layout.population_path.is_file()
    assert layout.events_path.is_file()
    assert layout.decisions_path.is_file()
    assert layout.lineage_path.is_file()


def test_apply_writes_a_row_per_declared_row_for_a_fidelity_declaring_emit(
    bundle_dir: Path, state_dir: Path
) -> None:
    result = apply(bundle_dir, state_dir=state_dir)
    receipt = result.artifacts["receipt"]
    assert receipt["emits"]["payment_recovery_moment"]["rows"] == 64
    emit_path = RunLayout.for_state_dir(state_dir, result.artifacts["run_id"]).emit_path(
        "payment_recovery_moment"
    )
    lines = [line for line in emit_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 64


def test_apply_refuses_when_the_bundle_moved_since_the_plan(
    bundle_dir: Path, state_dir: Path, tmp_path: Path
) -> None:
    import shutil

    working_copy = tmp_path / "bundle"
    shutil.copytree(bundle_dir, working_copy)
    planned = plan(working_copy, state_dir=state_dir)
    assert planned.success

    # Move the bundle by editing a FLUX document after the plan was taken.
    world_path = working_copy / "world.flux.yml"
    world_path.write_text(world_path.read_text(encoding="utf-8") + "\n# moved\n", encoding="utf-8")

    applied = apply(
        working_copy, run_id=planned.artifacts["run_id"], state_dir=state_dir, auto_plan=False
    )
    assert not applied.success
    assert applied.artifacts["code"] == FLX_2101.code


def test_apply_refuses_when_planjson_was_hand_edited(bundle_dir: Path, state_dir: Path) -> None:
    planned = plan(bundle_dir, state_dir=state_dir)
    layout = RunLayout.for_state_dir(state_dir, planned.artifacts["run_id"])
    document = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    document["seed"] = document["seed"] + 1
    layout.plan_path.write_text(json.dumps(document), encoding="utf-8")

    applied = apply(
        bundle_dir, run_id=planned.artifacts["run_id"], state_dir=state_dir, auto_plan=False
    )
    assert not applied.success
    assert applied.artifacts["code"] == "FLX-2102"


def test_apply_with_no_recorded_plan_raises_flx_2107_not_flx_2101(
    bundle_dir: Path, state_dir: Path
) -> None:
    """H4: no ``plan.json`` at this run id is a distinct failure from a moved
    ``inputDigest`` -- the two used to share ``FLX-2101``'s title, which
    named the wrong failure for this case.
    """
    applied = apply(bundle_dir, run_id="0123456789abcdef", state_dir=state_dir, auto_plan=False)
    assert not applied.success
    assert applied.artifacts["code"] == "FLX-2107"
    assert applied.artifacts["code"] != FLX_2101.code


def test_two_applies_of_one_plan_produce_the_same_run_digest(
    bundle_dir: Path, state_dir: Path
) -> None:
    first = apply(bundle_dir, state_dir=state_dir)
    second = apply(bundle_dir, run_id=first.artifacts["run_id"], state_dir=state_dir)
    assert first.success and second.success
    assert first.artifacts["run_digest"] == second.artifacts["run_digest"]


# ---------------------------------------------------------------------------
# KERNEL-SPEC §3: the path list lives beside the plan, not in it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def planned_run(tmp_path_factory: pytest.TempPathFactory) -> Tuple[RunLayout, Dict[str, Any]]:
    """One plan of the fixture bundle, shared by the KERNEL-SPEC §3 tests below.

    Planning this bundle executes the whole loop over its population, and every
    assertion below is about the artifacts *one* plan writes -- so they take one
    plan between them rather than one each. The two tests that damage the run
    directory work on :func:`copied_run` instead, so the shared one stays intact
    whatever order the module runs in.
    """
    state = tmp_path_factory.mktemp("seed-paths") / ".flux"
    result = plan(FIXTURE_BUNDLE, state_dir=state)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["run_id"])
    return layout, read_json(layout.plan_path)


def copied_run(
    planned_run: Tuple[RunLayout, Dict[str, Any]], tmp_path: Path
) -> Tuple[RunLayout, Dict[str, Any]]:
    """A private copy of the shared planned run, safe to damage."""
    import shutil

    layout, document = planned_run
    state = tmp_path / ".flux"
    shutil.copytree(layout.state_dir, state)
    return RunLayout.for_state_dir(state, layout.run_id), document


def test_the_plan_carries_a_count_table_and_no_path_list(
    planned_run: Tuple[RunLayout, Dict[str, Any]],
) -> None:
    layout, document = planned_run

    assert "seedTreePaths" not in document
    assert layout.plan_path.stat().st_size < PLAN_SIZE_CEILING_BYTES
    shapes = document["seedTreeShapes"]
    assert shapes and list(shapes) == sorted(shapes)
    assert set(shapes) <= {shape.name for shape in st_mod.CANONICAL_PATH_SHAPES}
    # The table summarises the file beside it, so the two must agree.
    paths = read_seed_paths(layout, document)
    assert sum(shapes.values()) == len(paths)
    assert shapes == st_mod.shape_counts(paths)


def test_the_seed_paths_file_is_sorted_and_hashes_to_the_plans_digest(
    planned_run: Tuple[RunLayout, Dict[str, Any]],
) -> None:
    """§3: "one canonical path per line; raw-bytes digest = seedTreeDigest"."""
    import hashlib

    layout, document = planned_run
    data = layout.seed_paths_path.read_bytes()

    lines = data.decode("utf-8").splitlines()
    assert lines == sorted(lines)
    assert len(set(lines)) == len(lines)
    assert document["seedTreeDigest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert len(st_mod.parse_seed_paths(data)) == len(lines)


def test_re_planning_rewrites_the_same_path_file_byte_for_byte(
    planned_run: Tuple[RunLayout, Dict[str, Any]],
) -> None:
    """The file is rewritten whole on every plan, so a killed write repairs itself."""
    layout, _ = planned_run
    before = layout.seed_paths_path.read_bytes()
    layout.seed_paths_path.write_bytes(before[: len(before) // 2])

    again = plan(FIXTURE_BUNDLE, state_dir=layout.state_dir)
    assert again.success, again.stderr
    assert again.artifacts["run_id"] == layout.run_id
    assert layout.seed_paths_path.read_bytes() == before


def test_a_run_touching_an_unanticipated_path_stops_with_flx_2201(
    planned_run: Tuple[RunLayout, Dict[str, Any]],
) -> None:
    """The FLX-2201 half of the plan binding, now read from the file beside the plan."""
    layout, document = planned_run
    root = st_mod.derive_root(bytes(range(32)), document["seed"])
    tree = planned_seed_tree(root, layout, document)
    planned = read_seed_paths(layout, document)

    assert tree.stream(planned[0]) is not None
    unanticipated = st_mod.market_path("no-such-world", 10**6)
    assert unanticipated not in planned
    with pytest.raises(FluxError) as caught:
        tree.stream(unanticipated)
    assert caught.value.code.code == "FLX-2201"


def test_a_seed_path_file_that_moved_since_the_plan_is_refused(
    planned_run: Tuple[RunLayout, Dict[str, Any]], tmp_path: Path
) -> None:
    """Editing the path list would widen the set FLX-2201 checks against."""
    layout, document = copied_run(planned_run, tmp_path)
    layout.seed_paths_path.write_bytes(layout.seed_paths_path.read_bytes() + b'["sweep",0]\n')

    with pytest.raises(FluxError) as caught:
        read_seed_paths(layout, document)
    assert caught.value.code.code == "FLX-2108"


def test_a_missing_seed_path_file_is_refused(
    planned_run: Tuple[RunLayout, Dict[str, Any]], tmp_path: Path
) -> None:
    layout, document = copied_run(planned_run, tmp_path)
    layout.seed_paths_path.unlink()

    with pytest.raises(FluxError) as caught:
        read_seed_paths(layout, document)
    assert caught.value.code.code == "FLX-2108"
