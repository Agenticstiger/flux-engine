# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a delivery hands each sink besides the rows: the grade the run's plan records, the bundle
and the Simulation's id (ADR 0038 section 3).

Pinned here:

* a context on either delivery path (``run --sink`` and ``apply --run-id --sink``) carries the grade,
  the bundle and the Simulation's id;
* a run that pins no profile records no grade, and the context says why;
* a plan edited after the run is not believed, and the context names the check it failed;
* a sink that ignores the new fields writes what it wrote before.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from flux_engine.sinks import registry
from flux_engine.sinks.receipts import StreamContext
from flux_engine.stages.apply import apply
from flux_engine.stages.deliver import privacy_grade_of
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEVICES = FIXTURES / "kafka-devices"
PROFILE = FIXTURES / "kafka-devices.profile.json"
SIMULATION = "sim-devices"


class _Recorder:
    """A sink target that notes each stream's context and publishes nothing."""

    name = "recorder"

    def __init__(self) -> None:
        self.contexts: List[StreamContext] = []

    def sink_for(self, context: StreamContext) -> Any:
        self.contexts.append(context)
        return _Sink(context)


class _Sink:
    def __init__(self, context: StreamContext) -> None:
        from flux_engine.sinks.receipts import SinkReceipt

        self.context = context
        self.receipt = None
        self._receipt = SinkReceipt(
            sink="recorder",
            run_id=context.run_id,
            expose_id=context.expose_id,
            output_digest=context.output_digest.value,
            rows=context.rows,
            target="x",
        )

    def open(self, header: Any) -> None:
        pass

    def write(self, row: Any) -> None:
        pass

    def close(self) -> None:
        self.receipt = self._receipt


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    found = _Recorder()
    monkeypatch.setattr(registry, "load_factory", lambda name: lambda target, options: found)
    return found


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(DEVICES, bundle)
    return bundle


def _check(contexts: List[StreamContext], bundle: Path, grade: str) -> None:
    assert len(contexts) == 6
    for context in contexts:
        assert context.privacy_grade is not None
        assert context.privacy_grade.grade == grade and context.privacy_grade.problem is None
        assert Path(str(context.bundle_directory)) == bundle
        assert context.simulation_id == SIMULATION


def test_run_sink_hands_each_stream_the_grade_the_bundle_and_the_simulation(
    tmp_path: Path, recorder: _Recorder
) -> None:
    bundle = _bundle(tmp_path)
    result = run(bundle, state_dir=tmp_path / "state", profile=PROFILE, sink="recorder:x")
    assert result.success, result.stderr
    _check(recorder.contexts, bundle, "P1")


def test_apply_run_id_sink_hands_each_stream_the_same(tmp_path: Path, recorder: _Recorder) -> None:
    bundle, state = _bundle(tmp_path), tmp_path / "state"
    planned = plan(bundle, state_dir=state, profile=PROFILE)
    assert planned.success, planned.stderr
    applied = apply(bundle, run_id=planned.artifacts["run_id"], state_dir=state, sink="recorder:x")
    assert applied.success, applied.stderr
    _check(recorder.contexts, bundle, "P1")


def test_a_run_that_pins_no_profile_records_no_grade_and_says_so(
    tmp_path: Path, recorder: _Recorder
) -> None:
    bundle = _bundle(tmp_path)
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    start = text.index("    profile:\n")
    end = text.index("    history:")
    (bundle / "simulation.flux.yml").write_text(text[:start] + text[end:], encoding="utf-8")
    result = run(bundle, state_dir=tmp_path / "state", sink="recorder:x")
    assert result.success, result.stderr
    for context in recorder.contexts:
        assert context.privacy_grade is not None and context.privacy_grade.grade is None
        assert "records no privacy grade" in str(context.privacy_grade.problem)


def test_a_plan_edited_after_the_run_gives_no_grade_and_names_its_check(tmp_path: Path) -> None:
    bundle, state = _bundle(tmp_path), tmp_path / "state"
    result = run(bundle, state_dir=state, profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["plan"]["run_id"])
    assert privacy_grade_of(layout).grade == "P1"
    document: Dict[str, Any] = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    document["privacyGrade"] = "P0"
    layout.plan_path.write_text(json.dumps(document), encoding="utf-8")
    recorded = privacy_grade_of(layout)
    assert recorded.grade is None
    assert "plan does not check" in str(recorded.problem)
    assert "FLX-" in str(recorded.problem)


class _Stripped:
    """A target that hands its sink the context as it was before the new fields."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name

    def sink_for(self, context: StreamContext) -> Any:
        return self.inner.sink_for(
            dataclasses.replace(
                context, privacy_grade=None, bundle_directory=None, simulation_id=None
            )
        )


def test_a_file_sink_writes_the_same_files_and_receipt_with_or_without_the_new_fields(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyarrow")
    from flux_engine.sinks.registry import open_target
    from flux_engine.stages.deliver import deliver_run

    bundle, state = _bundle(tmp_path), tmp_path / "state"
    result = run(bundle, state_dir=state, profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["plan"]["run_id"])

    def receipts(directory: Path, strip: bool) -> List[Mapping[str, Any]]:
        target: Any = open_target(f"parquet:{directory}")
        if strip:
            target = _Stripped(target)
        delivered = deliver_run(layout, bundle, target=target)
        files = {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        documents = [
            {key: value for key, value in item.as_document().items() if key != "target"}
            for item in delivered
        ]
        return [{"receipts": documents, "files": files}]

    with_fields = receipts(tmp_path / "a", strip=False)
    without = receipts(tmp_path / "b", strip=True)
    assert with_fields == without
    assert with_fields[0]["files"], "the sink wrote files"
