# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the generator tests.

Everything here is a *declaration*: a schema and a fidelity block. The
generator's whole input surface is those two things plus a seed root, so
the fixtures are the whole input surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from flux_engine.core.seedtree import SeedTreeRoot, derive_root
from flux_engine.generator.paths import GeneratorStreams
from flux_engine.seam.contract import CompiledExpose, compile_expose
from tests.flux_examples import examples_are_required

SIM_ID = "sim-widget"
EXPOSE_ID = "widget_stream"

#: A stand-in seam digest. The generator only needs ``H`` to be a
#: well-formed digest string; where it comes from is the seam's business.
H = "sha256:" + "1" * 64
OTHER_H = "sha256:" + "2" * 64

TIME_BOUNDS: Dict[str, str] = {
    "start": "2026-07-01T00:00:00Z",
    "end": "2026-09-30T23:59:59Z",
}


def expose_from_schema(
    schema: List[Mapping[str, Any]], *, expose_id: str = EXPOSE_ID
) -> CompiledExpose:
    """Compile an ad-hoc expose from a column list."""
    return compile_expose(
        {
            "exposeId": expose_id,
            "kind": "stream",
            "contract": {"schema": list(schema)},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )


def root_for(seam_digest: str = H, seed: int = 42) -> SeedTreeRoot:
    """A seed-tree root derived the way KERNEL-SPEC §3 derives one."""
    from flux_engine.core.seedtree import digest_bytes

    return derive_root(digest_bytes(seam_digest), seed)


@pytest.fixture
def root() -> SeedTreeRoot:
    return root_for()


@pytest.fixture
def streams(root: SeedTreeRoot) -> GeneratorStreams:
    return GeneratorStreams(root=root)


def fidelity(columns: Optional[Mapping[str, Any]] = None, **rest: Any) -> Dict[str, Any]:
    """Build a fidelity block from a column declaration mapping."""
    block: Dict[str, Any] = {"columns": dict(columns or {})}
    block.update(rest)
    return block


#: The FLUX specification repository, checked out beside this one.
FLUX_REPO = Path(__file__).resolve().parents[3] / "FLUX"
TELCO_DIR = FLUX_REPO / "examples" / "telco-payment-recovery"


@pytest.fixture(scope="session")
def telco_dir() -> Path:
    if not TELCO_DIR.is_dir():
        if examples_are_required():
            pytest.fail(f"FLUX_EXAMPLES_REQUIRED is set but {TELCO_DIR} is not checked out")
        pytest.skip(f"the FLUX example bundle is not checked out at {TELCO_DIR}")
    return TELCO_DIR
