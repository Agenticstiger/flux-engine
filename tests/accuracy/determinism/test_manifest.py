# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The determinism manifest is a function of the seed and nothing else.

Pass criteria (pull-request tier; the nightly matrix compares the same manifest across
operating systems, Python versions and NumPy versions, ``docs/accuracy/DESIGN.md`` section 7):

* two builds in one process give identical ``digests``;
* a fresh interpreter with another ``PYTHONHASHSEED``, ``LC_ALL=C`` and a hostile ambient
  ``decimal`` context gives identical ``digests``;
* the pinned end-to-end run of ``telco-fidelity-small`` has its pinned digest;
* ``compare.py`` exits 0 for equal manifests and 1 for a one-digit change, naming the key
  and the cells that disagree;
* the end-to-end run under an ambient precision of 5 or 7 with ``ROUND_UP`` gives the
  baseline's digests. Those two contexts used to break it: precision 5 raised, and
  precisions 6 and 7 moved the run digest.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from tests.accuracy.determinism import compare as compare_module
from tests.accuracy.determinism.manifest import (
    MANIFEST_SCHEMA,
    REPO_ROOT,
    build_manifest,
    canonical_json,
    derive_seed,
)
from tests.accuracy.harness.record import ResultLog
from tests.accuracy.harness.tier import Tier

FAMILY = "determinism"

#: The pinned run digest of ``tests/fixtures/telco-fidelity-small`` at its committed seed
#: (``spec.seed: 42``), and of its one emit. Not the nightly's fresh seed: these are the same
#: bytes every night. A change here is a change in what a run is, and goes through the
#: golden changelog like any other (``GOLDEN-CHANGELOG.md``).
PINNED_RUN_DIGEST = "e0ba53b525c66686a5a71348ddc9dbe53667f5485476ea215ba0de8ce1afbdbe"
PINNED_EMIT_DIGEST = "6df957f18e20332f46e53964cf1c43794c5dde50297a4328f5b0afc691b24213"


@pytest.fixture(scope="module")
def seed(tier: Tier) -> int:
    return tier.seed("determinism", "manifest")


@pytest.fixture(scope="module")
def quick(tier: Tier) -> bool:
    return tier.pick(pr=True, nightly=False)


@pytest.fixture(scope="module")
def baseline(seed: int, quick: bool) -> Dict[str, Any]:
    return build_manifest(seed, quick=quick)


class HarnessError(RuntimeError):
    """The test's own machinery failed. Never an ``AssertionError``, so it cannot be read
    as a finding about the engine."""


def _spawn_manifest(
    tmp_path: Path,
    seed: int,
    quick: bool,
    *,
    hash_seed: str = "1234567",
    ambient: str = "",
    skip_run: bool = False,
) -> Tuple["subprocess.CompletedProcess[str]", Optional[Dict[str, Any]]]:
    """Build a manifest in a fresh interpreter in a hostile environment.

    Returns the finished process and the manifest it wrote (``None`` if it wrote none).
    """
    out = tmp_path / f"child-{hash_seed}-{ambient.replace(':', '_')}-{skip_run}.json"
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    argv: List[str] = [
        sys.executable,
        "-m",
        "tests.accuracy.determinism.manifest",
        "--seed",
        str(seed),
        "--out",
        str(out),
    ]
    if quick:
        argv.append("--quick")
    if ambient:
        argv += ["--ambient-decimal", ambient]
    if skip_run:
        argv.append("--skip-run")
    done = subprocess.run(
        argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600, check=False
    )
    manifest: Optional[Dict[str, Any]] = (
        json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    )
    return done, manifest


def _child_manifest(tmp_path: Path, seed: int, quick: bool, **options: Any) -> Dict[str, Any]:
    """:func:`_spawn_manifest`, requiring the child to succeed."""
    done, manifest = _spawn_manifest(tmp_path, seed, quick, **options)
    if done.returncode != 0 or manifest is None:
        raise HarnessError(f"the child manifest build failed: {done.stderr[-1500:]}")
    return manifest


def _differences(left: Mapping[str, Any], right: Mapping[str, Any]) -> List[str]:
    return compare_module.compare({"left": left, "right": right})


# ---------------------------------------------------------------------------
# The manifest's shape
# ---------------------------------------------------------------------------


def _no_floats(value: Any) -> bool:
    if isinstance(value, float):
        return False
    if isinstance(value, dict):
        return all(_no_floats(item) for item in value.values())
    if isinstance(value, list):
        return all(_no_floats(item) for item in value)
    return True


def test_manifest_is_canonical_and_float_free(baseline: Dict[str, Any]) -> None:
    text = canonical_json(baseline)
    assert json.loads(text) == baseline
    assert (
        text
        == json.dumps(baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    )
    assert _no_floats(baseline)
    assert baseline["schema"] == MANIFEST_SCHEMA
    assert set(baseline) == {"schema", "profile", "seed", "digests", "environment"}
    for key, value in baseline["digests"].items():
        assert isinstance(value, str) and len(value) == 64, key
        int(value, 16)
    families = {key.split(".", 1)[0] for key in baseline["digests"]}
    assert families == {"kernel", "numerics", "seedtree", "generator", "run"}
    assert {"python", "numpy", "platform"} <= set(baseline["environment"])


def test_pinned_end_to_end_run_has_its_pinned_digest(
    baseline: Dict[str, Any], accuracy_log: ResultLog, seed: int
) -> None:
    digests = baseline["digests"]
    accuracy_log.record(
        family=FAMILY,
        case="pinned-run",
        criterion="the small fixture's run digest and emit digest equal their pins",
        passed=digests["run.digest"] == PINNED_RUN_DIGEST
        and digests["run.emit.payment_recovery_moment"] == PINNED_EMIT_DIGEST,
        seed=seed,
        metrics={"runDigest": digests["run.digest"]},
    )
    assert digests["run.digest"] == PINNED_RUN_DIGEST
    assert digests["run.emit.payment_recovery_moment"] == PINNED_EMIT_DIGEST


def test_seeded_digests_move_with_the_seed_and_pinned_ones_do_not(
    baseline: Dict[str, Any], seed: int, quick: bool
) -> None:
    other = build_manifest(derive_seed(seed, "other"), include_run=False, quick=quick)
    moved = {key for key in other["digests"] if other["digests"][key] != baseline["digests"][key]}
    seeded = {
        key
        for key in baseline["digests"]
        if key.startswith(("kernel.", "seedtree.", "generator.rows"))
    }
    # Every seeded digest moved; nothing that is not a function of the seed did.
    assert seeded <= moved, sorted(seeded - moved)
    assert not {key for key in moved if key.startswith(("numerics.", "generator.plan"))}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_two_in_process_builds_are_identical(
    baseline: Dict[str, Any], seed: int, quick: bool, accuracy_log: ResultLog
) -> None:
    again = build_manifest(seed, quick=quick)
    differing = _differences(baseline, again)
    accuracy_log.record(
        family=FAMILY,
        case="in-process-twice",
        criterion="two builds in one process have identical digests",
        passed=not differing,
        seed=seed,
        metrics={"digests": len(baseline["digests"]), "differing": len(differing)},
    )
    assert not differing, "\n".join(differing)


def test_fresh_interpreter_with_hostile_environment_is_identical(
    baseline: Dict[str, Any], seed: int, quick: bool, tmp_path: Path, accuracy_log: ResultLog
) -> None:
    # Another PYTHONHASHSEED, LC_ALL=C, and an ambient decimal context set before the
    # engine runs: eight digits, rounding away from zero.
    child = _child_manifest(tmp_path, seed, quick, ambient="8:ROUND_UP")
    differing = _differences(baseline, child)
    accuracy_log.record(
        family=FAMILY,
        case="hostile-subprocess",
        criterion="PYTHONHASHSEED, LC_ALL=C and an ambient decimal context change no digest",
        passed=not differing,
        seed=seed,
        metrics={"ambient": "8:ROUND_UP", "digests": len(child["digests"])},
    )
    assert not differing, "\n".join(differing)


def test_library_surfaces_ignore_even_a_five_digit_ambient_context(
    baseline: Dict[str, Any], seed: int, quick: bool, tmp_path: Path
) -> None:
    child = _child_manifest(tmp_path, seed, quick, ambient="5:ROUND_UP", skip_run=True)
    expected = {k: v for k, v in baseline["digests"].items() if not k.startswith("run.")}
    assert child["digests"] == expected


@pytest.mark.parametrize(
    "ambient",
    [
        pytest.param("5:ROUND_UP", id="precision-5"),
        pytest.param("7:ROUND_UP", id="precision-7"),
    ],
)
def test_end_to_end_run_ignores_a_low_precision_ambient_context(
    ambient: str,
    baseline: Dict[str, Any],
    seed: int,
    quick: bool,
    tmp_path: Path,
    accuracy_log: ResultLog,
) -> None:
    done, child = _spawn_manifest(tmp_path, seed, quick, ambient=ambient)
    if done.returncode == 0 and child is None:
        raise HarnessError("the child exited 0 and wrote no manifest")
    differing = [] if child is None else _differences(baseline, child)
    accuracy_log.record(
        family=FAMILY,
        case=f"ambient-decimal-{ambient}",
        criterion="the end-to-end run does not depend on the ambient decimal context",
        passed=done.returncode == 0 and not differing,
        seed=seed,
        metrics={"ambient": ambient, "exit": done.returncode, "differing": len(differing)},
    )
    assert done.returncode == 0, f"the child run raised: {done.stderr[-1500:]}"
    assert not differing, "\n".join(differing)


# ---------------------------------------------------------------------------
# compare.py
# ---------------------------------------------------------------------------


def _write(path: Path, manifest: Mapping[str, Any]) -> str:
    path.write_text(canonical_json(manifest), encoding="utf-8")
    return str(path)


def _tiny() -> Dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "profile": "quick",
        "seed": 7,
        "digests": {"kernel.a": "0" * 64, "kernel.b": "1" * 64, "run.digest": "2" * 64},
        "environment": {"python": "3.11.0", "numpy": "1.26.0", "platform": "linux"},
    }


def test_compare_exits_zero_for_equal_manifests_whatever_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    left, right = _tiny(), _tiny()
    right["environment"] = {"python": "3.13.1", "numpy": "2.3.0", "platform": "darwin"}
    third = copy.deepcopy(left)
    paths = [
        _write(tmp_path / f"{n}.json", m) for n, m in (("a", left), ("b", right), ("c", third))
    ]
    assert compare_module.main(paths) == 0
    assert "identical" in capsys.readouterr().out


def test_compare_exits_one_for_a_one_digit_change_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    left, right = _tiny(), _tiny()
    changed = right["digests"]["kernel.b"]
    right["digests"]["kernel.b"] = changed[:-1] + ("2" if changed[-1] != "2" else "3")
    third = copy.deepcopy(left)
    a, b, c = (
        _write(tmp_path / "linux-3.11.json", left),
        _write(tmp_path / "macos-3.12.json", right),
        _write(tmp_path / "linux-3.13.json", third),
    )
    assert compare_module.main([a, b, c]) == 1
    out = capsys.readouterr().out
    assert "DIFFERS kernel.b" in out
    assert "kernel.a" not in out
    # The odd cell out is named, and so are the two that agree.
    assert out.count("macos-3.12.json") == 1
    assert str(tmp_path / "linux-3.11.json") in out and str(tmp_path / "linux-3.13.json") in out


def test_compare_treats_a_missing_digest_and_a_seed_mismatch_as_differences(
    tmp_path: Path,
) -> None:
    left, right = _tiny(), _tiny()
    del right["digests"]["run.digest"]
    assert any("run.digest" in line for line in _differences(left, right))
    other_seed = _tiny()
    other_seed["seed"] = 8
    assert any("seed differs" in line for line in _differences(left, other_seed))


def test_compare_runs_as_a_module_and_needs_nothing_but_the_standard_library(
    tmp_path: Path,
) -> None:
    a = _write(tmp_path / "a.json", _tiny())
    other = _tiny()
    other["digests"]["run.digest"] = "f" * 64
    b = _write(tmp_path / "b.json", other)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    Path(a + ".copy").write_text(Path(a).read_text(encoding="utf-8"), encoding="utf-8")
    equal = subprocess.run(
        [sys.executable, "-m", "tests.accuracy.determinism.compare", a, a + ".copy"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert equal.returncode == 0, equal.stderr
    unequal = subprocess.run(
        [sys.executable, "-m", "tests.accuracy.determinism.compare", a, b],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unequal.returncode == 1
    assert "DIFFERS run.digest" in unequal.stdout


def test_compare_modules_import_no_dependency_group_member(tmp_path: Path) -> None:
    """``manifest.py`` and ``compare.py`` run in a cell that installs ``.[dev]`` and numpy only."""
    code = (
        "import sys, importlib;"
        "importlib.import_module('tests.accuracy.determinism.manifest');"
        "importlib.import_module('tests.accuracy.determinism.compare');"
        "bad = [m for m in ('scipy', 'hypothesis_jsonschema', 'pytest_benchmark') "
        "if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_compare_rejects_manifests_with_no_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = _tiny()
    empty["digests"] = {}
    a = _write(tmp_path / "a.json", empty)
    b = _write(tmp_path / "b.json", empty)
    assert compare_module.main([a, b]) == 2
    assert "no digests" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"digests": []}', '{"seed": 1}'],
    ids=["empty-file", "not-json", "not-an-object", "digests-not-a-mapping", "no-digests-key"],
)
def test_compare_exits_two_for_unreadable_input_not_one(
    content: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _write(tmp_path / "good.json", _tiny())
    bad = tmp_path / "bad.json"
    bad.write_text(content, encoding="utf-8")
    assert compare_module.main([good, str(bad)]) == 2
    assert "bad.json" in capsys.readouterr().err
    assert compare_module.main([good, str(tmp_path / "missing.json")]) == 2


def test_compare_treats_a_schema_mismatch_as_a_difference(tmp_path: Path) -> None:
    other = _tiny()
    other["schema"] = "flux-accuracy-determinism/2"
    assert any("schema differs" in line for line in _differences(_tiny(), other))
    a = _write(tmp_path / "a.json", _tiny())
    b = _write(tmp_path / "b.json", other)
    assert compare_module.main([a, b]) == 1


def test_compare_expect_fails_unless_exactly_that_many_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [_write(tmp_path / f"{n}.json", _tiny()) for n in ("a", "b", "c")]
    assert compare_module.main(["--expect", "3", *paths]) == 0
    assert compare_module.main(["--expect", "4", *paths]) == 2
    assert "expected 4 manifests, got 3" in capsys.readouterr().err
    assert compare_module.main(["--expect", "2", *paths]) == 2
    assert compare_module.main(paths) == 0
