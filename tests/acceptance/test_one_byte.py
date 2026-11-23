# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One-byte-in, one-answer-out: KERNEL-SPEC's central promise, adversarially.

Two mutations of the shrunken telco fixture (``tests/fixtures/telco-fidelity-
small``), against the same golden baseline recorded from the unmutated
bundle:

1. **A trait mean moves one byte** (``persona.flux.yml``'s
   ``monthlySpend.mean: 42.0`` -> ``42.1``) -- semantically load-bearing:
   the generator draws from this distribution, so the run's synthetic
   output moves. ``fluxsim verify`` must exit non-zero and name the first
   component that moved (``flux_engine.core.trace.moved_components``),
   never a bare "digest mismatch".
2. **A metadata annotation moves** (``metadata.owner.team`` -> a fresh
   string -- the schema's own metadata block accepts no free-text
   ``description`` field, so ``owner.team`` is this fixture's annotation
   surface) -- semantically inert to the simulation's *output*: nothing in
   the seed tree, the generator, or any emitted row reads ``metadata``, so
   no entry in ``run-digest.json``'s own ``components`` map moves. See
   ``test_mutating_only_metadata_annotations_moves_no_output_component``'s
   docstring for why the top-level golden-regression digest still does
   move, by KERNEL-SPEC §11 design, and the resolved spec ambiguity that
   follows from it.
3. **A pure YAML comment is inserted** into ``world.flux.yml`` -- the
   strongest form of an inert edit: not even the parsed document changes.
   It too moves ``inputDigest`` (§11 digests raw file bytes) and moves no
   component, and ``fluxsim verify`` must say precisely that rather than
   report a bare mismatch a reader would read as drift in the run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.api.verdict import Reproducibility
from flux_engine.core.trace import moved_components
from flux_engine.stages.apply import apply
from flux_engine.stages.verify import INPUT_ONLY_DETAIL, verify

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


def _load(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: Dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _bundle_copy(root: Path, name: str) -> Path:
    bundle = root / name
    shutil.copytree(SMALL_FIXTURE_BUNDLE, bundle)
    return bundle


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".flux"


@pytest.fixture()
def golden_baseline_run_id(tmp_path: Path, state_dir: Path) -> str:
    """apply() + verify(--update-golden) the *unmutated* bundle: the
    baseline both mutation scenarios compare against.
    """
    baseline_bundle = _bundle_copy(tmp_path, "baseline")
    applied = apply(baseline_bundle, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])

    updated = verify(
        baseline_bundle,
        run_id=run_id,
        state_dir=state_dir,
        update_golden=True,
        reason="one-byte acceptance test baseline",
    )
    assert updated.success, updated.stderr
    assert updated.artifacts["golden"]["updated"] is True
    return run_id


def test_mutating_a_trait_mean_by_one_byte_fails_verify_and_names_the_first_moved_component(
    tmp_path: Path, state_dir: Path, golden_baseline_run_id: str
) -> None:
    mutated_bundle = _bundle_copy(tmp_path, "mutated-trait-mean")
    persona_path = mutated_bundle / "persona.flux.yml"
    persona = _load(persona_path)
    before = persona["spec"]["traits"]["monthlySpend"]["mean"]
    assert before == 42.0, "fixture drifted -- update this test's expected 'before' value"
    persona["spec"]["traits"]["monthlySpend"]["mean"] = 42.1  # one byte: '0' -> '1'
    _dump(persona_path, persona)

    applied = apply(mutated_bundle, state_dir=state_dir)
    assert applied.success, applied.stderr
    mutated_run_id = str(applied.artifacts["run_id"])
    assert mutated_run_id != golden_baseline_run_id, (
        "the mutated bundle must produce a distinct inputDigest/run_id, "
        "otherwise this test is not exercising a moved run at all"
    )

    result = verify(mutated_bundle, run_id=mutated_run_id, state_dir=state_dir)

    assert result.exit_code != 0
    assert not result.success
    golden = result.artifacts["golden"]
    assert golden["reproducibility"] == Reproducibility.NOT_REPRODUCIBLE
    assert golden["movedComponents"], "verify must name at least one moved component"

    # Cross-check against moved_components() directly, over the same two
    # component maps verify() itself compared -- proves "names the first
    # moved component" against the spec's own function, not merely against
    # whatever verify() happened to report.
    simulation_id = applied.artifacts["receipt"]["simulationId"]
    baseline_components = json.loads((state_dir / "golden" / f"{simulation_id}.json").read_text())[
        "components"
    ]
    mutated_components = json.loads(
        (state_dir / "runs" / mutated_run_id / "run-digest.json").read_text()
    )["components"]
    expected_moved = moved_components(baseline_components, mutated_components)
    assert (
        expected_moved
    ), "the fixture change did not move any component -- test is not testing anything"
    assert golden["movedComponents"][0] == expected_moved[0]
    # The negative control for the two inert-edit tests below: a
    # load-bearing edit is never reported as "outputs identical".
    assert golden["outputsIdentical"] is False
    assert golden["detail"] is None


def test_mutating_only_metadata_annotations_moves_no_output_component(
    tmp_path: Path, state_dir: Path, golden_baseline_run_id: str
) -> None:
    """The narrower, honest claim this engine actually makes -- see this
    module's ``SPEC AMBIGUITY`` note below for the build-item wording this
    test had to depart from and why.

    KERNEL-SPEC §11 defines ``inputDigest`` as a digest over the *raw
    bytes* of every bundle document, deliberately with no exemption for
    metadata -- so a metadata-only edit still moves ``inputDigest``, and
    because ``inputDigest`` is itself one of ``run-digest.json``'s own
    fields, it still moves the whole-document ``runDigest`` the golden
    regression compares. A metadata-only edit therefore does *not* leave
    ``fluxsim verify`` reporting ``reproducible`` against a prior baseline --
    confirmed as the FAIL half of this test, immediately below.

    What a metadata-only edit *does* leave alone is every component in
    ``run-digest.json``'s own ``components`` map (population, events,
    decisions, emits, ...) -- nothing in the seed tree, the generator, or
    any emitted row reads ``metadata``. ``moved_components()`` reports
    none of them moved, which is the real, checkable form of "this change
    was inert to the simulation's output" KERNEL-SPEC's design supports.

    SPEC AMBIGUITY (resolved): the build item for this test asked for
    "mutate metadata.annotations/description -> verify stays green" (a
    top-level ``fluxsim verify`` exit-0, ``reproducible`` claim). Given
    KERNEL-SPEC §11's ``inputDigest`` construction above, that claim is
    false for *any* golden-regression check against a prior baseline, by
    design, not by a bug this build introduced -- weakening this test to
    assert the wrong thing (or picking a mutation the schema silently
    tolerates without moving ``inputDigest`` -- no such field exists in
    this fixture's schema) would misreport the engine's real behaviour.
    Resolved by testing the narrower, true claim (no *component* moved)
    and asserting the digest-level mismatch as an explicit, named fact
    rather than eliding it.
    """
    mutated_bundle = _bundle_copy(tmp_path, "mutated-metadata-only")
    persona_path = mutated_bundle / "persona.flux.yml"
    persona = _load(persona_path)
    persona["metadata"]["owner"]["team"] = "a-completely-different-team-name"
    _dump(persona_path, persona)

    applied = apply(mutated_bundle, state_dir=state_dir)
    assert applied.success, applied.stderr
    mutated_run_id = str(applied.artifacts["run_id"])
    assert mutated_run_id != golden_baseline_run_id, (
        "a metadata edit still moves inputDigest (KERNEL-SPEC §11), so it must "
        "still land in a distinct run directory -- see this test's docstring"
    )

    result = verify(mutated_bundle, run_id=mutated_run_id, state_dir=state_dir)

    # The true, documented claim: no output component moved.
    assert result.artifacts["golden"]["movedComponents"] == []

    # The explicit, named consequence of KERNEL-SPEC §11's inputDigest
    # construction: the top-level golden regression still reports a moved
    # digest (never silently "reproducible") -- this is not this test
    # weakening its own claim, it is the claim this test actually makes.
    assert result.artifacts["golden"]["reproducibility"] == Reproducibility.NOT_REPRODUCIBLE
    assert not result.success
    assert result.exit_code == 1

    # ...and it is reported as what it is: a moved input binding over an
    # unmoved run, never an unexplained mismatch.
    assert result.artifacts["golden"]["outputsIdentical"] is True
    assert result.artifacts["golden"]["detail"] == INPUT_ONLY_DETAIL
    assert "every component identical" in result.artifacts["headline"]
    assert INPUT_ONLY_DETAIL in result.stderr


def test_inserting_a_yaml_comment_moves_the_run_digest_and_no_component(
    tmp_path: Path, state_dir: Path, golden_baseline_run_id: str
) -> None:
    """A comment is the strongest inert edit there is: the parsed document
    is byte-identical, only the file is not.

    KERNEL-SPEC §11 builds ``inputDigest`` from ``{path: raw-bytes digest}``
    over every bundle document, so the comment moves ``inputDigest`` and,
    through it (§10's run digest document carries ``inputDigest`` as a
    field), the top-level ``runDigest``. The public FLUX documentation
    states the same intent from the other side: the verify gate "fails the
    build on a single byte of drift" (``docs/concepts/determinism-
    governance.md`` in the FLUX spec repo). This test therefore pins both
    halves of the honest claim:

    * the run digest **does** move -- there is no comment exemption, and
      this build does not invent one;
    * **no component** of the run moves, and ``fluxsim verify`` says so in
      its headline, its stderr and its artifacts, so a reader is never
      left to infer drift in the simulation from a digest that moved for
      a comment.

    See this stage's REQUEST if literal comment-invariance of ``runDigest``
    is ever wanted: it is a KERNEL-SPEC §11 change plus a
    ``kernelVersion`` bump, not an engine bug.
    """
    mutated_bundle = _bundle_copy(tmp_path, "mutated-comment-only")
    world_path = mutated_bundle / "world.flux.yml"
    original_bytes = world_path.read_bytes()
    comment = b"# a comment no part of the simulation reads\n"
    world_path.write_bytes(comment + original_bytes)

    # Prove the mutation is a pure comment: the parsed documents agree.
    assert yaml.safe_load(world_path.read_text(encoding="utf-8")) == yaml.safe_load(
        original_bytes.decode("utf-8")
    )
    assert world_path.read_bytes() != original_bytes

    applied = apply(mutated_bundle, state_dir=state_dir)
    assert applied.success, applied.stderr
    mutated_run_id = str(applied.artifacts["run_id"])
    assert mutated_run_id != golden_baseline_run_id

    result = verify(mutated_bundle, run_id=mutated_run_id, state_dir=state_dir)
    golden = result.artifacts["golden"]

    assert golden["reproducibility"] == Reproducibility.NOT_REPRODUCIBLE
    assert golden["runDigest"] != golden["baselineDigest"]
    assert golden["movedComponents"] == []
    assert golden["outputsIdentical"] is True
    assert golden["detail"] == INPUT_ONLY_DETAIL
    assert "every component identical" in result.artifacts["headline"]
    assert result.exit_code == 1

    # The component maps themselves, read off disk, not merely verify()'s
    # report of them: byte-identical between baseline and comment run.
    simulation_id = applied.artifacts["receipt"]["simulationId"]
    baseline_components = json.loads((state_dir / "golden" / f"{simulation_id}.json").read_text())[
        "components"
    ]
    mutated_components = json.loads(
        (state_dir / "runs" / mutated_run_id / "run-digest.json").read_text()
    )["components"]
    assert mutated_components == baseline_components
    assert moved_components(baseline_components, mutated_components) == ()
