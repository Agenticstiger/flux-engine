# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the temporal fixture's runs to ``tests/golden/temporal/digests.json``.

``tests/fixtures/temporal`` is planned and applied in two forms, and each pinned value in the data
file says where it came from:

* The keyless bundle, as the fixture is written, declares no ``lifetimes``, ``within``,
  ``hiddenColumns`` or ``hidden``. Its digests are **the ones origin/main (1c317c0) gives for the
  same bundle**, under kernel 0.1.0 and under kernel 0.2.0: they were computed by running the
  bundle on origin/main's own ``fluxsim``. A bundle written before the temporal keys existed
  replays to the digests it had, so a change that moves them is a bundle that no longer replays.
* The keyed bundle adds the chain of ``lifetimes`` and ``within`` entries from
  ``tests/fixtures/temporal/bundle.py``. Its digests are what this branch gives once the dates steps
  keep to each column's declared range. They guard the rows of a bundle with lifetimes, durations and
  ``within`` dates against later changes to periods, targets, rates and exposure. Kernel 0.1.0 refuses
  the keys with ``FLX-3105``, naming the kernel.

Every run is taken with ``--jobs 1`` and ``--jobs 4``, which must agree.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Set, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED: Dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "temporal" / "digests.json").read_text(encoding="utf-8")
)

GOLDEN_DIR = Path(__file__).resolve().parent / "temporal"
_KEYS = ("lifetimes", "within", "hiddenColumns", "hidden")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """Copies of the frozen bundle files, so a run never writes into the repository."""
    root = tmp_path_factory.mktemp("golden-temporal")
    for form in ("keyless", "keyed"):
        shutil.copytree(GOLDEN_DIR / form, root / form)
    return {form: root / form for form in ("keyless", "keyed")}


def _observed(bundle: Path, state_dir: Path, kernel: str, jobs: int) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state_dir, kernel=kernel, jobs=jobs)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    document = planned.artifacts["plan_document"]
    applied = apply(bundle, run_id=run_id, state_dir=state_dir, auto_plan=False, jobs=jobs)
    assert applied.success, applied.stderr
    receipt = applied.artifacts["receipt"]
    emits = receipt["emits"]
    return {
        "emits": {name: emits[name]["outputDigest"] for name in sorted(emits)},
        "generatorDigests": {
            name: document["emits"][name]["generator"]["generatorDigest"]
            for name in sorted(document["emits"])
        },
        "inputDigest": receipt["inputDigest"],
        "rows": {name: emits[name]["rows"] for name in sorted(emits)},
        "runDigest": receipt["runDigest"],
        "runId": run_id,
    }


CASES: Tuple[Tuple[str, str], ...] = (
    ("keyless", "0.1.0"),
    ("keyless", "0.2.0"),
    ("keyed", "0.2.0"),
)


@pytest.mark.parametrize("jobs", [1, 4])
@pytest.mark.parametrize("form,kernel", CASES)
def test_the_run_gives_the_pinned_digests(
    bundles: Dict[str, Path], tmp_path: Path, form: str, kernel: str, jobs: int
) -> None:
    observed = _observed(bundles[form], tmp_path / ".flux", kernel, jobs)
    assert observed == PINNED[form]["kernels"][kernel]


def test_kernel_0_1_0_refuses_the_keyed_bundle(bundles: Dict[str, Path], tmp_path: Path) -> None:
    pinned = PINNED["keyed"]["refusal"]
    planned = plan(bundles["keyed"], state_dir=tmp_path / ".flux", kernel=pinned["kernel"])
    assert not planned.success
    assert planned.error_code == pinned["code"]
    assert f"kernel {pinned['kernel']}" in planned.stderr


def _keys_of(node: Any) -> Set[str]:
    """Every mapping key anywhere in a parsed document."""
    if isinstance(node, dict):
        return {str(key) for key in node} | set().union(*(_keys_of(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys_of(v) for v in node))
    return set()


def _keys_in(form: str) -> Set[str]:
    found: Set[str] = set()
    for path in sorted((GOLDEN_DIR / form).glob("*.yml")):
        found |= _keys_of(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def test_the_keyless_bundle_holds_none_of_the_keys() -> None:
    assert not set(_KEYS) & _keys_in("keyless")
    assert {"lifetimes", "within"} <= _keys_in("keyed")


def test_the_pins_say_where_they_came_from() -> None:
    assert "origin/main at 1c317c0" in PINNED["keyless"]["source"]
    assert "keep to the declared range" in PINNED["keyed"]["source"]


def test_plan_apply_and_verify_the_keyed_bundle_from_the_command_line(
    bundles: Dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One CLI run: verify passes and every ``lifetime`` and ``within`` line holds."""
    bundle, state = str(bundles["keyed"]), str(tmp_path / ".flux")

    def run(*arguments: str) -> Tuple[int, Dict[str, Any]]:
        code = main([*arguments, "--state-dir", state, "-j"])
        return code, json.loads(capsys.readouterr().out)

    code, planned = run("plan", bundle)
    assert code == 0
    run_id = planned["artifacts"]["run_id"]
    code, applied = run("apply", bundle, "--run-id", run_id)
    assert code == 0
    assert applied["artifacts"]["run_digest"] == PINNED["keyed"]["kernels"]["0.2.0"]["runDigest"]
    code, verified = run("verify", bundle, "--run-id", run_id)
    assert code == 0
    artifacts = verified["artifacts"]
    assert artifacts["rowConformance"]["ok"] is True
    assert artifacts["integrity"]["ok"] is True
    rules = [rule for emit in artifacts["rules"]["emits"].values() for rule in emit]
    kinds = sorted(str(rule["rule"]).split(":")[0] for rule in rules)
    assert kinds == ["lifetime", "lifetime", "within", "within", "within"]
    assert all(rule["met"] and rule["enforced"] for rule in rules)
