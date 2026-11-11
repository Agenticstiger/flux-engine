# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the seam tests: the shipped telco bundle, and bundles on demand.

Two sources of truth, deliberately different in kind.

``telco_bundle`` is the **real** example from the FLUX specification
repository, resolved through the real validator. Nothing about it is
restated here: the column count, the type names and the digests are read
out of the bundle by the tests that use them, never typed. If the example
moves, the tests that depend on its shape fail, which is the point --
they are the ones proving the seam works on the artifact the project
ships.

``write_bundle`` builds a **minimal** bundle in ``tmp_path`` from a small
Simulation and a FLUID contract the caller shapes. It exists so a test can
put one thing wrong -- a digest, a version pin, an expose id -- without
editing the shipped example, and so the seam's refusals are exercised on
bundles that are otherwise valid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import pytest
import yaml

from flux_engine.seam.contract import CompiledExpose, compile_expose, find_expose
from flux_engine.seam.resolve import (
    EXTENSION_NAMESPACE,
    LoadedBundle,
    ResolvedEmit,
    load_bundle,
    resolve_emit,
)
from tests.flux_examples import examples_are_required

#: The FLUX specification repository, checked out beside this one.
FLUX_REPO = Path(__file__).resolve().parents[3] / "FLUX"
TELCO_DIR = FLUX_REPO / "examples" / "telco-payment-recovery"

TELCO_SIMULATION_ID = "sim-payment-recovery"

# See tests/flux_examples.py: under FLUX_EXAMPLES_REQUIRED a missing checkout
# is a failure, not a silent skip.
requires_flux_examples = pytest.mark.skipif(
    not TELCO_DIR.is_dir() and not examples_are_required(),
    reason=(
        f"the FLUX example bundle is not checked out at {TELCO_DIR} "
        "(set FLUX_EXAMPLES_REQUIRED=1 to make this a failure instead)"
    ),
)


@pytest.fixture(scope="session")
def telco_dir() -> Path:
    """The shipped telco-payment-recovery bundle directory."""
    if not TELCO_DIR.is_dir():
        if examples_are_required():
            pytest.fail(f"FLUX_EXAMPLES_REQUIRED is set but {TELCO_DIR} is not checked out")
        pytest.skip(f"the FLUX example bundle is not checked out at {TELCO_DIR}")
    return TELCO_DIR


@pytest.fixture(scope="session")
def telco_bundle(telco_dir: Path) -> LoadedBundle:
    """The shipped bundle, loaded and validated."""
    return load_bundle(telco_dir)


@pytest.fixture(scope="session")
def telco_emit(telco_bundle: LoadedBundle) -> ResolvedEmit:
    """The shipped bundle's only emit, resolved."""
    return resolve_emit(telco_bundle, simulation_id=TELCO_SIMULATION_ID)


@pytest.fixture(scope="session")
def telco_expose(telco_emit: ResolvedEmit) -> CompiledExpose:
    """The compiled ``payment_recovery_moment`` expose."""
    return telco_emit.compile()


# ---------------------------------------------------------------------------
# Minimal bundles
# ---------------------------------------------------------------------------

MINIMAL_FLUID_VERSION = "0.7.5"


def minimal_contract(
    *,
    product_id: str = "test.gold.widget",
    expose_id: str = "widget_stream",
    schema: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """A FLUID DataProduct with one expose, valid for the vendored schema."""
    return {
        "fluidVersion": MINIMAL_FLUID_VERSION,
        "kind": "DataProduct",
        "id": product_id,
        "name": "Widget",
        "metadata": {"owner": {"team": "growth-lab"}},
        "exposes": [
            {
                "exposeId": expose_id,
                "kind": "stream",
                "contract": {
                    "schema": list(
                        schema
                        if schema is not None
                        else [
                            {"name": "widget_id", "type": "STRING", "required": True},
                            {"name": "amount", "type": "DECIMAL(18,4)"},
                        ]
                    )
                },
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "test.widget.v1"},
                },
            }
        ],
    }


def minimal_simulation(
    *,
    simulation_id: str = "sim-widget",
    world_id: str = "world-widget",
    emits: Optional[List[Mapping[str, Any]]] = None,
    extension_emits: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """A Simulation and the World it references -- the smallest legal bundle.

    *extension_emits* becomes ``extensions["com.agentics.flux/1"].emits``,
    keyed by exposeId -- the RFC-02 namespace SEAM-FIDELITY-SPEC §2's
    block, the declared row count and the column mapping live in.
    """
    world = {
        "fluxVersion": "0.5.0",
        "kind": "World",
        "id": world_id,
        "name": "Widget World",
        "metadata": {"owner": {"team": "growth-lab"}},
        "spec": {
            "population": {"size": 10, "lifecycleMix": {"active": 1.0}},
            "seed": 7,
        },
    }
    simulation: Dict[str, Any] = {
        "fluxVersion": "0.5.0",
        "kind": "Simulation",
        "id": simulation_id,
        "name": "Widget Simulation",
        "metadata": {"owner": {"team": "growth-lab"}},
        "spec": {
            "worldRef": world_id,
            "fidelity": "deterministic",
            "seed": 42,
            "timeBounds": {"start": "2026-07-01T00:00:00Z", "end": "2026-09-30T23:59:59Z"},
            "emits": list(emits if emits is not None else [minimal_emit()]),
        },
    }
    if extension_emits is not None:
        simulation["extensions"] = {EXTENSION_NAMESPACE: {"emits": dict(extension_emits)}}
    return [world, simulation]


def minimal_emit(
    *,
    product_ref: str = "test.gold.widget",
    expose_id: str = "widget_stream",
    fluid_version: str = MINIMAL_FLUID_VERSION,
    **extra: Any,
) -> Dict[str, Any]:
    """One ``emits[]`` entry; *extra* adds provenance, minCredibility, ...

    Not the fidelity block or the row count: SEAM-FIDELITY-SPEC §2 puts
    those in the document-level extension map, and FLUX 0.5.0 closes this
    object, so an entry carrying them is a bundle that cannot ship.
    """
    emit: Dict[str, Any] = {
        "productRef": product_ref,
        "exposeId": expose_id,
        "fluidVersion": fluid_version,
    }
    emit.update(extra)
    return emit


@pytest.fixture
def write_bundle(tmp_path: Path) -> Callable[..., Path]:
    """Return a function that writes a bundle into ``tmp_path`` and returns it."""

    def _write(
        *,
        flux_documents: Optional[List[Mapping[str, Any]]] = None,
        fluid_documents: Optional[List[Mapping[str, Any]]] = None,
        directory: Optional[Path] = None,
    ) -> Path:
        root = directory if directory is not None else tmp_path / "bundle"
        root.mkdir(parents=True, exist_ok=True)
        documents = flux_documents if flux_documents is not None else minimal_simulation()
        contracts = fluid_documents if fluid_documents is not None else [minimal_contract()]
        (root / "bundle.flux.yml").write_text(
            "\n---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in documents),
            encoding="utf-8",
        )
        for index, contract in enumerate(contracts):
            name = "contract.fluid.yml" if index == 0 else f"contract-{index}.fluid.yml"
            (root / name).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        return root

    return _write


@pytest.fixture
def widget_expose() -> CompiledExpose:
    """The minimal contract's expose, compiled -- for renderer tests."""
    return compile_expose(find_expose(minimal_contract(), "widget_stream"))


def expose_from_schema(schema: List[Mapping[str, Any]], *, expose_id: str = "s") -> CompiledExpose:
    """Compile an ad-hoc expose from a column list."""
    return compile_expose(
        {
            "exposeId": expose_id,
            "kind": "stream",
            "contract": {"schema": list(schema)},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )
