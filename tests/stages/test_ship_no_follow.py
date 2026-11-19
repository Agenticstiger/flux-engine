# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``ship`` replaces a link at the two run files it writes through a record or a scorecard.

``EVIDENCE.md`` is held by ``test_state_dir_no_follow``; these are the other two write
sites of the stage: the signed fidelity record (``fidelity-record-<exposeId>.json``)
and the scorecard (``scorecard.json``). A link is planted at each name, pointing at a
file outside the state directory; the file is unchanged and the name holds the
document the stage wrote.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.concordance.scorecard import SCORECARD_FILE
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship

from .conftest import FIXTURE_BUNDLE
from .test_scorecard_stages import (  # noqa: F401  (fixtures the scorecard tests share)
    Pinned,
    compiled_and_rows,
    own_profile,
    unpinned,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links"),
]

EXPOSE_ID = "payment_recovery_moment"


def _plant(link: Path, outside: Path) -> Path:
    outside.mkdir(exist_ok=True)
    victim = outside / f"victim-{link.name}"
    victim.write_bytes(b"original")
    link.unlink(missing_ok=True)
    link.symlink_to(victim)
    return victim


def test_ship_replaces_a_link_at_the_fidelity_record(tmp_path: Path) -> None:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))
    attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    record = layout.fidelity_record_path(EXPOSE_ID)
    victim = _plant(record, tmp_path / "outside")

    shipped = ship(
        FIXTURE_BUNDLE,
        run_id=layout.run_id,
        out_dir=tmp_path / "out",
        state_dir=layout.state_dir,
        committed_slice={EXPOSE_ID: f"file://{slice_path}"},
    )

    assert shipped.exit_code == 0, shipped.stderr
    assert victim.read_bytes() == b"original"
    assert record.is_file() and not record.is_symlink()
    assert json.loads(record.read_text(encoding="utf-8"))


def test_ship_replaces_a_link_at_the_scorecard(
    tmp_path: Path, own_profile: Dict[str, Any]  # noqa: F811
) -> None:
    pinned = Pinned(tmp_path, own_profile)
    scorecard = pinned.run_dir / SCORECARD_FILE
    victim = _plant(scorecard, tmp_path / "outside")

    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)

    assert shipped.exit_code == 0, shipped.stderr
    assert victim.read_bytes() == b"original"
    assert scorecard.is_file() and not scorecard.is_symlink()
    assert json.loads(scorecard.read_text(encoding="utf-8"))["overall"]["score"] == "1"
