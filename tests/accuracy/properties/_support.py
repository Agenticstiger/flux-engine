# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the properties family: a generator expose, a small run, a child process.

Nothing here asserts anything. It builds the inputs the relations run on, and
it is plain Python so the regression replay can import it without pytest
fixtures.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator.rows import GeneratorPlan, build_plan
from flux_engine.seam.contract import CompiledExpose, compile_expose


class HarnessError(RuntimeError):
    """The test's own setup failed. Never an ``AssertionError``, never a pinned defect."""


class HostileRunCrashed(Exception):
    """The child run died with ``decimal.InvalidOperation``: the pinned ambient-context defect."""


REPO_ROOT = Path(__file__).resolve().parents[3]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

SIM_ID = "sim-accuracy"
EXPOSE_ID = "accuracy_stream"
#: A stand-in seam digest: the generator needs a well-formed ``H`` and nothing more.
H = "sha256:" + "1" * 64

TIME_BOUNDS: Dict[str, str] = {"start": "2026-07-01T00:00:00Z", "end": "2026-09-30T23:59:59Z"}

# ---------------------------------------------------------------------------
# A generator expose that covers every family
# ---------------------------------------------------------------------------

#: One column per sampler path the generator has: identifier (derived), categorical
#: string, Poisson count with a null fraction, uniform int, bounded normal, exponential
#: rate, Bernoulli, uniform date, uniform timestamp.
BASE_SCHEMA: Tuple[Dict[str, Any], ...] = (
    {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "market", "type": "STRING"},
    {"name": "count", "type": "INT"},
    {"name": "level", "type": "INT"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "wait", "type": "DECIMAL(18,4)"},
    {"name": "active", "type": "BOOL"},
    {"name": "seen_on", "type": "DATE"},
    {"name": "seen_at", "type": "TIMESTAMP"},
)

BASE_FIDELITY_COLUMNS: Dict[str, Dict[str, Any]] = {
    "market": {"support": {"values": ["IE", "GB", "FR", "DE"]}, "maxCardinality": 8},
    "count": {"mean": "3", "nullFraction": "0.2"},
    "level": {"range": {"min": "0", "max": "1000"}},
    "amount": {
        "range": {"min": "0", "max": "10000"},
        "mean": "2500",
        "stddev": "1200",
    },
    "wait": {"rate": "0.5", "range": {"min": "0", "max": "1000"}},
    "active": {"levels": {"true": "0.25", "false": "0.75"}},
    "seen_on": {"range": {"min": "2026-07-01", "max": "2026-09-30"}},
    "seen_at": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"}},
}

#: Columns a relation may add to the base expose, keyed by a short spec name.
EXTRA_COLUMNS: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {
    "normal_decimal": (
        {"type": "DECIMAL(12,2)"},
        {"range": {"min": "-50", "max": "50"}, "mean": "5", "stddev": "10"},
    ),
    "poisson_int": ({"type": "INT"}, {"mean": "7"}),
    "uniform_int": ({"type": "INT"}, {"range": {"min": "-5", "max": "5"}}),
    "categorical_int": (
        {"type": "INT"},
        {"levels": {"1": "0.5", "2": "0.3", "3": "0.2"}},
    ),
    "string_levels": ({"type": "STRING"}, {"levels": {"a": "0.6", "b": "0.4"}}),
    "uuid": ({"type": "UUID"}, {}),
}


def expose_of(schema: Sequence[Mapping[str, Any]]) -> CompiledExpose:
    """Compile an ad-hoc stream expose from a column list."""
    return compile_expose(
        {
            "exposeId": EXPOSE_ID,
            "kind": "stream",
            "contract": {"schema": [dict(column) for column in schema]},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )


def root_of(seed: int, seam_digest: str = H) -> SeedTreeRoot:
    """A seed-tree root derived the way KERNEL-SPEC §3 derives one."""
    return derive_root(digest_bytes(seam_digest), seed)


def plan_of(
    schema: Sequence[Mapping[str, Any]],
    columns: Mapping[str, Any],
    *,
    rows: int,
    seam_digest: str = H,
) -> GeneratorPlan:
    return build_plan(
        expose_of(schema),
        sim_id=SIM_ID,
        seam_digest=seam_digest,
        rows=rows,
        fidelity={"columns": dict(columns)},
        time_bounds=TIME_BOUNDS,
    )


def base_plan(rows: int, *, order: Optional[Sequence[str]] = None) -> GeneratorPlan:
    """The base expose, optionally with its columns reordered to *order* (by name)."""
    schema: List[Dict[str, Any]] = [dict(column) for column in BASE_SCHEMA]
    if order is not None:
        by_name = {column["name"]: column for column in schema}
        schema = [by_name[name] for name in order]
    return plan_of(schema, BASE_FIDELITY_COLUMNS, rows=rows)


def base_names() -> Tuple[str, ...]:
    return tuple(column["name"] for column in BASE_SCHEMA)


# ---------------------------------------------------------------------------
# A small end-to-end run, in this process and in a child
# ---------------------------------------------------------------------------

#: The run-directory files that carry the run's bytes. ``plan.json`` is left out on
#: purpose: it records the wall-clock time the plan was written, which is metadata and
#: is excluded from every digest.
RUN_FILES: Tuple[str, ...] = (
    "population.ndjson",
    "events.ndjson",
    "decisions.ndjson",
    "decisions.meta.ndjson",
    "suppressions.ndjson",
    "lineage.ndjson",
    "receipt.json",
    "run-digest.json",
)


#: The OpenLineage file. Each event carries ``eventTime``, the wall-clock moment it was
#: written, which the OpenLineage spec requires and which no digest covers. It is the one
#: field removed before bytes are compared; the rest of the file is compared whole.
LINEAGE = "lineage.ndjson"


def _without_wall_clock(raw: bytes) -> bytes:
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key != "eventTime"}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    lines = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return "\n".join(json.dumps(strip(line), sort_keys=True) for line in lines).encode("utf-8")


@dataclass(frozen=True)
class RunBytes:
    """What one plan + apply wrote, as bytes."""

    run_id: str
    receipt: Mapping[str, Any]
    files: Mapping[str, bytes]

    @property
    def run_digest(self) -> str:
        return str(self.receipt["runDigest"])

    @property
    def output_digests(self) -> Dict[str, str]:
        return {name: str(item["outputDigest"]) for name, item in self.receipt["emits"].items()}


def read_run(run_dir: Path) -> RunBytes:
    """Collect the emit streams, seam attestations and run files of one run directory."""
    files: Dict[str, bytes] = {}
    for name in RUN_FILES:
        path = run_dir / name
        if path.is_file():
            files[name] = (
                _without_wall_clock(path.read_bytes()) if name == LINEAGE else path.read_bytes()
            )
    for path in sorted((run_dir / "emits").glob("*")):
        files[f"emits/{path.name}"] = path.read_bytes()
    for path in sorted(run_dir.glob("seam-attestation-*.json")):
        files[path.name] = path.read_bytes()
    receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    return RunBytes(run_id=run_dir.name, receipt=receipt, files=files)


def run_flux(argv: Sequence[str]) -> Dict[str, Any]:
    """Run ``flux_engine.cli.main`` in this process and return its ``--json`` envelope."""
    from flux_engine.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["-j", *argv])
    envelope = json.loads(buffer.getvalue())
    if code != 0 or not envelope.get("ok"):
        raise HarnessError(f"flux {' '.join(argv)} failed: {envelope.get('error')}")
    return envelope


def plan_and_apply(bundle: Path, state_dir: Path) -> RunBytes:
    """The two-step flow, ``plan`` then ``apply --run-id``, in this process."""
    planned = run_flux(["plan", str(bundle), "--state-dir", str(state_dir)])
    run_id = _run_id(planned)
    applied = run_flux(["apply", str(bundle), "--state-dir", str(state_dir), "--run-id", run_id])
    return read_run(Path(applied["artifacts"]["run_dir"]))


def apply_only(bundle: Path, state_dir: Path) -> RunBytes:
    """``apply`` with a fresh plan taken first."""
    applied = run_flux(["apply", str(bundle), "--state-dir", str(state_dir)])
    return read_run(Path(applied["artifacts"]["run_dir"]))


def _run_id(envelope: Mapping[str, Any]) -> str:
    artifacts = envelope["artifacts"]
    for key in ("run_id", "runId"):
        if key in artifacts:
            return str(artifacts[key])
    return str(artifacts["plan_digest"]).split(":", 1)[1][:16]


def hostile_child_run(
    bundle: Path,
    state_dir: Path,
    *,
    hash_seed: str,
    prec: int = 0,
    rounding: str = "ROUND_HALF_EVEN",
) -> RunBytes:
    """Plan + apply in a fresh interpreter with another ``PYTHONHASHSEED`` and, when *prec*
    is positive, an ambient ``decimal`` context of that precision and rounding mode set
    before the engine runs."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    script = Path(__file__).with_name("_child_run.py")
    completed = subprocess.run(
        [sys.executable, str(script), str(bundle), str(state_dir), str(prec), rounding],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        detail = f"child run failed ({completed.returncode}): {completed.stderr[-2000:]}"
        if "InvalidOperation" in completed.stderr:
            raise HostileRunCrashed(detail)
        raise HarnessError(detail)
    run_dir = Path(json.loads(completed.stdout.strip().splitlines()[-1])["run_dir"])
    return read_run(run_dir)


def copy_bundle(destination: Path) -> Path:
    """A private copy of the small bundle, safe to edit."""
    shutil.copytree(SMALL_BUNDLE, destination)
    return destination


def add_persona_trait(bundle: Path, name: str, declaration: Any) -> None:
    """Add one trait to the bundle's Persona document."""
    path = bundle / "persona.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["traits"][name] = deepcopy(declaration)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# The small bundle, staged for the world-level and signal-level relations
# ---------------------------------------------------------------------------


@dataclass
class SmallWorld:
    """The small bundle's documents, ready to materialise a population from."""

    bundle: Any
    cast: Any
    pack: Any
    world: Dict[str, Any]
    persona: Dict[str, Any]

    def resized(self, size: int) -> Dict[str, Any]:
        smaller = deepcopy(self.world)
        smaller["spec"]["population"]["size"] = size
        return smaller


_SMALL: Optional[SmallWorld] = None


def small_world() -> SmallWorld:
    """Load and validate ``tests/fixtures/telco-fidelity-small`` once per process."""
    global _SMALL
    if _SMALL is None:
        from jsonschema import Draft202012Validator, FormatChecker

        import flux_engine._vendor.flux_spec.validate as vendored_validate
        from flux_engine.runtime import resolve_cast
        from flux_engine.spec.validate import Bundle
        from flux_engine.verticals import get_pack

        schema = json.loads(Path(vendored_validate.FLUX_SCHEMA_PATH).read_text())
        bundle = Bundle(SMALL_BUNDLE, Draft202012Validator(schema, format_checker=FormatChecker()))
        errors = bundle.run()
        if errors:
            raise HarnessError(f"the small bundle does not validate: {errors}")
        cast = resolve_cast(bundle.flux_docs)
        pack = get_pack(cast.world["spec"]["vertical"])
        persona_id = cast.ids("Persona")[0]
        _SMALL = SmallWorld(
            bundle=bundle,
            cast=cast,
            pack=pack,
            world=deepcopy(dict(cast.world)),
            persona=deepcopy(dict(bundle.flux_docs[persona_id])),
        )
    return _SMALL


def population_of(persona: Mapping[str, Any], *, seed: int, size: int) -> Any:
    """Materialise a *size*-subject population of the small world around *persona*."""
    from flux_engine.core.seedtree import SeedTree
    from flux_engine.world import TraceLog, materialise_population

    small = small_world()
    return materialise_population(
        world=small.resized(size),
        personas=[persona],
        pack=small.pack,
        seed_tree=SeedTree(root=derive_root(b"H", seed)),
        trace=TraceLog(),
    )


def emit_small(*, seed: int, size: int, arrival: Any) -> Tuple[Any, ...]:
    """Materialise *size* subjects and emit their signals under *arrival*.

    The population is drawn on one tree and the signals on a fresh tree at the same
    root, so two calls that differ only in *arrival* differ only in the arrival model.
    """
    from flux_engine.core.clock import VirtualClock
    from flux_engine.core.seedtree import SeedTree
    from flux_engine.runtime import emit_signals
    from flux_engine.world import PopulationSpec, TraceLog, materialise_population

    small = small_world()
    world = small.resized(size)
    root = derive_root(b"H", seed)
    store = materialise_population(
        world=world,
        personas=[small.persona],
        pack=small.pack,
        seed_tree=SeedTree(root=root),
        trace=TraceLog(),
    )
    bounds = small.cast.simulation["spec"]["timeBounds"]
    clock = VirtualClock.from_time_bounds(bounds["start"], bounds["end"])
    return emit_signals(
        cast=small.cast,
        world=PopulationSpec.from_document(world),
        store=store,
        clock=clock,
        seed_tree=SeedTree(root=root),
        trace=TraceLog(),
        pack=small.pack,
        arrival=arrival,
    )


def default_arrival() -> Any:
    """The engine's own default arrival model."""
    from flux_engine.runtime.signals import DEFAULT_ARRIVAL_MODEL

    return DEFAULT_ARRIVAL_MODEL
