# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.replay import replay
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

from .conftest import FIXTURE_BUNDLE, read_json, write_committed_slice

#: One applied run, shared by every test in this module: apply() re-executes
#: the whole 25k-subject simulation, and none of these tests mutate the run
#: directory in a way that would make another test's read of it stale.
#: (Only verify(update_golden=True) writes anything, and it writes under
#: state_dir/golden/, never into the run directory itself.)


@pytest.fixture(scope="module")
def bundle_dir() -> Path:
    assert FIXTURE_BUNDLE.is_dir(), FIXTURE_BUNDLE
    return FIXTURE_BUNDLE


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("verify-ship-replay") / ".flux"


@pytest.fixture(scope="module")
def applied_run_id(bundle_dir: Path, state_dir: Path) -> str:
    result = apply(bundle_dir, state_dir=state_dir)
    assert result.success, result.stderr
    return str(result.artifacts["run_id"])


def test_verify_with_no_out_of_band_golden_yet_is_not_checked(
    bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> None:
    """No <state_dir>/golden/ file yet -- honest NOT_CHECKED, not a silent pass.

    The fixture declares no ``spec.golden.baselineDigest`` at all (it used
    to carry a hand-typed placeholder; no literal can be correct there --
    the bundle's own bytes feed ``inputDigest``, which feeds ``runDigest``),
    so ``specGoldenMatch`` is ``None``: nothing was declared, so nothing
    matched or failed to match. The declared-vs-actual refusal is covered
    in ``tests/stages/test_verify_declared_baseline.py``.
    """
    result = verify(bundle_dir, run_id=applied_run_id, state_dir=state_dir)
    assert result.success, result.stderr
    assert result.artifacts["reproducibility"] == "not-checked"
    assert result.artifacts["golden"]["specGoldenMatch"] is None
    assert result.artifacts["declaredBaselineDrift"] is False


def test_verify_update_golden_then_reproduces_against_the_out_of_band_file(
    bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> None:
    updated = verify(
        bundle_dir,
        run_id=applied_run_id,
        state_dir=state_dir,
        update_golden=True,
        reason="pin the first baseline for this fixture",
    )
    assert updated.success, updated.stderr
    assert updated.artifacts["golden"]["updated"] is True

    again = verify(bundle_dir, run_id=applied_run_id, state_dir=state_dir)
    assert again.artifacts["reproducibility"] == "reproducible"


@pytest.fixture(scope="module")
def mismatched_committed_slice_uri(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A committed slice whose declared ``mean`` is nowhere near this run's
    synthetic mean -- the fixture's ``amount`` property uses an absolute
    tolerance of 50; an offset-0 slice sits thousands away from it."""
    path = tmp_path_factory.mktemp("mismatched-slice") / "slice.ndjson"
    return write_committed_slice(path, amount_offset="0")


def test_verify_update_golden_refuses_when_a_leg_fails(
    bundle_dir: Path, state_dir: Path, applied_run_id: str, mismatched_committed_slice_uri: str
) -> None:
    """M9: ``--update-golden`` must not report success having verified
    nothing but the golden write itself -- a failing fidelity leg refuses
    the update, and the refusal is legible (names the failing leg), not a
    bare non-zero exit.
    """
    result = verify(
        bundle_dir,
        run_id=applied_run_id,
        state_dir=state_dir,
        update_golden=True,
        reason="should be refused",
        committed_slice={"payment_recovery_moment": mismatched_committed_slice_uri},
    )
    assert not result.success
    assert result.artifacts["golden"]["updated"] is False
    assert result.artifacts["fidelity"]["payment_recovery_moment"]["status"] == "failed"
    assert "fidelity check failed" in result.stderr


def test_verify_reports_fidelity_declared_not_checked_with_no_slice(
    bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> None:
    result = verify(bundle_dir, run_id=applied_run_id, state_dir=state_dir)
    assert result.artifacts["fidelity"]["payment_recovery_moment"]["status"] == (
        "declared, not checked"
    )


@pytest.fixture(scope="module")
def committed_slice_uri(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("committed-slice") / "slice.ndjson"
    return write_committed_slice(path, amount_offset="4900")


def test_verify_fidelity_check_runs_given_a_committed_slice(
    bundle_dir: Path, state_dir: Path, applied_run_id: str, committed_slice_uri: str
) -> None:
    result = verify(
        bundle_dir,
        run_id=applied_run_id,
        state_dir=state_dir,
        committed_slice={"payment_recovery_moment": committed_slice_uri},
    )
    outcome = result.artifacts["fidelity"]["payment_recovery_moment"]
    assert outcome["status"] in ("passed", "failed")


def test_ship_withholds_a_declared_emit_with_no_committed_slice(
    bundle_dir: Path, state_dir: Path, applied_run_id: str, tmp_path: Path
) -> None:
    result = ship(bundle_dir, run_id=applied_run_id, out_dir=tmp_path / "dist", state_dir=state_dir)
    assert not result.success
    assert result.artifacts["code"] == "FLX-8001"
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


def test_ship_withheld_event_travels_in_stdout_not_printed_directly(
    bundle_dir: Path,
    state_dir: Path,
    applied_run_id: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """M11: the withheld CloudEvent(s) are data on ``StageResult``, not a
    direct ``print()`` this function makes on its own -- a caller decides
    whether and when they reach a real stream.

    They also do not *replace* the human readout. ``stdout`` in human mode
    is a rendered summary naming the expose and the checks that refused;
    the machine format travels in ``artifacts["events"]``, where a
    ``--json`` caller reads it.
    """
    result = ship(bundle_dir, run_id=applied_run_id, out_dir=tmp_path / "dist", state_dir=state_dir)
    assert not result.success
    captured = capsys.readouterr()
    assert captured.out == ""
    events = result.artifacts["events"]
    assert len(events) == 1
    assert events[0]["type"] == "com.agentics.flux.ship.withheld"
    assert events[0]["source"].startswith("flux://")
    # A person reads the summary; a machine reads `events`.
    assert "withheld: payment_recovery_moment" in result.stdout
    assert "committed-slice.present" in result.stdout
    assert "nothing was written to the output directory" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


@pytest.fixture(scope="module")
def matching_committed_slice_uri(
    tmp_path_factory: pytest.TempPathFactory, bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> str:
    """A committed slice built from the run's own emitted rows.

    Guarantees the fidelity check's declared ``mean`` property evaluates
    ``within`` tolerance regardless of the kernel's own numerics, because
    the "committed" side and the synthetic side are, by construction, the
    same data -- this fixture exists to reach the gate's ``EMIT`` path
    deterministically for a stage-layer test, not to claim anything about
    real-data fidelity.
    """
    layout = RunLayout.for_state_dir(state_dir, applied_run_id)
    attestation = read_json(layout.seam_attestation_path("payment_recovery_moment"))
    columns = attestation["columns"]
    lines = [
        line
        for line in layout.emit_path("payment_recovery_moment")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines]
    path = tmp_path_factory.mktemp("matching-slice") / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"file://{path}"


def test_ship_writes_evidence_md_before_packing_and_captures_pack_stdout(
    bundle_dir: Path,
    state_dir: Path,
    applied_run_id: str,
    tmp_path: Path,
    matching_committed_slice_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """H7 + B3, isolated from the vendored bundler's own schema behaviour.

    ``pack_evidence`` is stubbed so this test exercises exactly what
    :mod:`flux_engine.stages.ship` itself is responsible for: writing
    ``EVIDENCE.md`` into the run directory *before* calling
    ``pack_evidence`` (so it is staged into the archive under ``run/``),
    and capturing whatever the packer prints to stdout into
    ``StageResult.stdout`` rather than letting it reach the real stream.
    The gate itself still runs for real, against a real (if
    self-referential -- see ``matching_committed_slice_uri``) committed
    slice, so this only stubs the one collaborator (the vendored bundler)
    this build's own schema does not yet satisfy.
    """
    import importlib

    from flux_engine.evidence.pack import EvidencePackResult

    # Not `import flux_engine.stages.ship as ship_module`: `flux_engine/
    # stages/__init__.py` does `from flux_engine.stages.ship import ship`,
    # which -- because the imported name and the submodule share one word
    # -- rebinds the `ship` *attribute* on the `flux_engine.stages` package
    # from the submodule to the function. `import a.b.c as x` resolves via
    # that attribute chain (`x = a; x = x.b; x = x.c`), not a `sys.modules`
    # lookup, so it would silently hand this test the function instead of
    # the module. `importlib.import_module` looks the submodule up by its
    # full dotted name in `sys.modules`, sidestepping the shadowing.
    ship_module = importlib.import_module("flux_engine.stages.ship")

    layout = RunLayout.for_state_dir(state_dir, applied_run_id)
    seen_evidence_md_exists = {}

    def _fake_pack_evidence(bundle_dir_arg, run_dir_arg, out_dir_arg, **kwargs):
        seen_evidence_md_exists["value"] = (Path(run_dir_arg) / "EVIDENCE.md").is_file()
        print("[ok] packed fake-archive.flux.tgz")
        return EvidencePackResult(
            ok=True,
            archive=Path(out_dir_arg) / "fake-archive.flux.tgz",
            manifest={},
            build_statement_path=Path(out_dir_arg) / "fake.attestation.json",
            runtime_statement_path=Path(out_dir_arg) / "fake.runtime-evidence.json",
        )

    monkeypatch.setattr(ship_module, "pack_evidence", _fake_pack_evidence)

    result = ship(
        bundle_dir,
        run_id=applied_run_id,
        out_dir=tmp_path / "dist-stubbed",
        state_dir=state_dir,
        committed_slice={"payment_recovery_moment": matching_committed_slice_uri},
    )

    assert result.success, result.stderr
    assert seen_evidence_md_exists.get("value") is True

    captured = capsys.readouterr()
    assert "[ok] packed" not in captured.out
    assert "[ok] packed fake-archive.flux.tgz" in result.stdout

    evidence_path = layout.run_dir / "EVIDENCE.md"
    assert evidence_path.is_file()
    text = evidence_path.read_text(encoding="utf-8")
    assert "## What Was Not Proven" in text
    assert "## Fidelity" in text
    assert "payment_recovery_moment" in text


def test_ship_gate_checks_bind_to_the_recomputed_seam_digests(
    bundle_dir: Path, state_dir: Path, applied_run_id: str, tmp_path: Path, committed_slice_uri: str
) -> None:
    """The gate's binding.H/G/expose checks all pass regardless of the outcome."""
    result = ship(
        bundle_dir,
        run_id=applied_run_id,
        out_dir=tmp_path / "dist",
        state_dir=state_dir,
        committed_slice={"payment_recovery_moment": committed_slice_uri},
    )
    decision = result.artifacts["decisions"]["payment_recovery_moment"]
    binding_checks = [c for c in decision["checks"] if c["check"].startswith("binding.")]
    assert binding_checks
    assert all(c["passed"] for c in binding_checks)


def test_replay_of_an_unmoved_bundle_matches_its_own_run_digest(
    bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> None:
    layout = RunLayout.for_state_dir(state_dir, applied_run_id)
    run_digest_document = read_json(layout.run_digest_path)
    run_digest = digest_of_document(run_digest_document, subject="runDigest").value

    result = replay(
        bundle_dir, run_id=applied_run_id, assert_digest=run_digest, state_dir=state_dir
    )
    assert result.success, result.stderr


def test_replay_refuses_a_wrong_asserted_digest(
    bundle_dir: Path, state_dir: Path, applied_run_id: str
) -> None:
    fake_digest = "sha256:" + "0" * 64
    result = replay(
        bundle_dir, run_id=applied_run_id, assert_digest=fake_digest, state_dir=state_dir
    )
    assert not result.success
    assert result.artifacts["code"] == "FLX-2106"


def test_replay_propagates_the_underlying_failures_error_code(
    bundle_dir: Path, state_dir: Path, tmp_path: Path
) -> None:
    """The envelope offers ``error.code`` so a caller never has to regex stderr.

    ``replay``'s apply-failure branch built its ``StageResult`` without
    ``error_code``, so the one command most likely to be wired into a CI
    gate reported ``null`` for a failure that carried a real ``FLX-nnnn``:
    a caller could not tell "no plan recorded" (FLX-2107) from "the bundle
    moved" (FLX-2101) from an unclassified internal crash. Every other
    stage propagates it; this pins replay to the same contract.
    """
    result = replay(
        bundle_dir,
        run_id="deadbeefdeadbeef",
        assert_digest="sha256:" + "0" * 64,
        state_dir=tmp_path / "empty",
    )
    assert not result.success
    assert result.error_code == "FLX-2107"
    # The code was already in `artifacts`; the envelope reads the other field.
    assert result.artifacts["code"] == "FLX-2107"


def test_replay_error_code_matches_the_apply_it_wraps(
    bundle_dir: Path, state_dir: Path, applied_run_id: str, tmp_path: Path
) -> None:
    """A moved bundle: apply reports FLX-2101, so replay must report FLX-2101."""
    import shutil

    moved = tmp_path / "moved-bundle"
    shutil.copytree(bundle_dir, moved)
    persona = moved / "persona.flux.yml"
    persona.write_text(persona.read_text(encoding="utf-8") + "\n# moved\n", encoding="utf-8")

    result = replay(
        moved,
        run_id=applied_run_id,
        assert_digest="sha256:" + "0" * 64,
        state_dir=state_dir,
    )
    assert not result.success
    assert result.error_code == "FLX-2101"
