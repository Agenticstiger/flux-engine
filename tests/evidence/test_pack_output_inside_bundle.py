# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An output directory inside the bundle is a product of packing, never an input to it.

``fluxsim run . --verify --ship -o out``, run from the bundle directory, is the
command ``fluxsim init`` prints, and ``out`` then sits inside the bundle. Staging
the whole bundle used to copy ``out/`` along with it, so the second run's archive
held the first run's pack, the third held both, and the Merkle root moved for a
reason that had nothing to do with the bundle.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import List

import pytest

from flux_engine.calibrate.provenance import write_calibration_provenance
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.evidence.pack import pack_evidence
from flux_engine.starter import scaffold

pytestmark = pytest.mark.unit

_RUN_DIGEST = DigestRef(
    value="sha256:" + "aa" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)


def _pack(bundle: Path, run_dir: Path, out_dir: Path) -> List[str]:
    result = pack_evidence(
        bundle,
        run_dir,
        out_dir,
        run_digest=_RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert result.ok, result.problems
    (archive,) = out_dir.glob("*.flux.tgz")
    with tarfile.open(archive) as handle:
        return sorted(handle.getnames())


def test_an_output_directory_inside_the_bundle_is_not_packed_into_the_next_archive(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    scaffold(bundle)
    run_dir = tmp_path / "run"
    write_calibration_provenance(
        run_dir / "calibration-provenance.json",
        world_ref="q3-retention-world",
        dials={},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    out_dir = bundle / "out"

    first = _pack(bundle, run_dir, out_dir)
    second = _pack(bundle, run_dir, out_dir)
    third = _pack(bundle, run_dir, out_dir)

    assert second == first == third
    assert [name for name in third if "/out/" in name or name.endswith("/out")] == []
    # What is attested is still there: the documents and the run's own artifacts.
    assert "bundle/simulation.flux.yml" in third
    assert "bundle/run/calibration-provenance.json" in third
