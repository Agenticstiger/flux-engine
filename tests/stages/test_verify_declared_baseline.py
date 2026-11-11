# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` is fail-CLOSED on KERNEL-SPEC §10's declared equality.

§10 makes ``golden.baselineDigest === runDigest`` normative. Verify used
to print the mismatch and still exit 0, and the ``--json`` envelope
returned ``"ok": true, "error": null`` with ``specGoldenMismatch: true``
buried three levels down in ``artifacts.golden`` -- so a CI gate or a
downstream consumer following the envelope's own contract saw green
over a violated declaration.

Note what a declared baseline can and cannot be. The bundle's own bytes
feed ``inputDigest``, which feeds ``runDigest``, so writing a run's real
digest into the YAML *moves* that digest: the declaration is a
self-referential fixed point and no literal satisfies it. That is why the
repo's own fixtures declare no baseline at all, and why this module builds
the mismatching bundle it needs rather than shipping one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flux_engine.stages.run import run
from flux_engine.stages.verify import verify

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

_WRONG_BASELINE = "sha256:" + "9f2c1a0b" * 8


@pytest.fixture(scope="module")
def bundle_with_a_wrong_declared_baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bundle = tmp_path_factory.mktemp("declared-baseline") / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert "golden:" not in text, "the fixture must declare no baseline"
    text = text.replace(
        "  fidelity: deterministic\n",
        f'  fidelity: deterministic\n  golden:\n    baselineDigest: "{_WRONG_BASELINE}"\n',
        1,
    )
    simulation.write_text(text, encoding="utf-8")
    return bundle


def test_a_mismatched_declared_baseline_fails_verify(
    bundle_with_a_wrong_declared_baseline: Path, tmp_path: Path
) -> None:
    state = tmp_path / ".flux"
    applied = run(bundle_with_a_wrong_declared_baseline, state_dir=state)
    assert applied.success, applied.stderr
    run_id = applied.artifacts["plan"]["run_id"]

    result = verify(bundle_with_a_wrong_declared_baseline, run_id=run_id, state_dir=state)

    assert result.exit_code == 1
    assert result.success is False
    assert result.artifacts["declaredBaselineDrift"] is True
    assert "declared baseline: does not match" in result.artifacts["headline"]
    # Prose, not an internal JSON key: `specGoldenMismatch` is the name of
    # a field in the envelope, and a human line that quotes it is the
    # machine surface leaking into the one a person reads. The fact it
    # carried -- which digest was declared and which one this run produced
    # -- is still there, and the artifacts still expose the field.
    assert "specGoldenMismatch" not in result.stderr
    assert "declared baseline: does not match" in result.stderr
    assert "spec.golden.baselineDigest" in result.stderr
    assert result.artifacts["golden"]["specGoldenMismatch"] is True


def test_the_drift_can_be_allowed_explicitly(
    bundle_with_a_wrong_declared_baseline: Path, tmp_path: Path
) -> None:
    """A soft mode has to be asked for; it is not the default."""
    state = tmp_path / ".flux"
    applied = run(bundle_with_a_wrong_declared_baseline, state_dir=state)
    assert applied.success, applied.stderr

    result = verify(
        bundle_with_a_wrong_declared_baseline,
        run_id=applied.artifacts["plan"]["run_id"],
        state_dir=state,
        allow_declared_baseline_drift=True,
    )
    assert result.exit_code == 0
    assert result.artifacts["declaredBaselineDrift"] is True
    assert "allowed by --allow-declared-baseline-drift" in result.stderr


def test_a_bundle_declaring_no_baseline_verifies_green(tmp_path: Path) -> None:
    """The positive control: no declaration, no drift, exit 0."""
    state = tmp_path / ".flux"
    applied = run(SMALL_BUNDLE, state_dir=state, verify=True)
    assert applied.success, applied.stderr
    assert applied.artifacts["verify"]["declaredBaselineDrift"] is False


def test_writing_a_runs_own_digest_into_the_bundle_does_not_satisfy_the_declaration(
    tmp_path: Path,
) -> None:
    """The fixed point, pinned: this is why no literal baseline can be right.

    ``inputDigest`` is taken over the bundle's raw file bytes and
    ``runDigest`` is taken over a document carrying it, so editing the
    YAML to hold run N's digest changes the digest run N+1 produces.
    """
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    state = tmp_path / ".flux"

    first = run(bundle, state_dir=state)
    assert first.success, first.stderr
    first_digest = first.artifacts["apply"]["run_digest"]

    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8").replace(
            "  fidelity: deterministic\n",
            f'  fidelity: deterministic\n  golden:\n    baselineDigest: "{first_digest}"\n',
            1,
        ),
        encoding="utf-8",
    )

    second = run(bundle, state_dir=state)
    assert second.success, second.stderr
    assert second.artifacts["apply"]["run_digest"] != first_digest


def test_the_failing_path_carries_the_recovery_hint(
    bundle_with_a_wrong_declared_baseline: Path, tmp_path: Path
) -> None:
    """The hint used to be appended only when the drift was already allowed.

    So the one invocation that actually failed over this -- the first
    contact a reader has with the command the CLI's own epilog puts in
    front of them -- was told a digest did not match another digest, and
    nothing about what to do next.
    """
    state = tmp_path / ".flux"
    applied = run(bundle_with_a_wrong_declared_baseline, state_dir=state)
    assert applied.success, applied.stderr

    result = verify(
        bundle_with_a_wrong_declared_baseline,
        run_id=applied.artifacts["plan"]["run_id"],
        state_dir=state,
    )
    assert result.exit_code == 1
    assert "--update-golden" in result.stderr
    assert "--allow-declared-baseline-drift" in result.stderr


def test_a_golden_regression_names_what_moved_to_a_human(tmp_path: Path) -> None:
    """``movedComponents`` reached ``--json`` callers alone.

    A determinism engine's whole output is "this stopped reproducing, and
    here is what moved". The human readout was the bare headline
    ``reproducible: no`` -- no baseline digest, no run digest, no
    component name -- while the answer sat in
    ``artifacts.golden.movedComponents``.
    """
    state = tmp_path / ".flux"
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)

    recorded = run(
        bundle,
        state_dir=state,
        verify=True,
        update_golden=True,
        reason="recording the first baseline for this fixture",
    )
    assert recorded.success, recorded.stderr

    persona = bundle / "persona.flux.yml"
    persona.write_text(
        persona.read_text(encoding="utf-8").replace("mean: 42.0", "mean: 47.0"), encoding="utf-8"
    )
    regressed = run(bundle, state_dir=state, verify=True)

    assert not regressed.success
    verified = regressed.artifacts["verify"]
    moved = verified["golden"]["movedComponents"]
    assert moved, "the fixture edit has to move at least one component"
    stderr = regressed.stderr
    assert "golden regression" in stderr
    assert verified["golden"]["baselineDigest"] in stderr
    assert verified["golden"]["runDigest"] in stderr
    for component in moved:
        assert component in stderr
