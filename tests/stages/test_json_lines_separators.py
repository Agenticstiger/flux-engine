# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A level holding U+2028, U+2029 or U+0085 is one value of one JSON line, end to end.

The writers emit canonical JSON, which leaves those characters raw inside a string, and
:meth:`str.splitlines` ends a line at each of them. Every reader of a JSON-lines file splits at
a newline only (:func:`flux_engine.safeio.json_lines`), so a run holding such a level verifies,
its ledgers read as written, a committed slice holding one is read whole, and the sampled and
the full readers count the same lines.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.stages.run import run
from flux_engine.stages.verify import _decoded_rows, _emit_lines, verify

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

SEPARATORS = [" ", " ", "\u0085"]
_VALUES = '["IE", "GB", "FR", "DE"]'


def _bundle_with_level(tmp_path: Path, separator: str) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert text.count(_VALUES) == 1
    # YAML ends a line at U+2028 and U+0085, so the level is written as an escape.
    level = "I" + "".join(f"\\u{ord(c):04x}" for c in separator) + "E"
    simulation.write_text(text.replace(_VALUES, f'["{level}", "GB", "FR", "DE"]'), encoding="utf-8")
    return bundle


@pytest.mark.parametrize("separator", SEPARATORS, ids=["U+2028", "U+2029", "U+0085"])
def test_a_run_holding_a_level_with_a_line_separator_verifies(
    tmp_path: Path, separator: str
) -> None:
    bundle = _bundle_with_level(tmp_path, separator)
    result = run(bundle, state_dir=tmp_path / "state", verify=True)
    assert result.success, result.stderr
    run_id = result.artifacts["plan"]["run_id"]
    emit = tmp_path / "state" / "runs" / run_id / "emits" / "payment_recovery_moment.ndjson"
    raw = emit.read_bytes()
    assert ("I" + separator + "E").encode("utf-8") in raw  # written raw, as canonical JSON does
    lines = raw.decode("utf-8").split("\n")[:-1]
    assert len(lines) == raw.count(b"\n") == 64
    assert _decoded_rows(emit, "payment_recovery_moment")[1] == []
    # The sampled reader and the full reader count the same lines.
    assert list(_emit_lines(emit)) == lines


def test_a_committed_slice_record_holding_a_separator_is_one_record(tmp_path: Path) -> None:
    bundle = _bundle_with_level(tmp_path, " ")
    state = tmp_path / "state"
    ran = run(bundle, state_dir=state)
    assert ran.success, ran.stderr
    rows = [
        {
            "subscriber_id": f"sub-{index:05d}",
            "aborted_at": f"2026-07-{(index % 28) + 1:02d}T09:00:00.000Z",
            "amount": str(25 * (index % 400) + 10),
            "market": ("I E", "GB", "FR", "DE")[index % 4],
        }
        for index in range(200)
    ]
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    from flux_engine.concordance.boundary.slice import read_slice

    committed: Dict[str, str] = {"payment_recovery_moment": f"file://{slice_path}"}
    read, reference = read_slice(committed["payment_recovery_moment"])
    assert len(read) == 200 and reference.last_line == 200
    result = verify(
        bundle,
        run_id=ran.artifacts["plan"]["run_id"],
        state_dir=state,
        committed_slice=committed,
    )
    assert "FLX-5035" not in (result.stderr or "")
    assert "FLX-5035" not in json.dumps(result.artifacts, default=str)


@pytest.mark.unit
@pytest.mark.parametrize("separator", SEPARATORS, ids=["U+2028", "U+2029", "U+0085"])
def test_a_ledger_record_holding_a_separator_is_read_as_the_one_record_written(
    tmp_path: Path, separator: str
) -> None:
    from flux_engine.stages.verify import _ledger_records

    records = [
        {"op": "trait", "ordinal": 0, "name": "tier", "value": f"lo{separator}w"},
        {"op": "market", "ordinal": 0, "market": "IE"},
    ]
    path = tmp_path / "population.ndjson"
    path.write_bytes(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    )
    grouped = _ledger_records(path, ["population"])
    assert grouped is not None and grouped["population"] == records


@pytest.mark.unit
@pytest.mark.parametrize("separator", SEPARATORS, ids=["U+2028", "U+2029", "U+0085"])
def test_the_ship_and_module_readers_take_a_row_holding_a_separator_whole(
    tmp_path: Path, separator: str
) -> None:
    from flux_engine.stages.ship import _emit_rows

    rows = [["a", f"x{separator}y"], ["b", "z"]]
    path = tmp_path / "emit.ndjson"
    path.write_bytes(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    )
    assert _emit_rows(path) == rows
