# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Replay determinism: the acceptance-suite's central promise.

Three independent checks:

1. **Cross-process, cross-environment determinism.** The whole plan+apply
   loop, run twice as *separate* ``python -m flux_engine.cli`` subprocesses
   -- different ``PYTHONHASHSEED`` (so a dict-iteration bug or an
   unsorted-set bug can't hide behind one hash seed), a different ``TZ``
   and ``LC_ALL`` (so a stray ``datetime.now()`` or locale-sensitive
   formatting can't hide behind the CI box's own locale), and a different
   cwd -- must produce the byte-identical ``runDigest``.
2. **What an inert edit does and does not move, across processes.** The
   same loop over a bundle whose only change is an inserted YAML comment,
   in a third environment again: the ``runDigest`` moves (KERNEL-SPEC §11
   digests raw file bytes -- there is no comment exemption, and this build
   does not invent one), while every entry of ``run-digest.json``'s
   ``components`` map stays byte-identical. That pair is the honest form
   of "a comment changes nothing that matters".
3. **Seed fuzz.** 50 distinct seeds on a shrunken (population 500) copy of
   the telco fixture: every one of them replays (``fluxsim replay
   --assert-digest`` against its own first digest succeeds), and no two
   distinct seeds collide on the same digest.

Uses ``tests/fixtures/telco-fidelity-small`` (a population-500 copy of
``tests/fixtures/telco-fidelity``, patched only in ``world.flux.yml``'s
``spec.population.size`` -- see that fixture directory) rather than the
25k-subject original: the full population's ~90s-per-``apply()`` cost
would make a 50-seed fuzz loop impractical, and KERNEL-SPEC's determinism
promise does not scale with population size.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


def _run_flux(args: List[str], *, cwd: Path, env: Dict[str, str]) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "flux_engine.cli", *args]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


def _plan_apply_run_id(bundle_dir: Path, *, cwd: Path, state_dir: Path, env: Dict[str, str]) -> str:
    """Runs ``plan`` then ``apply`` as real subprocesses; returns the run id."""
    planned = _run_flux(
        ["--json", "plan", str(bundle_dir), "--state-dir", str(state_dir)], cwd=cwd, env=env
    )
    assert planned.returncode == 0, planned.stderr
    run_id = str(json.loads(planned.stdout)["artifacts"]["run_id"])

    applied = _run_flux(
        ["--json", "apply", str(bundle_dir), "--run-id", run_id, "--state-dir", str(state_dir)],
        cwd=cwd,
        env=env,
    )
    assert applied.returncode == 0, applied.stderr
    return run_id


def _plan_and_apply(bundle_dir: Path, *, cwd: Path, state_dir: Path, env: Dict[str, str]) -> str:
    """Runs ``plan`` then ``apply`` as real subprocesses; returns the run digest."""
    planned = _run_flux(
        ["--json", "plan", str(bundle_dir), "--state-dir", str(state_dir)], cwd=cwd, env=env
    )
    assert planned.returncode == 0, planned.stderr
    run_id = json.loads(planned.stdout)["artifacts"]["run_id"]

    applied = _run_flux(
        ["--json", "apply", str(bundle_dir), "--run-id", run_id, "--state-dir", str(state_dir)],
        cwd=cwd,
        env=env,
    )
    assert applied.returncode == 0, applied.stderr
    return str(json.loads(applied.stdout)["artifacts"]["run_digest"])


def test_replay_across_processes_hash_seeds_locales_timezones_and_cwds(
    tmp_path: Path,
) -> None:
    """The literal build-item requirement: two subprocess runs, deliberately
    made to disagree about everything an accidental non-determinism could
    hide behind, still produce the same ``runDigest``.
    """
    import os

    base_env = dict(os.environ)
    base_env.pop("PYTHONPATH", None)

    env_a = dict(base_env, PYTHONHASHSEED="random", TZ="Asia/Kolkata", LC_ALL="tr_TR.UTF-8")
    env_b = dict(base_env, PYTHONHASHSEED="random", TZ="UTC", LC_ALL="C")

    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "run-from-here" / "cwd-b"
    cwd_a.mkdir(parents=True)
    cwd_b.mkdir(parents=True)

    digest_a = _plan_and_apply(
        SMALL_FIXTURE_BUNDLE, cwd=cwd_a, state_dir=cwd_a / ".flux", env=env_a
    )
    digest_b = _plan_and_apply(
        SMALL_FIXTURE_BUNDLE, cwd=cwd_b, state_dir=cwd_b / ".flux", env=env_b
    )

    assert digest_a == digest_b


def _run_digest_document(state_dir: Path, run_id: str) -> Dict[str, object]:
    document = json.loads(
        (state_dir / "runs" / run_id / "run-digest.json").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def test_a_yaml_comment_moves_the_run_digest_and_no_component_across_processes(
    tmp_path: Path,
) -> None:
    """The two halves of the inert-edit claim, in one subprocess-level test.

    A single comment line is inserted into ``world.flux.yml``; nothing else
    in the bundle differs, and the parsed documents are equal. Run in a
    separate process, under a different ``PYTHONHASHSEED``/``TZ``/``LC_ALL``
    and from a different cwd than the unmutated bundle:

    * ``runDigest`` **moves**. KERNEL-SPEC §11 builds ``inputDigest`` from
      ``{path: raw-bytes digest}`` over every bundle document, with no
      exemption for bytes a parser drops, and §10's run digest document
      carries ``inputDigest`` as a field.
    * every entry of the ``components`` map (population, events, journeys,
      delivery, assignment, emits, decisions) is **byte-identical** -- the
      simulation itself did not move, and that is the checkable content of
      "the comment changed nothing".

    ``tests/acceptance/test_one_byte.py`` carries the ``fluxsim verify``
    readout for the same pair.
    """
    import os

    base_env = dict(os.environ)
    base_env.pop("PYTHONPATH", None)

    plain_bundle = tmp_path / "plain"
    commented_bundle = tmp_path / "commented"
    shutil.copytree(SMALL_FIXTURE_BUNDLE, plain_bundle)
    shutil.copytree(SMALL_FIXTURE_BUNDLE, commented_bundle)

    world_path = commented_bundle / "world.flux.yml"
    original_bytes = world_path.read_bytes()
    world_path.write_bytes(b"# a comment no part of the simulation reads\n" + original_bytes)
    assert world_path.read_bytes() != original_bytes
    assert yaml.safe_load(world_path.read_text(encoding="utf-8")) == yaml.safe_load(
        original_bytes.decode("utf-8")
    )

    cwd_plain = tmp_path / "cwd-plain"
    cwd_commented = tmp_path / "elsewhere" / "cwd-commented"
    cwd_plain.mkdir(parents=True)
    cwd_commented.mkdir(parents=True)
    state_plain = cwd_plain / ".flux"
    state_commented = cwd_commented / ".flux"

    run_plain = _plan_apply_run_id(
        plain_bundle,
        cwd=cwd_plain,
        state_dir=state_plain,
        env=dict(base_env, PYTHONHASHSEED="random", TZ="Asia/Kolkata", LC_ALL="tr_TR.UTF-8"),
    )
    run_commented = _plan_apply_run_id(
        commented_bundle,
        cwd=cwd_commented,
        state_dir=state_commented,
        env=dict(base_env, PYTHONHASHSEED="random", TZ="UTC", LC_ALL="C"),
    )

    plain_document = _run_digest_document(state_plain, run_plain)
    commented_document = _run_digest_document(state_commented, run_commented)

    assert plain_document["inputDigest"] != commented_document["inputDigest"]
    assert plain_document["components"] == commented_document["components"]


def _seeded_bundle_copy(root: Path, seed: int) -> Path:
    """A private copy of the shrunken fixture with ``simulation.flux.yml``'s
    ``spec.seed`` overwritten to *seed* -- the Simulation's own seed wins
    over the World's (``flux_engine.core.seedtree.effective_seed``), so
    this is the one field a fuzz loop needs to vary.
    """
    bundle = root / f"seed-{seed}"
    shutil.copytree(SMALL_FIXTURE_BUNDLE, bundle)
    sim_path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(sim_path.read_text(encoding="utf-8"))
    document["spec"]["seed"] = seed
    sim_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def test_fifty_seed_fuzz_all_replay_and_distinct_seeds_diverge(tmp_path: Path) -> None:
    from flux_engine.stages.apply import apply
    from flux_engine.stages.replay import replay

    digests_by_seed: Dict[int, str] = {}
    for seed in range(1, 51):
        bundle = _seeded_bundle_copy(tmp_path / "bundles", seed)
        state_dir = tmp_path / "states" / f"seed-{seed}" / ".flux"

        applied = apply(bundle, state_dir=state_dir)
        assert applied.success, (seed, applied.stderr)
        run_id = str(applied.artifacts["run_id"])
        run_digest = str(applied.artifacts["run_digest"])
        digests_by_seed[seed] = run_digest

        replayed = replay(bundle, run_id=run_id, assert_digest=run_digest, state_dir=state_dir)
        assert replayed.success, (seed, replayed.stderr)

    assert len(digests_by_seed) == 50
    assert len(set(digests_by_seed.values())) == 50, (
        "two distinct seeds produced the same run digest: " f"{sorted(digests_by_seed.items())}"
    )
