# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The devices bundle scaled up, generated once, and its streams as a sink receives them.

Shared by ``test_kafka_sink_overhead.py`` (the sink over a broker that keeps nothing) and
``kafka_real_broker.py`` (the sink over a real broker).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from flux_engine.api.emit import EmitHeader
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.sinks.receipts import StreamContext
from flux_engine.stages.deliver import privacy_grade_of
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.run import run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BUNDLE = FIXTURES / "kafka-devices"
PROFILE = FIXTURES / "kafka-devices.profile.json"
SIMULATION = "sim-devices"


def scaled_bundle(target: Path, rows: int) -> Path:
    """Copy the devices bundle to *target*, scaled so its events and ``reading`` hold ~*rows* rows."""
    shutil.copytree(BUNDLE, target)
    document = target / "simulation.flux.yml"
    text = document.read_text(encoding="utf-8")
    entities = max(1, rows * 3 // 10)
    for old, new in (
        ("entities: 60\n", f"entities: {entities}\n"),
        ("device_version:\n        rows: 200\n", f"device_version:\n        rows: {rows}\n"),
        ("reading:\n        rows: 600\n", f"reading:\n        rows: {rows}\n"),
        ("device_id: { range: { min: '1', max: '200' } }", f"device_id: {{ range: {{ min: '1', max: '{rows}' }} }}"),
        ("device_sk: { range: { min: '1', max: '200' } }", f"device_sk: {{ range: {{ min: '1', max: '{rows}' }} }}"),
        ("reading_id: { range: { min: '1', max: '600' } }", f"reading_id: {{ range: {{ min: '1', max: '{rows}' }} }}"),
    ):  # fmt: skip
        assert old in text, f"the devices bundle's {old.strip()!r} moved"
        text = text.replace(old, new)
    document.write_text(text, encoding="utf-8")
    return target


@dataclass(frozen=True)
class Stream:
    """One generated stream, ready to hand to a sink: its header, context and parsed rows."""

    header: EmitHeader
    context: StreamContext
    rows: List[List[Any]]


@dataclass(frozen=True)
class Prepared:
    bundle: Path
    layout: RunLayout

    def stream(self, expose_id: str) -> Stream:
        """The rows of *expose_id* as the deliver stage hands them to a sink."""
        from flux_engine.seam.resolve import load_bundle, resolve_all_emits

        receipt = json.loads(self.layout.receipt_path.read_text(encoding="utf-8"))
        entry = next(
            item
            for item in resolve_all_emits(load_bundle(self.bundle), simulation_id=SIMULATION)
            if item.expose_id == expose_id
        )
        expose = entry.compile()
        reported = receipt["emits"][expose_id]
        lines = self.layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        digest = DigestRef(
            value=reported["outputDigest"],
            domain=DigestDomain.JCS_NDJSON_STREAM,
            subject=f"outputDigest:{expose_id}",
        )
        header = EmitHeader(
            expose_id=expose_id,
            columns=expose.column_names,
            types=expose.type_names,
            contract_digest=entry.contract_digest,
        )
        context = StreamContext(
            run_id=self.layout.run_id,
            expose_id=expose_id,
            rows=len(rows),
            output_digest=digest,
            declared_types=tuple(column.declared_type for column in expose.columns),
            privacy_grade=privacy_grade_of(self.layout, self.bundle),
            bundle_directory=self.bundle,
            simulation_id=SIMULATION,
        )
        return Stream(header, context, rows)


def generate(root: Path, rows: int) -> Prepared:
    """Plan, generate and apply the bundle scaled to *rows* under *root*; its run, ready to deliver."""
    bundle = scaled_bundle(root / "bundle", rows)
    result = run(bundle, state_dir=root / "state", profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(root / "state", result.artifacts["plan"]["run_id"])
    return Prepared(bundle, layout)
