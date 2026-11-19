# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""No stage writes through a link planted under the state directory.

Every file a stage writes is written under a name the engine owns. These tests
plant a link at each of those names, pointing at a file outside the state
directory, run the stage, and hold two things: the file outside is unchanged,
byte for byte, and the stage either replaced the link with the file it wrote or
refused (``FLX-2117`` for a file it appends to, ``FLX-2118`` for a directory
that is a link or a run directory that holds one).

Two passes over ``apply`` give both halves. With the directory scan in
``RunLayout.ensure`` in place, a run directory that already holds a link is
refused before anything is written. With that scan switched off, every write
site is on its own, and each one must replace the link rather than follow it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from flux_engine.starter import scaffold

from .conftest import FIXTURE_BUNDLE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links"),
]

REASON = "baseline recorded for the no-follow tests"


@pytest.fixture(scope="module")
def starter(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("no-follow") / "bundle"
    scaffold(target)
    return target


def _state(bundle: Path, root: Path) -> RunLayout:
    result = run(bundle, state_dir=root / "state")
    assert result.exit_code == 0, result.stderr
    return RunLayout.for_state_dir(root / "state", result.artifacts["plan"]["run_id"])


@pytest.fixture(scope="module")
def applied_starter(starter: Path, tmp_path_factory: pytest.TempPathFactory) -> RunLayout:
    return _state(starter, tmp_path_factory.mktemp("applied-starter"))


@pytest.fixture(scope="module")
def applied_fidelity(tmp_path_factory: pytest.TempPathFactory) -> RunLayout:
    return _state(FIXTURE_BUNDLE, tmp_path_factory.mktemp("applied-fidelity"))


def _copy(layout: RunLayout, destination: Path) -> RunLayout:
    shutil.copytree(layout.state_dir, destination / "state")
    return RunLayout.for_state_dir(destination / "state", layout.run_id)


def _plant(link: Path, outside: Path) -> Path:
    """Replace *link* with a link to a new file under *outside*; returns that file."""
    victim = outside / f"victim-{link.name}"
    victim.write_bytes(b"original bytes of " + link.name.encode())
    link.unlink()
    link.symlink_to(victim)
    return victim


def _plant_all(paths: List[Path], outside: Path) -> Dict[Path, Path]:
    outside.mkdir(exist_ok=True)
    return {path: _plant(path, outside) for path in paths}


def _unchanged(planted: Dict[Path, Path]) -> None:
    for link, victim in planted.items():
        assert victim.read_bytes() == b"original bytes of " + link.name.encode(), link


def _apply_written(layout: RunLayout) -> List[Path]:
    """Every file ``apply`` writes for the run, as they stand after a run."""
    run_dir = layout.run_dir
    named = [
        layout.population_path,
        layout.events_path,
        layout.suppressions_path,
        layout.decisions_path,
        layout.decisions_meta_path,
        layout.lineage_path,
        layout.column_provenance_path,
        layout.run_digest_path,
        layout.receipt_path,
        *sorted(run_dir.glob("emits/*.ndjson")),
        *sorted(run_dir.glob("seam-attestation-*.json")),
        *sorted(run_dir.glob("fidelity-input-*.json")),
    ]
    assert all(path.is_file() for path in named), [p for p in named if not p.is_file()]
    return named


@pytest.fixture()
def guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off the scan for links in the run directory, so the writers stand alone."""
    monkeypatch.setattr("flux_engine.stages.layout.refuse_links_within", lambda directory: None)


# --- apply: the directory scan -----------------------------------------------


def test_apply_refuses_a_run_directory_that_holds_a_link_and_writes_nothing(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    planted = _plant_all(_apply_written(layout), tmp_path / "outside")

    with pytest.raises(FluxError) as refused:
        apply(starter, run_id=layout.run_id, state_dir=layout.state_dir)

    assert refused.value.code.code == "FLX-2118"
    _unchanged(planted)
    assert all(link.is_symlink() for link in planted)


# --- apply: each write site, on its own --------------------------------------


def test_apply_replaces_a_link_at_every_name_it_writes(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, guard_off: None
) -> None:
    layout = _copy(applied_starter, tmp_path)
    written = _apply_written(layout)
    planted = _plant_all(written, tmp_path / "outside")

    again = apply(starter, run_id=layout.run_id, state_dir=layout.state_dir)

    assert again.exit_code == 0, again.stderr
    _unchanged(planted)
    assert not [link for link in planted if link.is_symlink()]
    assert all(path.is_file() for path in written)


def test_apply_replaces_a_link_at_the_fidelity_input(
    applied_fidelity: RunLayout, tmp_path: Path, guard_off: None
) -> None:
    layout = _copy(applied_fidelity, tmp_path)
    (fidelity_input,) = layout.run_dir.glob("fidelity-input-*.json")
    planted = _plant_all([fidelity_input], tmp_path / "outside")

    again = apply(FIXTURE_BUNDLE, run_id=layout.run_id, state_dir=layout.state_dir)

    assert again.exit_code == 0, again.stderr
    _unchanged(planted)
    assert fidelity_input.is_file() and not fidelity_input.is_symlink()


def test_apply_replaces_a_link_at_the_parameter_register(tmp_path: Path, guard_off: None) -> None:
    bundle = tmp_path / "bundle"
    scaffold(bundle)
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8")
        + "extensions:\n  com.agentics.flux/1:\n    parameters:\n"
        "      - document: persona-pragmatic-family\n"
        "        pointer: /spec/traits\n"
        "        source: assumption\n"
        "        reason: The growth team's persona workshop.\n",
        encoding="utf-8",
    )
    layout = _state(bundle, tmp_path / "run")
    assert layout.parameter_register_path.is_file()
    planted = _plant_all([layout.parameter_register_path], tmp_path / "outside")

    again = apply(bundle, run_id=layout.run_id, state_dir=layout.state_dir)

    assert again.exit_code == 0, again.stderr
    _unchanged(planted)
    assert not layout.parameter_register_path.is_symlink()


# --- plan --------------------------------------------------------------------


def test_plan_replaces_a_link_at_the_plan_and_the_seed_paths(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, guard_off: None
) -> None:
    layout = _copy(applied_starter, tmp_path)
    planted = _plant_all([layout.plan_path, layout.seed_paths_path], tmp_path / "outside")

    again = plan(starter, state_dir=layout.state_dir)

    assert again.exit_code == 0, again.stderr
    _unchanged(planted)
    assert not layout.plan_path.is_symlink() and not layout.seed_paths_path.is_symlink()


def test_plan_refuses_a_run_directory_that_holds_a_link(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    planted = _plant_all([layout.plan_path], tmp_path / "outside")
    with pytest.raises(FluxError) as refused:
        plan(starter, state_dir=layout.state_dir)
    assert refused.value.code.code == "FLX-2118"
    _unchanged(planted)


# --- ship and verify ---------------------------------------------------------


def test_ship_replaces_a_link_at_the_evidence_report(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    report = layout.run_dir / "EVIDENCE.md"
    report.write_text("stale", encoding="utf-8")
    planted = _plant_all([report], tmp_path / "outside")

    shipped = ship(
        starter, run_id=layout.run_id, out_dir=tmp_path / "out", state_dir=layout.state_dir
    )

    assert shipped.exit_code == 0, shipped.stderr
    _unchanged(planted)
    assert report.is_file() and not report.is_symlink()
    assert "stale" not in report.read_text(encoding="utf-8")


def test_verify_replaces_a_link_at_the_golden_record_and_changelog(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    first = verify(
        starter,
        run_id=layout.run_id,
        state_dir=layout.state_dir,
        update_golden=True,
        reason=REASON,
    )
    assert first.exit_code == 0, first.stderr
    golden = sorted(layout.golden_dir.iterdir())
    assert len(golden) == 2

    planted = _plant_all(golden, tmp_path / "outside")
    with pytest.raises(FluxError) as refused:
        verify(
            starter,
            run_id=layout.run_id,
            state_dir=layout.state_dir,
            update_golden=True,
            reason=REASON,
        )
    assert refused.value.code.code == "FLX-2118"
    _unchanged(planted)


def test_verify_replaces_a_link_at_the_golden_files_when_the_scan_is_off(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _copy(applied_starter, tmp_path)
    assert verify(
        starter, run_id=layout.run_id, state_dir=layout.state_dir, update_golden=True, reason=REASON
    ).success
    golden = sorted(layout.golden_dir.iterdir())
    planted = _plant_all(golden, tmp_path / "outside")
    # A baseline that exists is read before it is rewritten, through a link it
    # is not: the read finds no baseline and the write replaces the link.
    monkeypatch.setattr("flux_engine.stages.layout.refuse_links_within", lambda directory: None)

    again = verify(
        starter, run_id=layout.run_id, state_dir=layout.state_dir, update_golden=True, reason=REASON
    )

    assert again.exit_code == 0, again.stderr
    _unchanged(planted)
    assert not [path for path in golden if path.is_symlink()]


@pytest.mark.parametrize(
    "name",
    ["population.ndjson", "decisions.ndjson", "emits/payment_recovery_moment.ndjson"],
)
def test_verify_does_not_read_a_ledger_through_a_link(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, name: str
) -> None:
    """A link to a copy with the very same bytes still is not the file the run wrote."""
    layout = _copy(applied_starter, tmp_path)
    ledger = layout.run_dir / name
    copy = tmp_path / "outside-copy.ndjson"
    original = ledger.read_bytes()
    copy.write_bytes(original)
    ledger.unlink()
    ledger.symlink_to(copy)

    result = verify(starter, run_id=layout.run_id, state_dir=layout.state_dir)

    assert result.exit_code != 0
    assert copy.read_bytes() == original


# --- the directories themselves ----------------------------------------------


def _link_directory(directory: Path, tmp_path: Path) -> None:
    target = tmp_path / f"elsewhere-{directory.name}"
    if directory.exists():
        shutil.move(str(directory), str(target))
    else:
        target.mkdir()
    directory.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("which", ["runs", "run", "emits"])
def test_ensure_refuses_a_directory_that_is_a_link(
    applied_starter: RunLayout, tmp_path: Path, which: str
) -> None:
    layout = _copy(applied_starter, tmp_path)
    directory = {"runs": layout.runs_dir, "run": layout.run_dir, "emits": layout.emits_dir}[which]
    _link_directory(directory, tmp_path)
    before = sorted(p for p in tmp_path.rglob("*") if not p.is_symlink())

    with pytest.raises(FluxError) as refused:
        layout.ensure()

    assert refused.value.code.code in {"FLX-2118", "FLX-2112"}
    assert sorted(p for p in tmp_path.rglob("*") if not p.is_symlink()) == before


def test_ensure_refuses_a_run_directory_that_is_a_link_to_a_sibling_run(
    applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    other = layout.runs_dir / ("0" * 16)
    shutil.move(str(layout.run_dir), str(other))
    layout.run_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises(FluxError) as refused:
        layout.ensure()
    assert refused.value.code.code == "FLX-2118"


def test_ensure_refuses_a_link_nested_in_the_emits_directory(
    applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    (layout.emits_dir / "nested").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FluxError) as refused:
        layout.ensure()
    assert refused.value.code.code == "FLX-2118"


def test_the_golden_directory_that_is_a_link_is_refused(
    applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    _link_directory(layout.golden_dir, tmp_path)
    with pytest.raises(FluxError) as refused:
        layout.ensure_golden_dir()
    assert refused.value.code.code == "FLX-2118"
    assert not list((tmp_path / "elsewhere-golden").iterdir())


def test_verify_update_golden_refuses_a_golden_directory_that_is_a_link(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    _link_directory(layout.golden_dir, tmp_path)
    with pytest.raises(FluxError) as refused:
        verify(
            starter,
            run_id=layout.run_id,
            state_dir=layout.state_dir,
            update_golden=True,
            reason=REASON,
        )
    assert refused.value.code.code == "FLX-2118"
    assert not list((tmp_path / "elsewhere-golden").iterdir())


@pytest.mark.parametrize("stage", ["ship", "verify"])
def test_the_stages_that_do_not_create_the_run_directory_refuse_a_linked_one(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, stage: str
) -> None:
    layout = _copy(applied_starter, tmp_path)
    _link_directory(layout.emits_dir, tmp_path)
    call: Callable[[], object] = (
        (lambda: verify(starter, run_id=layout.run_id, state_dir=layout.state_dir))
        if stage == "verify"
        else (
            lambda: ship(
                starter, run_id=layout.run_id, out_dir=tmp_path / "out", state_dir=layout.state_dir
            )
        )
    )
    with pytest.raises(FluxError) as refused:
        call()
    assert refused.value.code.code == "FLX-2118"


# --- the directory checks that read, deliver and apply make ---------------------


def _refused_with(call: Callable[[], object]) -> str:
    """The FLX code a stage call ends in, whether it raises or returns a failed result."""
    try:
        result = call()
    except FluxError as error:
        return error.code.code
    return str(getattr(result, "error_code", None))


@pytest.mark.parametrize("stage", ["verify-golden-read", "deliver", "apply"])
def test_a_stage_that_only_reads_or_delivers_refuses_a_linked_directory(
    starter: Path, applied_starter: RunLayout, tmp_path: Path, stage: str
) -> None:
    from types import SimpleNamespace

    from flux_engine.stages.run import _deliver_step

    layout = _copy(applied_starter, tmp_path)
    if stage == "verify-golden-read":
        _link_directory(layout.golden_dir, tmp_path)
        code = _refused_with(
            lambda: verify(starter, run_id=layout.run_id, state_dir=layout.state_dir)
        )
    elif stage == "deliver":
        _link_directory(layout.emits_dir, tmp_path)
        code = _refused_with(
            lambda: _deliver_step(
                SimpleNamespace(name="memory"),
                SimpleNamespace(artifacts={}),
                layout.run_id,
                layout.state_dir,
                {},
                [],
                ship=False,
            )
        )
    else:
        _link_directory(layout.emits_dir, tmp_path)
        code = _refused_with(
            lambda: apply(starter, run_id=layout.run_id, state_dir=layout.state_dir)
        )
    assert code == "FLX-2118"


def test_ship_refuses_a_linked_output_directory_before_writing_anything(
    starter: Path, applied_starter: RunLayout, tmp_path: Path
) -> None:
    layout = _copy(applied_starter, tmp_path)
    before = sorted(p.name for p in layout.run_dir.iterdir())
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "out").symlink_to(real, target_is_directory=True)

    with pytest.raises(FluxError) as refused:
        ship(starter, run_id=layout.run_id, out_dir=tmp_path / "out", state_dir=layout.state_dir)

    assert refused.value.code.code == "FLX-2118"
    assert sorted(p.name for p in layout.run_dir.iterdir()) == before
    assert not list(real.iterdir())
