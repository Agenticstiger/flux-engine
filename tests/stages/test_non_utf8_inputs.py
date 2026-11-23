# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit or a committed slice that is not UTF-8 is a coded refusal, never an uncaught exception."""

from __future__ import annotations

from pathlib import Path

import pytest

from flux_engine.concordance.boundary.slice import read_slice
from flux_engine.core.errors import FluxError
from flux_engine.stages.run import run
from flux_engine.stages.verify import _decoded_rows, verify

pytestmark = pytest.mark.integration

EXPOSE = "payment_recovery_moment"


def test_verify_refuses_an_emit_that_is_not_utf8_with_a_code(
    bundle_dir: Path, state_dir: Path
) -> None:
    ran = run(bundle_dir, state_dir=state_dir)
    assert ran.success, ran.stderr
    run_id = ran.artifacts["plan"]["run_id"]
    emit = state_dir / "runs" / run_id / "emits" / f"{EXPOSE}.ndjson"
    with emit.open("ab") as stream:
        stream.write(b"\xff\xfe\n")
    result = verify(bundle_dir, run_id=run_id, state_dir=state_dir)
    assert result.exit_code == 1
    assert result.error_code is not None and result.error_code.startswith("FLX-")
    assert "is not UTF-8 text" in (result.stderr or "")


@pytest.mark.unit
def test_the_decoder_of_rows_names_a_file_that_is_not_utf8_as_a_problem(tmp_path: Path) -> None:
    emit = tmp_path / "e.ndjson"
    emit.write_bytes(b'["a"]\n\xff\xfe\n')
    rows, problems = _decoded_rows(emit, "t")
    assert rows == [] and problems == ["t: e.ndjson is not UTF-8 text"]


@pytest.mark.unit
def test_a_committed_slice_that_is_not_utf8_is_refused_as_flx_5035(tmp_path: Path) -> None:
    path = tmp_path / "slice.ndjson"
    path.write_bytes(b'{"a": "1"}\n\xff\xfe\n')
    with pytest.raises(FluxError) as refused:
        read_slice(f"file://{path}")
    assert refused.value.code.code == "FLX-5035"
    assert "is not UTF-8 text" in str(refused.value)


@pytest.mark.unit
def test_the_ship_reader_of_rows_refuses_an_emit_that_is_not_utf8(tmp_path: Path) -> None:
    from flux_engine.stages.ship import _emit_rows

    path = tmp_path / "e.ndjson"
    path.write_bytes(b'["a"]\n\xff\n')
    with pytest.raises(FluxError) as refused:
        _emit_rows(path)
    assert refused.value.code.code == "FLX-8001"
