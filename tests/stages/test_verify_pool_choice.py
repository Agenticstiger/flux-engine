# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` starts a pool only when it pays, and reads its bundle once.

A pool costs about half a second to start, so ``--jobs 4`` on a small run was slower than
``--jobs 1``. The choice is made from the rows the receipt records, before any work, and it moves
no verdict and no byte. Held here, on the five-emit star schema of ``tests/fixtures/related-star``:

* a run below the threshold makes no pool at any ``--jobs``, a run above it does;
* the verdict, the artifacts and every line of output are the same in one process and through a
  real pool;
* the bundle is validated and parsed once per ``verify``, not once for each step that reads it, and
  a bundle edited between two loads is loaded again.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.seam.resolve import SeamBundle, bundle_load_scope, load_bundle
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

V = importlib.import_module("flux_engine.stages.verify")
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"


class Applied:
    """The star schema, planned and applied once."""

    def __init__(self, root: Path) -> None:
        self.bundle = root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.state = root / "state"
        planned = plan(self.bundle, state_dir=self.state)
        assert planned.success, planned.stderr
        applied = apply(self.bundle, state_dir=self.state, run_id=str(planned.artifacts["run_id"]))
        assert applied.success, applied.stderr
        self.layout = RunLayout.for_state_dir(self.state, str(applied.artifacts["run_id"]))

    def verify(self, **kwargs: Any) -> Any:
        return run_verify(
            self.bundle,
            run_id=self.layout.run_id,
            state_dir=self.state,
            allow_declared_baseline_drift=True,
            **kwargs,
        )


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> Applied:
    return Applied(tmp_path_factory.mktemp("verify-pool-choice"))


def _everything(result: Any) -> Dict[str, Any]:
    return {
        "exit": result.exit_code,
        "artifacts": repr(result.artifacts),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _spy_pool(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """Record the ``jobs`` each ``map_in_pool`` call of ``verify`` is given; run the work here."""
    seen: List[int] = []

    def spy(function: Any, tasks: Any, jobs: int, **_: Any) -> List[Any]:
        seen.append(jobs)
        return [function(task) for task in tasks]

    monkeypatch.setattr(V, "map_in_pool", spy)
    return seen


def test_a_small_run_makes_no_pool_whatever_the_jobs(
    applied: Applied, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: List[int] = []

    class Counted(concurrent.futures.ProcessPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            made.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Counted)
    result = applied.verify(jobs=4)
    assert result.exit_code == 0, result.stderr
    assert made == [], "a run too small to repay a pool still started one"


def test_a_run_past_the_threshold_still_uses_the_pool(
    applied: Applied, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_pool(monkeypatch)
    assert applied.verify(jobs=4).exit_code == 0
    assert seen == [1], "the small run was handed to the pool"
    seen.clear()
    monkeypatch.setattr(V, "_POOL_MIN_PARALLEL_ROWS", 1)
    assert applied.verify(jobs=4).exit_code == 0
    assert seen == [4]


def test_the_pool_is_chosen_from_the_rows_outside_the_largest_emit() -> None:
    def tasks(*rows: object) -> List[Any]:
        return [V._EmitTask(None, f"e{n}", {"rows": count}) for n, count in enumerate(rows)]  # type: ignore[arg-type]

    big = V._POOL_MIN_PARALLEL_ROWS
    assert not V._pool_pays(tasks())
    assert not V._pool_pays(tasks(10 * big))
    assert not V._pool_pays(tasks(10 * big, big - 1))
    assert V._pool_pays(tasks(10 * big, big))
    assert not V._pool_pays(tasks(None, "x", True))


def test_the_verdict_and_every_byte_are_the_same_through_a_real_pool(
    applied: Applied, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threshold only moves where emits are read: forced to a pool, the output is the same."""
    one = _everything(applied.verify(jobs=1))
    monkeypatch.setattr(V, "_POOL_MIN_PARALLEL_ROWS", 0)
    pooled = _everything(applied.verify(jobs=2))
    assert one == pooled
    assert one["exit"] == 0


def test_verify_loads_its_bundle_once(applied: Applied, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: List[int] = []
    real = SeamBundle.run

    def counted(self: Any) -> Any:
        runs.append(1)
        return real(self)

    monkeypatch.setattr(SeamBundle, "run", counted)
    result = applied.verify()
    assert result.exit_code == 0, result.stderr
    assert runs == [1], f"the bundle was validated {len(runs)} times in one verify"


def test_a_bundle_changed_between_loads_is_loaded_again(tmp_path: Path) -> None:
    copy = tmp_path / "bundle"
    shutil.copytree(FIXTURE, copy)
    with bundle_load_scope():
        first = load_bundle(copy)
        assert load_bundle(copy) is first
        victim = copy / "persona.flux.yml"
        victim.write_bytes(victim.read_bytes() + b"\n# touched\n")
        assert load_bundle(copy) is not first
    assert load_bundle(copy) is not load_bundle(copy), "the memo outlived its scope"


def _add_unreadable_links(bundle: Path) -> None:
    """A dangling link and a link loop beside the documents: neither is a file the loader reads."""
    (bundle / "dangling.yml").symlink_to(bundle / "no-such-target")
    (bundle / "loop-a.yml").symlink_to(bundle / "loop-b.yml")
    (bundle / "loop-b.yml").symlink_to(bundle / "loop-a.yml")


def test_links_the_loader_ignores_load_the_same_inside_and_outside_the_scope(
    tmp_path: Path,
) -> None:
    copy = tmp_path / "bundle"
    shutil.copytree(FIXTURE, copy)
    _add_unreadable_links(copy)
    outside = load_bundle(copy)
    with bundle_load_scope():
        first = load_bundle(copy)
        assert load_bundle(copy) is first
    assert first.file_bytes == outside.file_bytes
    assert first.flux_documents == outside.flux_documents


def test_verify_succeeds_on_a_bundle_with_unreadable_links(tmp_path: Path) -> None:
    run = Applied(tmp_path)
    _add_unreadable_links(run.bundle)
    result = run.verify()
    assert result.exit_code == 0, result.stderr
