# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan`` stages its execution and ``apply --run-id`` commits it, generating nothing."""

from __future__ import annotations

import json
import os
import shutil
from importlib import import_module
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.stages.layout import RunLayout
from flux_engine.stages.staging import STAGE_DIRNAME

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")
pipeline_module = import_module("flux_engine.stages.pipeline")

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"

#: Written at the time of the run, and so not a function of the plan.
_TIMED = {"plan.json", "lineage.ndjson"}


def _bundle(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    return target


def _run_files(run_dir: Path) -> Dict[str, bytes]:
    return {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name not in _TIMED and STAGE_DIRNAME not in path.parts
    }


@pytest.fixture()
def planned(tmp_path: Path):  # type: ignore[no-untyped-def]
    bundle, state = _bundle(tmp_path), tmp_path / "state"
    result = plan_module.plan(bundle, state_dir=state)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["run_id"])
    return bundle, state, layout, result


def test_a_plan_stages_what_apply_commits_byte_for_byte(tmp_path: Path, planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, result = planned
    assert (layout.run_dir / STAGE_DIRNAME / "stage.json").is_file()
    assert not layout.receipt_path.exists()
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert not (layout.run_dir / STAGE_DIRNAME).exists()
    assert "emitted date_dim: 365 rows (generator)" in applied.stdout
    # The run a plan that did not stage and an apply that generated would have written.
    other = tmp_path / "other"
    unstaged = plan_module.plan(bundle, state_dir=other, stage=False)
    generated = apply_module.apply(bundle, run_id=unstaged.artifacts["run_id"], state_dir=other)
    assert generated.success, generated.stderr
    assert _run_files(layout.run_dir) == _run_files(
        RunLayout.for_state_dir(other, unstaged.artifacts["run_id"]).run_dir
    )


def test_apply_generates_no_row_when_it_commits(
    monkeypatch: pytest.MonkeyPatch, planned  # type: ignore[no-untyped-def]
) -> None:
    bundle, state, layout, _ = planned

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("apply generated rows")

    monkeypatch.setattr(pipeline_module, "execute", refuse)
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr


def _stage_file(layout: RunLayout, name: str) -> Path:
    return layout.run_dir / STAGE_DIRNAME / "runs" / layout.run_id / name


def _refused(layout: RunLayout, bundle: Path, state: Path, code: str) -> None:
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert (applied.exit_code, applied.error_code) == (1, code), applied.stderr
    assert not layout.receipt_path.exists()
    assert not layout.emit_path("item").exists()


def test_a_staged_stream_that_was_edited_is_refused_and_nothing_is_written(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    stream = _stage_file(layout, "emits/item.ndjson")
    stream.write_bytes(stream.read_bytes().replace(b"1", b"2", 1))
    _refused(layout, bundle, state, "FLX-2120")


def test_a_staged_file_swapped_for_a_link_is_refused(tmp_path: Path, planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    elsewhere = tmp_path / "elsewhere.ndjson"
    elsewhere.write_bytes(_stage_file(layout, "emits/item.ndjson").read_bytes())
    stream = _stage_file(layout, "emits/item.ndjson")
    stream.unlink()
    os.symlink(elsewhere, stream)
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.exit_code == 1 and applied.error_code in ("FLX-2120", "FLX-2118")
    assert not layout.receipt_path.exists()


def test_a_stage_directory_that_is_a_link_is_refused(tmp_path: Path, planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    stage = layout.run_dir / STAGE_DIRNAME
    moved = tmp_path / "moved-stage"
    stage.rename(moved)
    os.symlink(moved, stage)
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.exit_code == 1 and applied.error_code in ("FLX-2120", "FLX-2118")
    assert not layout.receipt_path.exists()


@pytest.mark.parametrize(
    "path", ["../plan.json", "/etc/hosts", "emits/../../x", ".hidden", "a/b/c"]
)
def test_a_manifest_path_that_leaves_the_run_directory_is_refused(path: str, planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    manifest = layout.run_dir / STAGE_DIRNAME / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["files"][0]["path"] = path
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused(layout, bundle, state, "FLX-2120")


def test_a_stream_that_matches_its_record_but_not_the_receipt_is_refused(planned) -> None:  # type: ignore[no-untyped-def]
    """The manifest digests are not the only check: a stream also has to hash to the receipt's."""
    bundle, state, layout, _ = planned
    manifest = layout.run_dir / STAGE_DIRNAME / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    stream = _stage_file(layout, "emits/item.ndjson")
    forged = stream.read_bytes().replace(b"1", b"2", 1)
    stream.write_bytes(forged)
    for entry in document["files"]:
        if entry["path"] == "emits/item.ndjson":
            from flux_engine.core.digest import digest_of_bytes

            entry["sha256"] = digest_of_bytes(forged, subject="stagedFile").value
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused(layout, bundle, state, "FLX-2120")


def test_a_bundle_that_moved_since_the_plan_is_refused_before_anything_is_written(
    planned,  # type: ignore[no-untyped-def]
) -> None:
    bundle, state, layout, _ = planned
    path = bundle / "simulation.flux.yml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# moved\n", encoding="utf-8")
    _refused(layout, bundle, state, "FLX-2101")
    assert (layout.run_dir / STAGE_DIRNAME).exists()


def test_a_stage_of_another_plan_is_not_used(tmp_path: Path, planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    manifest = layout.run_dir / STAGE_DIRNAME / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["planDigest"] = "sha256:" + "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert "committed_from_stage" not in applied.artifacts
    assert not (layout.run_dir / STAGE_DIRNAME).exists()


def test_a_plan_killed_before_its_record_leaves_a_stage_apply_does_not_use(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    (layout.run_dir / STAGE_DIRNAME / "stage.json").unlink()
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert "committed_from_stage" not in applied.artifacts


def test_a_plan_does_not_stage_into_a_run_that_was_applied(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    assert apply_module.apply(bundle, run_id=layout.run_id, state_dir=state).success
    again = plan_module.plan(bundle, state_dir=state)
    assert again.success and again.artifacts["staged"] is False
    assert not (layout.run_dir / STAGE_DIRNAME).exists()


def test_the_evidence_pack_never_takes_a_stage(tmp_path: Path) -> None:
    from flux_engine.evidence import pack

    assert pack.STAGE_DIRNAME == STAGE_DIRNAME
    run_dir = tmp_path / "run"
    (run_dir / STAGE_DIRNAME).mkdir(parents=True)
    (run_dir / STAGE_DIRNAME / "stage.json").write_text("{}")
    (run_dir / "receipt.json").write_text("{}")
    left_out = pack._skip_verified_files(run_dir, ())(str(run_dir), [STAGE_DIRNAME, "receipt.json"])
    assert left_out == [STAGE_DIRNAME]


def test_no_stage_discards_an_older_stage_of_the_same_plan(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    assert (layout.run_dir / STAGE_DIRNAME / "stage.json").is_file()
    again = plan_module.plan(bundle, state_dir=state, stage=False)
    assert again.success and again.artifacts["run_id"] == layout.run_id
    assert not (layout.run_dir / STAGE_DIRNAME).exists()
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.success and "committed_from_stage" not in applied.artifacts


@pytest.mark.parametrize("planned_with", [(None, None), ("sim-store-star", "eu-west")])
@pytest.mark.parametrize("apply_sim", [None, "sim-store-star"])
@pytest.mark.parametrize("apply_zone", [None, "eu-west"])
def test_a_stage_is_used_only_for_the_simulation_and_zone_it_was_planned_with(
    tmp_path: Path, planned_with: Any, apply_sim: Any, apply_zone: Any
) -> None:
    bundle = _bundle(tmp_path)
    p_sim, p_zone = planned_with

    def applied_run(stage: bool, name: str) -> Any:
        state = tmp_path / name
        made = plan_module.plan(
            bundle, state_dir=state, stage=stage, simulation_id=p_sim, zone=p_zone
        )
        assert made.success, made.stderr
        done = apply_module.apply(
            bundle,
            run_id=made.artifacts["run_id"],
            state_dir=state,
            simulation_id=apply_sim,
            zone=apply_zone,
        )
        assert done.success, done.stderr
        layout = RunLayout.for_state_dir(state, made.artifacts["run_id"])
        return done, _run_files(layout.run_dir)

    staged, with_stage = applied_run(True, "a")
    _, without = applied_run(False, "b")
    assert with_stage == without
    same = (p_sim, p_zone) == (apply_sim, apply_zone)
    assert staged.artifacts.get("committed_from_stage", False) is same


def test_a_manifest_emit_that_is_not_an_entry_is_refused_as_a_stage_error(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    manifest = layout.run_dir / STAGE_DIRNAME / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["emits"][0] = "item"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused(layout, bundle, state, "FLX-2120")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("processes", "x"),
        ("processes", 0),
        ("processes", True),
        ("processes", 1.5),
        ("rows", "12"),
        ("rows", -1),
        ("rows", None),
        ("source", 7),
        ("source", None),
    ],
)
def test_a_manifest_emit_field_the_summary_reads_is_refused_before_anything_is_written(
    planned, field: str, value: Any  # type: ignore[no-untyped-def]
) -> None:
    """``apply`` printed the emits after it committed, so a bad field stopped it too late."""
    bundle, state, layout, _ = planned
    manifest = layout.run_dir / STAGE_DIRNAME / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["emits"][0][field] = value
    manifest.write_text(json.dumps(document), encoding="utf-8")
    _refused(layout, bundle, state, "FLX-2120")


def test_a_refusal_of_an_edited_stage_carries_its_remedy_inline(planned) -> None:  # type: ignore[no-untyped-def]
    bundle, state, layout, _ = planned
    stream = _stage_file(layout, "emits/item.ndjson")
    stream.write_bytes(stream.read_bytes() + b'[1, "x"]\n')
    applied = apply_module.apply(bundle, run_id=layout.run_id, state_dir=state)
    assert applied.error_code == "FLX-2120"
    assert "emits/item.ndjson" in applied.stderr  # the file is named
    assert "delete the run's .stage directory and run `fluxsim apply` again" in applied.stderr
    assert "or run `fluxsim plan` again" in applied.stderr
