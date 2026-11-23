# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A recorded plan runs, and is stamped, under the kernel it names (ADR 0010, step 2).

``plan.json`` records ``kernelVersion``. ``apply``, ``replay``, ``verify`` and
``ship`` take the kernel from it, and :data:`flux_engine.core.kernels.DEFAULT_KERNEL`
is only the kernel a new plan takes, so a plan made under kernel 0.1.0 recomputes
the same ``inputDigest``, and every digest after it, whatever the default is.

The tests move the default to a kernel registered only here: kernel 0.2.0's
behaviour under its own kernel and trace version strings. All three of its versions
differ from 0.1.0's, and so does every digest, so a stage that ran or stamped the
default instead of the plan's kernel shows up as a moved digest or a wrong version in
an artifact. (A real kernel names the one trace format :mod:`flux_engine.core.trace`
writes, which a test below holds the registry to; this one does not, so that a stamped
trace version shows.)
"""

from __future__ import annotations

import ast
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from flux_engine.core import kernels
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2, Kernel
from flux_engine.stages.apply import apply, load_recorded_plan, plan_kernel
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.pipeline import execute
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

from .conftest import FIXTURE_BUNDLE, REPO_ROOT

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"

#: The kernel the tests move the default to: kernel 0.2.0, which every stage below
#: must not stamp into a run planned under 0.1.0.
ALTERNATE = Kernel(
    version="0.2.0-alternate",
    seed_tree_version=KERNEL_V2.seed_tree_version,
    trace_version="flux-trace/1-alternate",
)


def _register_alternate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kernels, "KERNELS", {**kernels.KERNELS, ALTERNATE.version: ALTERNATE})


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _values_under(value: Any, wanted: str) -> Iterator[Any]:
    """Every value held under a *wanted* key, however deep."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == wanted:
                yield inner
            yield from _values_under(inner, wanted)
    elif isinstance(value, list):
        for inner in value:
            yield from _values_under(inner, wanted)


def _lineage_values(layout: RunLayout, wanted: str) -> List[Any]:
    lines = layout.lineage_path.read_text(encoding="utf-8").splitlines()
    return [found for line in lines if line for found in _values_under(json.loads(line), wanted)]


@dataclass(frozen=True)
class Baseline:
    layout: RunLayout
    run_digest: str


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Baseline:
    """One run planned under kernel 0.1.0 by name, which is not the default, before any test moves it."""
    state = tmp_path_factory.mktemp("kernel-baseline") / ".flux"
    planned = plan(FIXTURE_BUNDLE, state_dir=state, kernel=KERNEL_V1.version)
    assert planned.success, planned.stderr
    applied = apply(
        FIXTURE_BUNDLE, run_id=str(planned.artifacts["run_id"]), state_dir=state, auto_plan=False
    )
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    assert _read(layout.plan_path)["kernelVersion"] == KERNEL_V1.version
    return Baseline(layout=layout, run_digest=str(applied.artifacts["run_digest"]))


@pytest.fixture()
def run(baseline: Baseline, tmp_path: Path) -> RunLayout:
    """A copy of the baseline's state directory, so a test can rewrite it."""
    state = tmp_path / ".flux"
    shutil.copytree(baseline.layout.state_dir, state)
    return RunLayout.for_state_dir(state, baseline.layout.run_id)


@pytest.fixture()
def default_moved(monkeypatch: pytest.MonkeyPatch) -> Kernel:
    """A new plan takes the alternate kernel: the default moves again, as it moved to 0.2.0."""
    _register_alternate(monkeypatch)
    monkeypatch.setattr(kernels, "DEFAULT_KERNEL", ALTERNATE)
    return ALTERNATE


def test_a_new_plan_takes_the_default_kernel(
    baseline: Baseline, default_moved: Kernel, tmp_path: Path
) -> None:
    """The positive control: the default does reach a new plan, and its digests move."""
    planned = plan(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert planned.success, planned.stderr
    document = _read(
        RunLayout.for_state_dir(tmp_path / ".flux", str(planned.artifacts["run_id"])).plan_path
    )
    assert document["kernelVersion"] == default_moved.version
    assert document["seedTreeVersion"] == default_moved.seed_tree_version
    assert document["inputDigest"] != _read(baseline.layout.plan_path)["inputDigest"]
    assert planned.artifacts["run_id"] != baseline.layout.run_id


def test_replay_runs_the_plans_kernel_when_the_default_moves(
    baseline: Baseline, run: RunLayout, default_moved: Kernel
) -> None:
    """The plan's run digest, and kernel 0.1.0 in every file the replay rewrites."""
    replayed = replay(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        assert_digest=baseline.run_digest,
        state_dir=run.state_dir,
    )
    assert replayed.success, replayed.stderr

    receipt = _read(run.receipt_path)
    assert receipt["inputDigest"] == _read(baseline.layout.receipt_path)["inputDigest"]
    assert receipt["kernelVersion"] == KERNEL_V1.version
    assert receipt["seedTreeVersion"] == KERNEL_V1.seed_tree_version
    assert receipt["traceVersion"] == KERNEL_V1.trace_version
    assert _read(run.run_digest_path) == _read(baseline.layout.run_digest_path)
    for key, expected in (
        ("kernelVersion", KERNEL_V1.version),
        ("seedTreeVersion", KERNEL_V1.seed_tree_version),
        ("traceVersion", KERNEL_V1.trace_version),
    ):
        found = _lineage_values(run, key)
        assert found and set(found) == {expected}, (key, found)


def test_verify_and_the_golden_record_name_the_plans_kernel(
    run: RunLayout, default_moved: Kernel
) -> None:
    updated = verify(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        state_dir=run.state_dir,
        update_golden=True,
        reason="kernel threading test",
        allow_declared_baseline_drift=True,
    )
    assert updated.success, updated.stderr
    simulation_id = _read(run.receipt_path)["simulationId"]
    assert _read(run.golden_path(simulation_id))["kernelVersion"] == KERNEL_V1.version
    rows = run.golden_changelog_path.read_text(encoding="utf-8").splitlines()
    assert rows[-1].startswith(f"| {KERNEL_V1.version} |"), rows[-1]

    checked = verify(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        state_dir=run.state_dir,
        allow_declared_baseline_drift=True,
    )
    assert checked.success, checked.stderr


def _committed_slice(layout: RunLayout, root: Path) -> str:
    """A slice built from the run's own rows, so the gate reaches its packing path."""
    columns = _read(layout.seam_attestation_path(EXPOSE_ID))["columns"]
    lines = [line for line in layout.emit_path(EXPOSE_ID).read_text("utf-8").splitlines() if line]
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines]
    path = root / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path.as_uri()


def test_ship_names_the_plans_kernel(run: RunLayout, default_moved: Kernel, tmp_path: Path) -> None:
    shipped = ship(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        out_dir=tmp_path / "dist",
        state_dir=run.state_dir,
        committed_slice={EXPOSE_ID: _committed_slice(run, tmp_path)},
    )
    assert shipped.success, shipped.stderr
    statement = _read(Path(str(shipped.artifacts["runtimeStatement"])))
    assert statement["predicate"]["kernelVersion"] == KERNEL_V1.version
    report = (run.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert f"Kernel version: `{KERNEL_V1.version}`" in report
    assert ALTERNATE.version not in report


def test_a_plan_naming_a_kernel_this_engine_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plan made by an engine with a kernel this one does not have (FLX-2119).

    Planned and applied under the alternate kernel, then read by an engine that implements
    kernel 0.1.0 alone, as an older engine would.
    """
    _register_alternate(monkeypatch)
    monkeypatch.setattr(kernels, "DEFAULT_KERNEL", ALTERNATE)
    state = tmp_path / ".flux"
    applied = apply(FIXTURE_BUNDLE, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])

    monkeypatch.setattr(kernels, "KERNELS", {KERNEL_V1.version: KERNEL_V1})
    monkeypatch.setattr(kernels, "DEFAULT_KERNEL", KERNEL_V1)
    results = {
        "apply": apply(FIXTURE_BUNDLE, run_id=run_id, state_dir=state, auto_plan=False),
        "replay": replay(FIXTURE_BUNDLE, run_id=run_id, assert_digest="x", state_dir=state),
        "verify": verify(
            FIXTURE_BUNDLE, run_id=run_id, state_dir=state, allow_declared_baseline_drift=True
        ),
        "ship": ship(FIXTURE_BUNDLE, run_id=run_id, out_dir=tmp_path / "dist", state_dir=state),
    }
    for stage, result in results.items():
        assert result.exit_code == 1, (stage, result.stderr)
        assert result.error_code == "FLX-2119", (stage, result.stderr)
        assert ALTERNATE.version in result.stderr, (stage, result.stderr)
        assert "this engine implements kernel 0.1.0 (seed tree flux-seed/1)" in result.stderr, (
            stage,
            result.stderr,
        )
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    "document",
    [
        {"kernelVersion": "0.1.0", "seedTreeVersion": "flux-seed/2"},
        {"kernelVersion": "0.2.0", "seedTreeVersion": "flux-seed/1"},
        {"kernelVersion": "0.9.0", "seedTreeVersion": "flux-seed/9"},
        {"kernelVersion": None, "seedTreeVersion": "flux-seed/1"},
        {"seedTreeVersion": "flux-seed/1"},
    ],
)
def test_a_plan_names_a_kernel_only_with_that_kernels_seed_tree(document: dict) -> None:
    with pytest.raises(FluxError) as refused:
        plan_kernel(document)
    assert refused.value.code.code == "FLX-2119"


def test_plan_kernel_reads_both_kernels() -> None:
    assert plan_kernel({"kernelVersion": "0.1.0", "seedTreeVersion": "flux-seed/1"}) is KERNEL_V1
    assert plan_kernel({"kernelVersion": "0.2.0", "seedTreeVersion": "flux-seed/2"}) is KERNEL_V2


def test_reading_a_plan_refuses_a_kernel_this_engine_lacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``load_recorded_plan`` refuses on its own, so a reader that never asks for the kernel does too."""
    _register_alternate(monkeypatch)
    monkeypatch.setattr(kernels, "DEFAULT_KERNEL", ALTERNATE)
    planned = plan(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(tmp_path / ".flux", str(planned.artifacts["run_id"]))
    assert load_recorded_plan(layout)["kernelVersion"] == ALTERNATE.version

    monkeypatch.setattr(kernels, "KERNELS", {KERNEL_V1.version: KERNEL_V1})
    with pytest.raises(FluxError) as refused:
        load_recorded_plan(layout)
    assert refused.value.code.code == "FLX-2119"


def test_kernel_0_1_0_takes_its_versions_from_the_modules_that_own_them() -> None:
    """One declaration each: the registry holds no copy that could drift.

    It also pins kernel 0.2.0 as the default (ADR 0010, the amendment of 2026-11-21). Moving
    the default moves every pinned digest of a new plan, which is a golden move, and this test
    moves with it.
    """
    from flux_engine.core import seedtree, trace

    assert KERNEL_V1.seed_tree_version == seedtree.SEED_TREE_VERSION == "flux-seed/1"
    assert KERNEL_V1.trace_version == trace.TRACE_VERSION == "flux-trace/1"
    assert kernels.KERNELS == {"0.1.0": KERNEL_V1, "0.2.0": KERNEL_V2}
    assert kernels.DEFAULT_KERNEL is KERNEL_V2
    assert KERNEL_V2.seed_tree_version == "flux-seed/2"
    assert kernels.KERNELS[kernels.DEFAULT_KERNEL.version] is kernels.DEFAULT_KERNEL


def test_a_run_digest_naming_another_kernel_than_its_plan_is_refused(
    run: RunLayout, tmp_path: Path
) -> None:
    """``apply`` writes the plan's kernel into run-digest.json; a copy from another run (FLX-2113)."""
    document = _read(run.run_digest_path)
    document["kernelVersion"] = ALTERNATE.version
    run.run_digest_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    checked = verify(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        state_dir=run.state_dir,
        allow_declared_baseline_drift=True,
    )
    shipped = ship(
        FIXTURE_BUNDLE, run_id=run.run_id, out_dir=tmp_path / "dist", state_dir=run.state_dir
    )
    for result in (checked, shipped):
        assert result.exit_code == 1, result.stderr
        assert result.error_code == "FLX-2113", result.stderr
        assert "kernelVersion" in result.stderr, result.stderr


#: The registry's names a stage must not reach for. The default is the kernel of
#: a new plan only, and kernel 0.1.0 by name is wrong for a plan of any other.
_REGISTRY_NAMES = ("DEFAULT_KERNEL", "KERNEL_V1")


def _registry_uses(path: Path) -> List[str]:
    """``name in scope`` for every use of a registry name in *path*.

    A use is a read of the name or of an attribute by that name, an import of
    it, or the name as a string, as ``getattr`` takes it. The scope is the
    innermost function around the use, or ``<module>``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: List[str] = []
    for node in ast.walk(tree):
        name: Any = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name
            if (
                name == "*"
                and getattr(parents.get(node), "module", None) == "flux_engine.core.kernels"
            ):
                name = "DEFAULT_KERNEL"
        elif isinstance(node, ast.Constant):
            name = node.value
        if name not in _REGISTRY_NAMES:
            continue
        scope: Any = parents.get(node)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        found.append(f"{name} in {scope.name if scope is not None else '<module>'}")
    return found


def test_only_a_new_plan_reads_the_default_kernel() -> None:
    """The registry reaches one place outside itself: ``execute``, for a new plan.

    A module that read the default anywhere else, or named kernel 0.1.0, would
    stamp a kernel the plan did not name into the artifacts of a recorded run,
    which is what this step removed.
    """
    registry = REPO_ROOT / "flux_engine" / "core" / "kernels.py"
    uses = {
        str(path.relative_to(REPO_ROOT)): _registry_uses(path)
        for path in sorted((REPO_ROOT / "flux_engine").rglob("*.py"))
        if path != registry
    }
    found = {path: names for path, names in uses.items() if names}
    assert found == {"flux_engine/stages/pipeline.py": ["DEFAULT_KERNEL in execute"]}, found


def test_every_kernel_names_the_trace_format_core_trace_writes() -> None:
    """``core.trace`` stamps ``traceVersion`` into run-digest.json and every component digest.

    It writes one format, so a kernel that named another would be stamped with
    two trace versions in one run.
    """
    from flux_engine.core import trace

    assert {kernel.trace_version for kernel in kernels.KERNELS.values()} == {trace.TRACE_VERSION}


def test_apply_refuses_an_execution_of_another_kernel_than_its_plan(run: RunLayout) -> None:
    """A caller's precomputed execution must have run the plan's kernel (FLX-2101)."""
    receipt = run.receipt_path.read_bytes()
    other = execute(FIXTURE_BUNDLE, kernel=ALTERNATE)
    refused = apply(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        state_dir=run.state_dir,
        auto_plan=False,
        precomputed=other,
    )
    assert refused.exit_code == 1, refused.stderr
    assert refused.error_code == "FLX-2101", refused.stderr
    assert f"ran kernel {ALTERNATE.version}" in refused.stderr, refused.stderr
    assert f"names kernel {KERNEL_V1.version}" in refused.stderr, refused.stderr
    assert run.receipt_path.read_bytes() == receipt


def test_the_single_kernel_constant_does_not_come_back() -> None:
    """``KERNEL_VERSION`` was the one constant every stage stamped; nothing defines it now."""
    for path in sorted((REPO_ROOT / "flux_engine").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        assert "KERNEL_VERSION" not in names, path


def test_a_new_plan_with_no_kernel_named_takes_kernel_0_2_0(tmp_path: Path) -> None:
    """The real default, not a stand-in: a plan that names no kernel records 0.2.0."""
    planned = plan(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(tmp_path / ".flux", str(planned.artifacts["run_id"]))
    document = _read(layout.plan_path)
    assert (document["kernelVersion"], document["seedTreeVersion"]) == (
        KERNEL_V2.version,
        KERNEL_V2.seed_tree_version,
    )
    assert plan_kernel(document) is KERNEL_V2


def test_a_plan_recorded_under_kernel_0_1_0_replays_to_its_digest_under_the_new_default(
    baseline: Baseline, run: RunLayout
) -> None:
    """An old plan replays on the kernel it names, to the digest it had, whatever a new plan takes."""
    assert kernels.DEFAULT_KERNEL is KERNEL_V2
    replayed = replay(
        FIXTURE_BUNDLE,
        run_id=run.run_id,
        assert_digest=baseline.run_digest,
        state_dir=run.state_dir,
    )
    assert replayed.success, replayed.stderr
    assert _read(run.plan_path)["kernelVersion"] == KERNEL_V1.version
    assert _read(run.run_digest_path) == _read(baseline.layout.run_digest_path)
